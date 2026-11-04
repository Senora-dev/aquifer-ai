# OpenSearch Serverless vector collection, reachable only through its VPC endpoint.

resource "aws_opensearchserverless_vpc_endpoint" "this" {
  name               = "${var.name}-vpce"
  vpc_id             = aws_vpc.this.id
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.endpoints.id]
}

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${var.name}-enc"
  type = "encryption"
  policy = jsonencode({
    Rules       = [{ ResourceType = "collection", Resource = ["collection/${var.name}"] }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${var.name}-net"
  type = "network"
  policy = jsonencode([{
    Rules           = [{ ResourceType = "collection", Resource = ["collection/${var.name}"] }]
    AllowFromPublic = false
    SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.this.id]
  }])
}

resource "aws_opensearchserverless_collection" "this" {
  name             = var.name
  type             = "VECTORSEARCH"
  description      = "Aquifer test collection"
  standby_replicas = var.aoss_standby_replicas
  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
  ]
}

resource "aws_opensearchserverless_access_policy" "data" {
  name = "${var.name}-access"
  type = "data"
  policy = jsonencode([
    {
      Description = "Ingestion: manage the index and write documents"
      Rules = [
        {
          ResourceType = "index"
          Resource     = ["index/${var.name}/*"]
          Permission = ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex",
          "aoss:ReadDocument", "aoss:WriteDocument"]
        },
        {
          ResourceType = "collection"
          Resource     = ["collection/${var.name}"]
          Permission   = ["aoss:DescribeCollectionItems"]
        },
      ]
      Principal = [aws_iam_role.discovery.arn, aws_iam_role.worker.arn]
    },
    {
      Description = "MCP server: read-only"
      Rules = [{
        ResourceType = "index"
        Resource     = ["index/${var.name}/*"]
        Permission   = ["aoss:DescribeIndex", "aoss:ReadDocument"]
      }]
      Principal = [aws_iam_role.mcp_task.arn]
    },
  ])
}
