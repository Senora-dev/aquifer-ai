module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.8.1"
  name    = "prod"
}

module "orders_queue" {
  source = "git::https://github.com/acme/terraform-modules.git//sqs-queue?ref=v1.4.0"
  name   = "orders-events"
}

resource "aws_db_instance" "orders_db" {
  identifier = "orders-db"
  engine     = "postgres"
  # Placed in the private subnets created by the VPC module.
  db_subnet_group_name = module.vpc.database_subnet_group
}
