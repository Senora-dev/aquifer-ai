# Minimal root module for Aquifer on AWS (private access through an SSM tunnel).
#
#   pip wheel --no-deps -w dist ../../../..        # the Aquifer wheel the server installs
#   terraform init && terraform plan -out tfplan && terraform apply tfplan
#
# Consuming the module from another repository: replace `source` with
#   "git::https://github.com/Senora-dev/aquifer-ai.git//deploy/aws/modules/aquifer?ref=<release tag>"
# and build the wheel from the same tag:
#   pip wheel --no-deps -w dist "git+https://github.com/Senora-dev/aquifer-ai.git@<release tag>"

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Team state: uncomment and fill in (S3 native locking needs Terraform >= 1.10).
  # backend "s3" {
  #   bucket       = "my-terraform-state"
  #   key          = "aquifer/terraform.tfstate"
  #   region       = "eu-west-1"
  #   use_lockfile = true
  # }
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "name" {
  description = "Deployment name: prefixes every resource and the SSM path /aquifer/<name>/."
  type        = string
  default     = "aquifer"
}

variable "extraction_model_id" {
  description = "Claude model or inference profile for LLM extraction and grounded answers; empty turns both off. Check access first: `aquifer install aws --check-only`."
  type        = string
  default     = ""
}

provider "aws" {
  region = var.region
}

locals {
  # Exactly one wheel in ./dist (one() fails if there are several; none is reported by the module).
  wheel = coalesce(one(fileset(path.module, "dist/aquifer-*.whl")), "dist/aquifer-MISSING.whl")
}

module "aquifer" {
  source = "../../modules/aquifer"

  name                = var.name
  wheel_path          = "${path.module}/${local.wheel}"
  extraction_model_id = var.extraction_model_id
  # No repositories here: connect sources in the UI (Sources → Add a source).
}

output "ui_url" {
  value = module.aquifer.ui_url
}

output "instance_id" {
  value = module.aquifer.instance_id
}

output "open_the_ui" {
  description = "Private access: start the SSM tunnel, then open ui_url."
  value       = "aws ssm start-session --region ${var.region} --target ${module.aquifer.instance_id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"18080\"]}'"
}

output "mcp_tokens_ref" {
  value = module.aquifer.mcp_tokens_ref
}
