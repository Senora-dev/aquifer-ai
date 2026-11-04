variable "name" {
  description = "Name prefix for every resource (also used as the OpenSearch collection name)."
  type        = string
  default     = "aquifer-test"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,27}$", var.name))
    error_message = "name must be 3-28 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  description = "AWS region. Must offer OpenSearch Serverless and the Bedrock models below."
  type        = string
  default     = "eu-west-1"
}

variable "repos" {
  description = "Exact owner/repo names to ingest. One trust domain: every MCP token can read all of them."
  type        = list(string)
  validation {
    condition     = length(var.repos) > 0 && alltrue([for r in var.repos : can(regex("^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$", r))])
    error_message = "repos must be a non-empty list of exact owner/repo names (no wildcards)."
  }
}

variable "embedding_model_id" {
  description = "Bedrock embedding model (1024-dim Titan v2 by default)."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "extraction_model_id" {
  description = "Bedrock model or inference profile for LLM extraction (e.g. eu.anthropic.claude-...)."
  type        = string
}

variable "semantic_index_enabled" {
  description = "Run LLM extraction. Deterministic extraction always runs."
  type        = bool
  default     = true
}

variable "aoss_standby_replicas" {
  description = "ENABLED (production, 2 OCU minimum) or DISABLED (test, 1 OCU minimum)."
  type        = string
  default     = "DISABLED"
  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.aoss_standby_replicas)
    error_message = "aoss_standby_replicas must be ENABLED or DISABLED."
  }
}

variable "schedule_minutes" {
  description = "Discovery interval."
  type        = number
  default     = 15
}

variable "create_test_client" {
  description = "Create a tiny SSM-only EC2 instance used to reach the internal MCP endpoint (port forwarding / tests)."
  type        = bool
  default     = true
}

variable "lambda_zip" {
  description = "Path of the Lambda bundle built by ./aquifer-test build."
  type        = string
  default     = ".build/lambda.zip"
}

variable "mcp_image_tag" {
  description = "Tag of the locally built MCP image (set by ./aquifer-test)."
  type        = string
  default     = "aquifer-mcp:local"
}
