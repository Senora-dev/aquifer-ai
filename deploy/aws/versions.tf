terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "aquifer"
      Environment = var.name
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = var.region
  # Every SSM parameter of this deployment lives under this path (config, GitHub token, MCP
  # tokens, MCP source credentials). `./aquifer-aws down` removes the path.
  ssm_prefix = "/aquifer/${var.name}"
}
