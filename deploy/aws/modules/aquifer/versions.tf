terraform {
  # 1.6: `moved` into modules, `check` blocks and the features this module uses.
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
