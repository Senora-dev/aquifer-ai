variable "name" {
  description = "Name of this deployment; prefixes every resource."
  type        = string
  default     = "aquifer"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,27}$", var.name))
    error_message = "name must be 3-28 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "repos" {
  description = "Exact owner/repo names to ingest. One trust domain: every MCP token can read all of them."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for r in var.repos : can(regex("^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$", r))])
    error_message = "repos must be exact owner/repo names (no wildcards)."
  }
}

variable "embedding_model_id" {
  description = "Bedrock embedding model (Titan Text Embeddings v2, 1024 dimensions)."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "extraction_model_id" {
  description = "Bedrock model or inference profile for LLM extraction, e.g. eu.anthropic.claude-haiku-4-5-20251001-v1:0. Empty disables LLM extraction."
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "t4g.small (2 GiB) fits ~25,000 chunks; t4g.micro ~5,000; t4g.medium ~100,000."
  type        = string
  default     = "t4g.small"
}

variable "data_volume_gb" {
  description = "Size of the encrypted data volume (index, queue, state)."
  type        = number
  default     = 10
}

variable "sync_interval_minutes" {
  description = "How often GitHub is checked for changes. Unchanged content costs no model calls."
  type        = number
  default     = 15
}

variable "snapshot_retention_days" {
  description = "Daily snapshots of the data volume to keep (0 disables backups)."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

variable "domain" {
  description = "Optional DNS name for public HTTPS (e.g. aquifer.example.com). Point an A record at the elastic IP; Caddy obtains a Let's Encrypt certificate. Empty = no inbound ports; reach MCP through an SSM tunnel."
  type        = string
  default     = ""
}

variable "https_allowed_cidrs" {
  description = "Source ranges allowed to reach HTTPS when `domain` is set. Requests still need an MCP bearer token."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "wheel_path" {
  description = "Path of the Aquifer wheel to install on the server (build it with `pip wheel --no-deps`). It is uploaded to the deployment's artifact bucket."
  type        = string
  validation {
    condition     = fileexists(var.wheel_path)
    error_message = "wheel_path does not exist. Build the wheel first: pip wheel --no-deps -w dist <aquifer source or git URL>."
  }
}

variable "extra_env" {
  description = "Additional AQUIFER_* settings for the server (see src/aquifer/core/config.py)."
  type        = map(string)
  default     = {}
}
