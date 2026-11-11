# Terraform module: Aquifer on AWS (small profile)

One deployment of Aquifer: an ARM EC2 instance running `aquifer serve` (UI and MCP), an
encrypted data volume with daily snapshots, an artifact bucket, SSM parameters, a log group and
the IAM roles they need. No NAT gateway, load balancer or managed database; no inbound ports
unless `domain` is set. Guided installation and the full guide: [docs/install-aws.md](../../../../docs/install-aws.md).

The module configures no provider and no backend: the root module does. Pin `source` and the
wheel to the same release tag.

```hcl
module "aquifer" {
  source     = "git::https://github.com/Senora-dev/aquifer-ai.git//deploy/aws/modules/aquifer?ref=v0.2.0"
  name       = "aquifer"
  wheel_path = "${path.module}/dist/aquifer-0.2.0-py3-none-any.whl"   # pip wheel --no-deps
}
```

## Inputs

| Name | Default | Description |
| --- | --- | --- |
| `name` | `aquifer` | Prefixes every resource and the SSM path `/aquifer/<name>/` |
| `wheel_path` | (required) | Aquifer wheel the server installs (must exist) |
| `repos` | `[]` | Repositories configured by Terraform; sources are usually added in the UI |
| `embedding_model_id` | `amazon.titan-embed-text-v2:0` | Bedrock embeddings (required) |
| `extraction_model_id` | `""` | Claude model or inference profile for extraction and answers; empty = off |
| `instance_type` | `t4g.small` | `t4g.medium` / `t4g.large` for more content |
| `data_volume_gb` | `10` | Encrypted gp3 data volume (grow only) |
| `sync_interval_minutes` | `15` | Source sync schedule |
| `snapshot_retention_days` | `7` | Daily snapshots kept; 0 disables |
| `log_retention_days` | `30` | CloudWatch Logs |
| `domain` | `""` | Public HTTPS on this name (Caddy + Let's Encrypt); empty = private (SSM tunnel) |
| `https_allowed_cidrs` | `["0.0.0.0/0"]` | Who may reach HTTPS when `domain` is set (tokens still required) |
| `extra_env` | `{}` | Additional `AQUIFER_*` settings |

## Outputs

`instance_id` (SSM target), `ui_url`, `mcp_url`, `access_mode` (`private`/`https`),
`mcp_tokens_ref` (where the access tokens are; never their values), `ssm_prefix`, `public_ip`,
`region`, `log_group`, `artifact_bucket`, `repos`, `data_volume_id`.

## Upgrading from the pre-module layout

`deploy/aws/main.tf` carries `moved` blocks from the root addresses (`aws_instance.server`, …) to
`module.aquifer.*`. If you copied the old root module into your own repository, add the same
blocks (one per resource in `main.tf`) before switching to this module, and check that the plan
shows only moves.
