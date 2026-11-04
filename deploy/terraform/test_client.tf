# A tiny private EC2 instance with no public IP and no SSH, reached only through SSM Session
# Manager. ./aquifer-test connect uses it to forward the internal MCP endpoint to localhost,
# so tests (and your own MCP client) can reach Aquifer from your laptop.

data "aws_ssm_parameter" "al2023_arm64" {
  count = var.create_test_client ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "test_client" {
  count              = var.create_test_client ? 1 : 0
  name               = "${var.name}-test-client"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "test_client_ssm" {
  count      = var.create_test_client ? 1 : 0
  role       = aws_iam_role.test_client[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "test_client" {
  count = var.create_test_client ? 1 : 0
  name  = "${var.name}-test-client"
  role  = aws_iam_role.test_client[0].name
}

resource "aws_security_group" "test_client" {
  count       = var.create_test_client ? 1 : 0
  name        = "${var.name}-test-client"
  description = "Aquifer SSM test client (no inbound)"
  vpc_id      = aws_vpc.this.id
  egress {
    description = "HTTPS (SSM)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "MCP load balancer"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.this.cidr_block]
  }
}

resource "aws_instance" "test_client" {
  count                       = var.create_test_client ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023_arm64[0].value
  instance_type               = "t4g.nano"
  subnet_id                   = aws_subnet.private[0].id
  vpc_security_group_ids      = [aws_security_group.test_client[0].id]
  iam_instance_profile        = aws_iam_instance_profile.test_client[0].name
  associate_public_ip_address = false
  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    encrypted = true
  }
  tags = { Name = "${var.name}-test-client" }
}
