output "region" {
  value = local.region
}

output "mcp_endpoint" {
  description = "Internal NLB DNS name (reachable only inside the VPC)."
  value       = aws_lb.mcp.dns_name
}

output "mcp_auth_secret_arn" {
  value = aws_secretsmanager_secret.mcp_tokens.arn
}

output "github_token_secret_arn" {
  value = aws_secretsmanager_secret.github_token.arn
}

output "discovery_function" {
  value = aws_lambda_function.discovery.function_name
}

output "worker_function" {
  value = aws_lambda_function.worker.function_name
}

output "queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "collection_endpoint" {
  value = aws_opensearchserverless_collection.this.collection_endpoint
}

output "ecs_cluster" {
  value = aws_ecs_cluster.this.name
}

output "ecs_service" {
  value = aws_ecs_service.mcp.name
}

output "mcp_log_group" {
  value = aws_cloudwatch_log_group.mcp.name
}

output "test_client_instance_id" {
  value = var.create_test_client ? aws_instance.test_client[0].id : null
}

output "repos" {
  value = var.repos
}
