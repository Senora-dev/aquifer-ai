# Root module used by ./aquifer-aws and `aquifer install aws`. It configures the provider and
# calls the reusable module in ./modules/aquifer. State: see backend.tf (written by
# `aquifer install aws --backend s3`); without it, state is the local file terraform.tfstate.
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
