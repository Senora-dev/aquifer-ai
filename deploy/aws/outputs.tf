output "instance_id" {
  value = aws_instance.server.id
}

output "public_ip" {
  description = "Elastic IP of the server (point the DNS record for `domain` here)."
  value       = aws_eip.server.public_ip
}

output "mcp_url" {
  description = "MCP endpoint: public HTTPS if a domain is set, otherwise through `./aquifer-aws connect`."
  value       = var.domain != "" ? "https://${var.domain}/mcp" : "http://127.0.0.1:18080/mcp (via ./aquifer-aws connect)"
}

output "ssm_prefix" {
  value = local.ssm_prefix
}

output "log_group" {
  value = aws_cloudwatch_log_group.server.name
}

output "artifact_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "repos" {
  value = var.repos
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
