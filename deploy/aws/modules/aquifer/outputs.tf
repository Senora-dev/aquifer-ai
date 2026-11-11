output "instance_id" {
  description = "EC2 instance running `aquifer serve` (SSM Session Manager target)."
  value       = aws_instance.server.id
}

output "public_ip" {
  description = "Elastic IP of the server (point the DNS record for `domain` here)."
  value       = aws_eip.server.public_ip
}

output "access_mode" {
  description = "`private` (no inbound ports; reach the UI and MCP through an SSM tunnel) or `https` (public HTTPS on `domain`, bearer tokens required)."
  value       = var.domain != "" ? "https" : "private"
}

output "ui_url" {
  description = "Browser UI. Private access: through the SSM tunnel on local port 18080."
  value       = var.domain != "" ? "https://${var.domain}/" : "http://127.0.0.1:18080/"
}

output "mcp_url" {
  description = "MCP endpoint: public HTTPS if a domain is set, otherwise through `./aquifer-aws connect`."
  value       = var.domain != "" ? "https://${var.domain}/mcp" : "http://127.0.0.1:18080/mcp (via ./aquifer-aws connect)"
}

output "mcp_tokens_ref" {
  description = "Secret reference of the access tokens (SSM SecureString, created by the server on first start). The value is never in Terraform state."
  value       = "ssm:${local.ssm_prefix}/mcp-tokens"
}

output "ssm_prefix" {
  description = "SSM path holding this deployment's configuration and secrets."
  value       = local.ssm_prefix
}

output "region" {
  value = local.region
}

output "log_group" {
  value = aws_cloudwatch_log_group.server.name
}

output "artifact_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "repos" {
  description = "Repositories configured by Terraform. Sources added in the UI are stored on the server, not here."
  value       = var.repos
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
