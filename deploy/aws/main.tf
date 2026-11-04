# Aquifer, small profile: one ARM instance, one encrypted data volume, daily snapshots.
# No NAT gateway, load balancer, VPC endpoints or managed database. See docs/cost.md.

# --- network: one public subnet, no inbound ports unless `domain` is set ----------------------

data "aws_ec2_instance_type_offerings" "here" {
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
  location_type = "availability-zone"
}

locals {
  az = sort(data.aws_ec2_instance_type_offerings.here.locations)[0]
}

resource "aws_vpc" "this" {
  cidr_block           = "10.80.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = "10.80.0.0/26"
  availability_zone = local.az
  tags              = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "server" {
  name        = "${var.name}-server"
  description = "Aquifer server: HTTPS egress; inbound only for public HTTPS when a domain is set"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "HTTPS to GitHub, AWS APIs (Bedrock, SSM, S3, CloudWatch), package mirrors, MCP sources"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = var.domain == "" ? [] : [80, 443]
    content {
      description = ingress.value == 80 ? "ACME HTTP-01 challenge and redirect" : "MCP over HTTPS (bearer tokens required)"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ingress.value == 80 ? ["0.0.0.0/0"] : var.https_allowed_cidrs
    }
  }
}

# --- identity: what the server may do ---------------------------------------------------------

locals {
  profile_prefixes = ["us.", "eu.", "apac.", "us-gov.", "jp.", "au.", "ca.", "global."]
  model_ids        = compact(distinct([var.embedding_model_id, var.extraction_model_id]))
  # IAM resources needed to invoke a model id (foundation model or cross-region inference profile).
  model_arns = flatten([for id in local.model_ids : (
    anytrue([for p in local.profile_prefixes : startswith(id, p)])
    ? [
      "arn:${local.partition}:bedrock:${local.region}:${local.account}:inference-profile/${id}",
      "arn:${local.partition}:bedrock:*::foundation-model/${join(".", slice(split(".", id), 1, length(split(".", id))))}",
    ]
    : ["arn:${local.partition}:bedrock:${local.region}::foundation-model/${id}"]
  )])
}

resource "aws_iam_role" "server" {
  name = "${var.name}-server"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "server" {
  name = "aquifer"
  role = aws_iam_role.server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Models"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.model_arns
      },
      {
        # Config, GitHub token, MCP tokens and MCP source credentials (renewed OAuth tokens are
        # written back). SecureStrings use the AWS-managed aws/ssm key.
        Sid      = "OwnParameters"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = ["arn:${local.partition}:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}/*"]
      },
      {
        Sid      = "Artifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${aws_cloudwatch_log_group.server.arn}:*"]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "server" {
  name = "${var.name}-server"
  role = aws_iam_role.server.name
}

# --- artifacts and configuration --------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${var.name}-artifacts-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_object" "wheel" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "artifacts/${basename(var.wheel_path)}"
  source = "${path.module}/${var.wheel_path}"
  etag   = filemd5("${path.module}/${var.wheel_path}")
}

locals {
  env = merge({
    AQUIFER_ENVIRONMENT                    = var.name
    AQUIFER_LOG_LEVEL                      = "INFO"
    AQUIFER_VECTOR_STORE__BACKEND          = "sqlite"
    AQUIFER_VECTOR_STORE__SQLITE_PATH      = "/var/lib/aquifer/index.db"
    AQUIFER_NODE__DATA_DIR                 = "/var/lib/aquifer"
    AQUIFER_NODE__SYNC_INTERVAL_S          = tostring(var.sync_interval_minutes * 60)
    AQUIFER_EMBEDDING__REGION              = local.region
    AQUIFER_EMBEDDING__MODEL_ID            = var.embedding_model_id
    AQUIFER_SEMANTIC_INDEX__REGION         = local.region
    AQUIFER_SEMANTIC_INDEX__MODEL_ID       = var.extraction_model_id
    AQUIFER_SEMANTIC_INDEX__ENABLED        = var.extraction_model_id == "" ? "false" : "true"
    AQUIFER_GITHUB__REPO_ALLOWLIST         = jsonencode(var.repos)
    AQUIFER_GITHUB__TOKEN_SECRET           = length(var.repos) > 0 ? "ssm:${local.ssm_prefix}/github-token" : ""
    AQUIFER_MCP__AUTH_TOKENS_REF           = "ssm:${local.ssm_prefix}/mcp-tokens"
    AQUIFER_MCP__HOST                      = "127.0.0.1"
    AQUIFER_MCP__PORT                      = "8080"
    AQUIFER_INTERCEPTORS                   = jsonencode(["aquifer.semantic.interceptor:SemanticIndexInterceptor"])
    AQUIFER_SSM_PREFIX                     = local.ssm_prefix
    AQUIFER_INTEGRATOR__CREDENTIALS_PREFIX = "ssm:${local.ssm_prefix}/sources"
    AQUIFER_INTEGRATOR__PUBLIC_URL         = var.domain != "" ? "https://${var.domain}" : "http://127.0.0.1:18080"
    AWS_REGION                             = local.region
    AWS_DEFAULT_REGION                     = local.region
  }, var.extra_env)
}

# The server's environment file. No secrets: credentials are referenced, not included.
resource "aws_ssm_parameter" "config" {
  name  = "${local.ssm_prefix}/config"
  type  = "String"
  tier  = "Standard"
  value = join("\n", [for k in sort(keys(local.env)) : "${k}='${local.env[k]}'"])
}

resource "aws_cloudwatch_log_group" "server" {
  name              = "/aquifer/${var.name}"
  retention_in_days = var.log_retention_days
}

# --- the server -------------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_ebs_volume" "data" {
  availability_zone = local.az
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-data", AquiferBackup = var.name }
}

resource "aws_instance" "server" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.insecure_value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.server.id]
  iam_instance_profile   = aws_iam_instance_profile.server.name

  # Standard credits: the instance slows down when its CPU credits run out instead of
  # billing surplus credits, so the monthly cost cannot exceed the on-demand price.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    name       = var.name
    region     = local.region
    bucket     = aws_s3_bucket.artifacts.id
    volume_id  = aws_ebs_volume.data.id
    ssm_prefix = local.ssm_prefix
    log_group  = aws_cloudwatch_log_group.server.name
    domain     = var.domain
  })

  lifecycle {
    # A newer AMI must not replace the server on every apply; patch with `./aquifer-aws upgrade-os`.
    # The elastic IP makes AWS report a public address; that is not a configuration change.
    ignore_changes = [ami, associate_public_ip_address]
  }

  tags = { Name = var.name }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.server.id
}

resource "aws_eip" "server" {
  domain   = "vpc"
  instance = aws_instance.server.id
  tags     = { Name = var.name }
}

# --- backups ----------------------------------------------------------------------------------

resource "aws_iam_role" "dlm" {
  count = var.snapshot_retention_days > 0 ? 1 : 0
  name  = "${var.name}-snapshots"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "dlm.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  count      = var.snapshot_retention_days > 0 ? 1 : 0
  role       = aws_iam_role.dlm[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  count              = var.snapshot_retention_days > 0 ? 1 : 0
  description        = "Daily snapshots of the ${var.name} data volume"
  execution_role_arn = aws_iam_role.dlm[0].arn
  state              = "ENABLED"
  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { AquiferBackup = var.name }
    schedule {
      name      = "daily"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }
      retain_rule {
        count = var.snapshot_retention_days
      }
    }
  }
}
