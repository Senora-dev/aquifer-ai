# Aquifer on AWS: small profile (default)

This profile runs Aquifer on one small ARM server in your AWS account, for about **$20 a month**
in eu-west-1. Model usage is billed on top; see [docs/cost.md](../../docs/cost.md). Terraform
manages everything, so `./aquifer-aws down` removes it completely.

```
your agent ──SSM tunnel (or HTTPS)──▶ EC2 t4g.small ── aquifer serve
                                        ├─ MCP server (bearer tokens)
                                        ├─ scheduler + job queue (retries, dead letters)
                                        ├─ ingestion workers (GitHub → extraction → embeddings)
                                        └─ SQLite index on an encrypted EBS volume (daily snapshots)
```

There is no NAT gateway, load balancer, VPC endpoint, OpenSearch or managed database. For
multi-AZ availability or more than ~50,000 chunks, use the [scale profile](../../docs/deployment.md#scale-profile-cdk-opensearch-serverless).

## Prerequisites

You need:
- AWS CLI v2, logged in
- Terraform ≥ 1.6
- Python 3.11+
- the AWS Session Manager plugin: `brew install --cask session-manager-plugin`
- the GitHub CLI (`gh auth login`), if you ingest GitHub

Bedrock access in the region:
- Titan Text Embeddings v2
- optionally a Claude model for extraction

`./aquifer-aws check` verifies all of this. It explains any Bedrock setup that is missing, for
example the one-time Anthropic use-case form or a Marketplace subscription.

Docker is **not** required.

## From zero to a working endpoint

```bash
cd deploy/aws
./aquifer-aws check                      # tools, AWS login, Bedrock models
AQUIFER_REPOS='["your-org/service-a","your-org/infra"]' ./aquifer-aws configure
./aquifer-aws plan                       # builds the wheel, lists every resource, creates nothing
./aquifer-aws up                         # applies that plan, stores the GitHub token, first sync
./aquifer-aws connect                    # tunnel to localhost + the `claude mcp add` command
```

`up` takes about 5 minutes; the first boot installs Python and Aquifer. `configure` picks the
first Claude model your account can invoke, preferring Claude Haiku 4.5: it costs about a third
of Sonnet per document and scored as well as Sonnet in the retrieval benchmark. Set
`AQUIFER_EXTRACTION_MODEL=""` to turn LLM extraction off, or name a model to use that one.

To try Aquifer without your own repositories, run `./aquifer-aws seed` before `configure`. It
creates three private GitHub repositories with synthetic data, issues and pull requests.

## What gets created (23 resources)

| Area | Resources | Monthly cost (eu-west-1) |
| --- | --- | --- |
| Server | EC2 t4g.small, CPU credits `standard`, IMDSv2, automatic recovery | $13.43 |
| Storage | 8 GiB root + 10 GiB data volume (gp3, encrypted) | $1.58 |
| Address | 1 Elastic IP (egress without NAT; stable address) | $3.65 |
| Backups | Data Lifecycle Manager policy: daily snapshot at 03:00 UTC, 7 kept | ≤ $0.50 |
| Logs | CloudWatch log group, 30-day retention | ≈ $0.05–0.30 |
| Config | SSM parameter (non-secret settings); S3 bucket with the Aquifer wheel | ~$0 |
| Network | VPC, one public subnet, internet gateway, route table, security group (no inbound ports) | free |
| Identity | IAM role + instance profile (Bedrock invoke on the configured models, own SSM path, own bucket, own log group), DLM role | free |

Secrets are never in Terraform state:
- `up` copies the GitHub token from `gh auth token` into SSM Parameter Store, as a SecureString
  under `/aquifer/<name>/`.
- The server creates the MCP token on first start.

## Daily use

| Command | What it does |
| --- | --- |
| `./aquifer-aws status` | Queue, last sync, index size, extraction backlog, dead letters |
| `./aquifer-aws sync [--full]` | Sync now instead of waiting (default every 15 minutes). `--full` ignores the incremental watermark; unchanged content is still skipped |
| `./aquifer-aws connect` | SSM tunnel to `http://127.0.0.1:18080/mcp` and the command to register it in Claude Code |
| `./aquifer-aws token` | Print the MCP bearer token (read from SSM) |
| `./aquifer-aws logs 30` | Last 30 minutes of server logs |
| `./aquifer-aws metrics` | Memory, CPU, disk, index files |
| `./aquifer-aws retry-failed` | Re-run LLM extraction that exhausted its retries (after fixing model access) |
| `./aquifer-aws run dead-letters [--requeue all]` | Jobs that failed 5 times, with their errors |
| `./aquifer-aws deploy` | Roll out code or `terraform.tfvars` changes (no instance replacement, index kept) |
| `./aquifer-aws github-token` | After rotating your GitHub token: copy the new one into SSM, restart, sync |
| `./aquifer-aws upgrade-os` | `dnf upgrade` + reboot (~1 minute without queries) |
| `./aquifer-aws down` | Destroy everything and delete `/aquifer/<name>/*` from SSM. Existing snapshots are kept |

Server-side commands run through SSM Run Command, so no SSH and no open ports are needed.

### Connecting other tools (MCP Integrator)

```bash
./aquifer-aws source-token eng-github --from-gh        # a token, laptop → SSM (never argv)
./aquifer-aws sources add eng-github --provider github --token-ref ssm:/aquifer/<name>/sources/eng-github/token
./aquifer-aws sources template eng-github --stream discussions --target your-org/your-repo
./aquifer-aws sources preview eng-github && ./aquifer-aws sources enable eng-github
./aquifer-aws authorize <oauth-source>                 # OAuth providers: tunnel + browser
./aquifer-aws sources status
./aquifer-aws test-integrator                          # e2e against GitHub's MCP server
```

Walkthrough, providers, coverage and safety: [docs/integrator.md](../../docs/integrator.md).
Source credentials live under `/aquifer/<name>/sources/` in SSM and are removed by `down`.

### Exposing MCP over HTTPS (optional)

By default the endpoint is reachable only through the SSM tunnel. To give agents a public URL:

1. Set `domain = "aquifer.example.com"` in `terraform.tfvars` (and optionally
   `https_allowed_cidrs`).
2. Run `./aquifer-aws deploy`.
3. Create a DNS A record pointing to the `public_ip` output.

On boot, Caddy obtains a Let's Encrypt certificate. Every request still needs a bearer token.

### Backups and recovery

- **Instance failure:** EC2 automatic recovery restarts the instance. The data volume is
  separate from the instance: replacing the server (for example after changing `user_data`)
  keeps the index.
- **Volume loss or corruption:** restore the latest snapshot.
  1. Create a volume from the snapshot in the same AZ (EC2 console → Snapshots → Create
     volume).
  2. `terraform state rm aws_ebs_volume.data aws_volume_attachment.data`
  3. `terraform import aws_ebs_volume.data <new-volume-id>`
  4. `./aquifer-aws deploy`
  RPO ≤ 24 hours. Anything newer is re-fetched by the next sync.
- **Without snapshots:** the index is derived data. A fresh deployment with `sync --full`
  rebuilds it from the sources. That costs embeddings and extraction again (see
  [docs/cost.md](../../docs/cost.md)).

### Rotating the GitHub token

Logging in again with `gh` does **not** update the copy in SSM. Run `./aquifer-aws github-token`.
It copies the token through a 0600 temporary file (never argv), restarts the service and syncs,
which validates access end to end. Revoke the old token on GitHub afterwards.

## Tests

```bash
./aquifer-aws test          # 25 end-to-end checks through a real MCP client + idempotency
./aquifer-aws test-updates  # commits a change and a deletion to GitHub, re-syncs, verifies, reverts
```

`./aquifer-aws test-integrator` runs the full MCP Integrator check against GitHub's MCP server:
- import, retrieval, unchanged runs, updates and deletions
- retries during an injected network fault
- recovery after `kill -9` mid-run

Its mutations go only to a dedicated fixture repository (see
[docs/integrator.md](../../docs/integrator.md#test-fixtures)).

`test` and `test-updates` expect the synthetic repositories created by `./aquifer-aws seed`; the
checks look for their content. Reports go to `.build/e2e-report.json` and
`.build/e2e-updates.json`.

## Limits of this profile

- One server in one Availability Zone. Recovery takes minutes, and there is no redundancy
  during an AZ outage.
- Vectors are held in memory. On t4g.small, measured: ~25,000 chunks with 22 ms search;
  50,000 chunks still work, with ~160 ms search at baseline CPU. Use t4g.medium or the scale
  profile beyond that.
- With `standard` CPU credits the bill cannot exceed the on-demand price. A new instance starts
  with no credits, though, so the first boot and the first large sync run at the baseline
  (20% of 2 vCPUs) until credits accrue (24 per hour).

## For AI agents

Follow the steps above. Rules:
- Run `plan` and show the user the resource list and cost before `up`.
- Never run `down` unless the user asks.
- If `check` reports expired AWS credentials, ask the user to run `aws sso login`.
- Every command prints `==>` headings, `✓`/`✗` results and `KEY=value` lines.
