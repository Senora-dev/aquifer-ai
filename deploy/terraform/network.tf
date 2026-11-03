# VPC with public subnets (NAT only) and private subnets for every workload.

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  tags              = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, 10 + count.index)
  tags              = { Name = "${var.name}-private-${count.index}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = var.name }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- security groups (same roles as the CDK stack) ---------------------------------------

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-endpoints"
  description = "Aquifer VPC endpoints"
  vpc_id      = aws_vpc.this.id
  ingress {
    description = "HTTPS from the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.this.cidr_block]
  }
}

resource "aws_security_group" "ingestion" {
  name        = "${var.name}-ingestion"
  description = "Aquifer ingestion Lambdas"
  vpc_id      = aws_vpc.this.id
  egress {
    description = "HTTPS only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The MCP and NLB groups reference each other, so their rules are standalone resources.
resource "aws_security_group" "mcp" {
  name        = "${var.name}-mcp"
  description = "Aquifer MCP tasks"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "mcp_https" {
  security_group_id = aws_security_group.mcp.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS only (ECR, Logs via NAT; AWS services via endpoints)"
}

resource "aws_security_group" "nlb" {
  name        = "${var.name}-nlb"
  description = "Aquifer MCP load balancer"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "nlb_clients" {
  security_group_id = aws_security_group.nlb.id
  cidr_ipv4         = aws_vpc.this.cidr_block
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "MCP clients inside the VPC"
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_tasks" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.mcp.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
  description                  = "to MCP tasks (traffic + health checks)"
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_nlb" {
  security_group_id            = aws_security_group.mcp.id
  referenced_security_group_id = aws_security_group.nlb.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
  description                  = "from the MCP load balancer only"
}

# --- endpoints ------------------------------------------------------------------------------

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(["bedrock-runtime", "secretsmanager", "sqs"])
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${local.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${var.name}-${each.key}" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.name}-s3" }
}
