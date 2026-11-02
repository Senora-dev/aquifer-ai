module "orders_queue" {
  source = "git::https://github.com/acme/terraform-modules.git//sqs-queue?ref=v1.2.0"
  name   = "orders-events-staging"
}
