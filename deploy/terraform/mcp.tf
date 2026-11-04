# The MCP server: image in ECR, Fargate service, internal NLB, bearer-token secret.

locals {
  image_tag = element(split(":", var.mcp_image_tag), 1)
  image_uri = "${aws_ecr_repository.mcp.repository_url}:${local.image_tag}"
}

resource "aws_ecr_repository" "mcp" {
  name                 = "${var.name}-mcp"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Push the image built locally by ./aquifer-test build. Re-runs whenever the tag changes.
resource "terraform_data" "push_image" {
  triggers_replace = [var.mcp_image_tag, aws_ecr_repository.mcp.repository_url]
  provisioner "local-exec" {
    command = <<-EOT
      set -e
      aws ecr get-login-password --region ${local.region} \
        | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.mcp.repository_url)[0]} >/dev/null
      docker tag ${var.mcp_image_tag} ${local.image_uri}
      docker push --quiet ${local.image_uri}
    EOT
  }
}

resource "random_password" "mcp_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "mcp_tokens" {
  name                    = "${var.name}/mcp-auth-tokens"
  description             = "Aquifer MCP bearer tokens: JSON object {principal: token}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "mcp_tokens" {
  secret_id     = aws_secretsmanager_secret.mcp_tokens.id
  secret_string = jsonencode({ default = random_password.mcp_token.result })
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/aquifer/${var.name}/mcp"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp_execution" {
  name               = "${var.name}-mcp-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "mcp_execution" {
  role       = aws_iam_role.mcp_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "mcp_execution_secret" {
  name = "read-auth-tokens"
  role = aws_iam_role.mcp_execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.mcp_tokens.arn }]
  })
}

resource "aws_iam_role" "mcp_task" {
  name               = "${var.name}-mcp-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "mcp_task" {
  name = "aquifer"
  role = aws_iam_role.mcp_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Query embedding only.
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.model_arns[var.embedding_model_id] },
      { Effect = "Allow", Action = ["aoss:APIAccessAll"], Resource = aws_opensearchserverless_collection.this.arn },
    ]
  })
}

resource "aws_ecs_task_definition" "mcp" {
  family                   = "${var.name}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.mcp_execution.arn
  task_role_arn            = aws_iam_role.mcp_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  container_definitions = jsonencode([{
    name                   = "mcp"
    image                  = local.image_uri
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = 8080, protocol = "tcp" }]
    environment            = [for k, v in merge(local.base_env, { AQUIFER_MCP__PORT = "8080" }) : { name = k, value = v }]
    secrets                = [{ name = "AQUIFER_MCP__AUTH_TOKENS", valueFrom = aws_secretsmanager_secret.mcp_tokens.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.mcp.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "mcp"
      }
    }
  }])
  depends_on = [terraform_data.push_image]
}

resource "aws_lb" "mcp" {
  name               = "${var.name}-mcp"
  internal           = true
  load_balancer_type = "network"
  subnets            = aws_subnet.private[*].id
  security_groups    = [aws_security_group.nlb.id]
}

resource "aws_lb_target_group" "mcp" {
  name        = "${var.name}-mcp"
  port        = 8080
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id
  health_check {
    protocol            = "TCP"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  deregistration_delay = 10
}

resource "aws_lb_listener" "mcp" {
  load_balancer_arn = aws_lb.mcp.arn
  port              = 80
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp.arn
  }
}

resource "aws_ecs_service" "mcp" {
  name            = "${var.name}-mcp"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.mcp.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.mcp.arn
    container_name   = "mcp"
    container_port   = 8080
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.mcp, aws_secretsmanager_secret_version.mcp_tokens]
}
