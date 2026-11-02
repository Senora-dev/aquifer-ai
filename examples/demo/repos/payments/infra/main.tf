# Production infrastructure for the payments platform.

module "billing_queue" {
  source = "git::https://github.com/acme/terraform-modules.git//sqs-queue?ref=v1.4.0"
  name   = "billing-events"
}

resource "aws_db_instance" "payments_db" {
  identifier     = "payments-db"
  engine         = "postgres"
  instance_class = "db.t4g.medium"
}

resource "aws_ecs_task_definition" "billing_api" {
  family = "billing-api"
  container_definitions = jsonencode([{
    name  = "billing-api"
    image = "acme/billing-api:1.42.0"
    environment = [
      { name = "DB_HOST", value = aws_db_instance.payments_db.address },
      { name = "QUEUE_URL", value = module.billing_queue.queue_url },
    ]
  }])
  depends_on = [aws_db_instance.payments_db]
}
