# Discovery (scheduled) and Worker (SQS) Lambdas, their queue, state and credentials.

locals {
  profile_prefixes = ["us.", "eu.", "apac.", "us-gov.", "jp.", "au.", "ca.", "global."]

  # IAM resources needed to invoke a model id (foundation model or inference profile).
  model_arns = {
    for id in distinct([var.embedding_model_id, var.extraction_model_id]) : id => (
      anytrue([for p in local.profile_prefixes : startswith(id, p)])
      ? [
        "arn:${data.aws_partition.current.partition}:bedrock:${local.region}:${local.account}:inference-profile/${id}",
        "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/${join(".", slice(split(".", id), 1, length(split(".", id))))}",
      ]
      : ["arn:${data.aws_partition.current.partition}:bedrock:${local.region}::foundation-model/${id}"]
    )
  }

  base_env = {
    AQUIFER_VECTOR_STORE__ENDPOINT   = aws_opensearchserverless_collection.this.collection_endpoint
    AQUIFER_VECTOR_STORE__INDEX      = "aquifer-context"
    AQUIFER_VECTOR_STORE__REGION     = local.region
    AQUIFER_EMBEDDING__REGION        = local.region
    AQUIFER_EMBEDDING__MODEL_ID      = var.embedding_model_id
    AQUIFER_SEMANTIC_INDEX__REGION   = local.region
    AQUIFER_SEMANTIC_INDEX__MODEL_ID = var.extraction_model_id
    AQUIFER_SEMANTIC_INDEX__ENABLED  = var.semantic_index_enabled ? "true" : "false"
    AQUIFER_GITHUB__REPO_ALLOWLIST   = jsonencode(var.repos)
  }
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name}-jobs"
  visibility_timeout_seconds = 1800 # 6x the worker timeout
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_s3_bucket" "state" {
  bucket_prefix = "${var.name}-state-"
  force_destroy = true # only watermarks live here
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state_tls" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# The value is written by ./aquifer-test (from `gh auth token`), never through Terraform,
# so the GitHub token is not stored in Terraform state.
resource "aws_secretsmanager_secret" "github_token" {
  name                    = "${var.name}/github-token"
  description             = "Aquifer GitHub token (read access to the configured repos)"
  recovery_window_in_days = 0
}

# --- IAM ------------------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "discovery" {
  name               = "${var.name}-discovery"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "vpc_access" {
  for_each   = { discovery = aws_iam_role.discovery.name, worker = aws_iam_role.worker.name }
  role       = each.value
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "discovery" {
  name = "aquifer"
  role = aws_iam_role.discovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.jobs.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.state.arn}/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.github_token.arn },
      { Effect = "Allow", Action = ["aoss:APIAccessAll"], Resource = aws_opensearchserverless_collection.this.arn },
    ]
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "aquifer"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]
        Resource = aws_sqs_queue.jobs.arn
      },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.github_token.arn },
      { Effect = "Allow", Action = ["aoss:APIAccessAll"], Resource = aws_opensearchserverless_collection.this.arn },
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = flatten(values(local.model_arns)) },
    ]
  })
}

# --- functions ------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(["discovery", "worker"])
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 14
}

locals {
  lambda_common = {
    runtime       = "python3.12"
    architectures = ["arm64"]
    memory_size   = 1024
    filename      = "${path.module}/${var.lambda_zip}"
    env = merge(local.base_env, {
      AQUIFER_INGESTION__QUEUE_URL    = aws_sqs_queue.jobs.url
      AQUIFER_INGESTION__STATE_BUCKET = aws_s3_bucket.state.bucket
      AQUIFER_GITHUB__TOKEN_SECRET    = aws_secretsmanager_secret.github_token.arn
      AQUIFER_INTERCEPTORS            = jsonencode(["aquifer.semantic.interceptor:SemanticIndexInterceptor"])
    })
  }
}

resource "aws_lambda_function" "discovery" {
  function_name    = "${var.name}-discovery"
  role             = aws_iam_role.discovery.arn
  handler          = "aquifer.ingestion.discovery_handler.handler"
  runtime          = local.lambda_common.runtime
  architectures    = local.lambda_common.architectures
  memory_size      = local.lambda_common.memory_size
  timeout          = 300
  filename         = local.lambda_common.filename
  source_code_hash = filebase64sha256(local.lambda_common.filename)
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.ingestion.id]
  }
  environment {
    variables = local.lambda_common.env
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.vpc_access]
}

resource "aws_lambda_function" "worker" {
  function_name    = "${var.name}-worker"
  role             = aws_iam_role.worker.arn
  handler          = "aquifer.ingestion.worker_handler.handler"
  runtime          = local.lambda_common.runtime
  architectures    = local.lambda_common.architectures
  memory_size      = local.lambda_common.memory_size
  timeout          = 300
  filename         = local.lambda_common.filename
  source_code_hash = filebase64sha256(local.lambda_common.filename)
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.ingestion.id]
  }
  environment {
    variables = local.lambda_common.env
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.vpc_access]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
  scaling_config {
    maximum_concurrency = 5
  }
}

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.name}-discovery"
  schedule_expression = "rate(${var.schedule_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "schedule" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.discovery.arn
}

resource "aws_lambda_permission" "schedule" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.discovery.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# --- alarms ---------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "${var.name}-dlq-not-empty"
  alarm_description   = "Aquifer ingestion jobs failed permanently (see the DLQ)"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${var.name}-worker-errors"
  alarm_description   = "Aquifer worker Lambda errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.worker.function_name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}
