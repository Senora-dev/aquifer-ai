# Deploying Aquifer

Aquifer runs in one of three ways. All three use the same application code, MCP tools and
security model, and none needs a third-party service.

| Profile | Baseline cost | Best for | Guide |
| --- | --- | --- | --- |
| **Small (default)**: one EC2 server, SQLite index, Terraform | **≈ $20 / month** (eu-west-1) | Teams up to ~50 repositories / ~25,000 chunks | [deploy/aws/README.md](../deploy/aws/README.md) |
| **Existing infrastructure**: one container + one volume | $0 extra infrastructure | Running on a VM, Docker host or Kubernetes you already have | [below](#existing-infrastructure-docker) |
| **Scale**: CDK stack on OpenSearch Serverless, Lambda, Fargate | ≈ $320–$504 / month | Multi-AZ availability, large indexes, private-only networking | [below](#scale-profile-cdk-opensearch-serverless) |

Model usage (Bedrock embeddings and extraction) is billed per use in every profile. Itemized
costs, measurements and tradeoffs are in [cost.md](./cost.md).

## Small profile (default)

```bash
cd deploy/aws
./aquifer-aws check
AQUIFER_REPOS='["your-org/service-a"]' ./aquifer-aws configure
./aquifer-aws plan && ./aquifer-aws up
./aquifer-aws connect
```

Full guide: [deploy/aws/README.md](../deploy/aws/README.md).

## Existing infrastructure (Docker)

`Dockerfile.server` runs everything in one process: the MCP server, the scheduler and the
ingestion workers. The index, job queue and state live on the `/data` volume.

```bash
cd deploy/docker
cp aquifer.env.example aquifer.env               # repositories, region, models
mkdir -p secrets && gh auth token > secrets/github-token && chmod 600 secrets/github-token
docker compose up -d
docker compose exec aquifer aquifer status
python3 -c 'import json; print(json.load(open("data/mcp-tokens.json"))["default"])'   # MCP token
```

The container needs:
- **A persistent volume** at `/data`. Back it up, or accept a full re-sync after losing it.
- **AWS credentials for Bedrock**: an instance or task role, or `AWS_*` variables. To run
  without AWS, set `AQUIFER_EMBEDDING__PROVIDER=hashing` and
  `AQUIFER_SEMANTIC_INDEX__ENABLED=false`, which gives lexical search and deterministic
  extraction only.
- **TLS** in front of port 8080 before exposing it beyond localhost. Requests always need a
  bearer token.

Credentials are passed as references:
- `AQUIFER_GITHUB__TOKEN_SECRET=file:/run/secrets/github-token`
- `AQUIFER_MCP__AUTH_TOKENS_REF=file:/data/mcp-tokens.json`

`ssm:` and `env:` references and Secrets Manager ids also work.

Operate it with the `aquifer` CLI inside the container (`docker compose exec aquifer aquifer …`):

| Command | What it does |
| --- | --- |
| `status` | Show the queue, last sync, index size and dead letters |
| `sync [--full]` | Sync now instead of waiting for the schedule |
| `retry-failed` | Re-run extraction that exhausted its retries |
| `dead-letters [--requeue all]` | List jobs that kept failing, or requeue them |

## Scale profile (CDK, OpenSearch Serverless)

This section deploys the CDK stack in `infrastructure/`. It costs ~$320–$504 a month before any
traffic, mostly for the OpenSearch Serverless minimum capacity (see [cost.md](./cost.md)). Use
it when you need multi-AZ availability or an index well beyond what one server holds. Every
step is validated by `cdk synth` tests and the read-only preflight script; review the
synthesized template (`cdk synth`) before deploying into a production account.

### Prerequisites

| Requirement | Version / notes |
| --- | --- |
| Python | 3.11 or 3.12 (the Lambda and MCP runtimes use 3.12) |
| Node.js | An LTS release (20, 22 or 24) for the AWS CDK CLI |
| AWS CDK | CLI v2 (`npm install -g aws-cdk` or `npx cdk`); `aws-cdk-lib` ≥ 2.140 < 3 |
| Docker | Running; CDK bundles the Lambda code and builds the MCP image locally |
| AWS credentials | Permission to create VPC, OpenSearch Serverless, Lambda, ECS/Fargate, ELB, SQS, S3, Secrets Manager, IAM, CloudWatch, EventBridge |
| Bedrock model access | Enabled in the target region for the embedding model and the extraction model |
| GitHub token | Read-only access to the repositories you configure (see below) |

### 1. Install and check

```bash
git clone https://github.com/Senora-dev/aquifer-ai.git && cd aquifer-ai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,cdk]"

export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=eu-west-1          # your region

GITHUB_TOKEN=<token> python scripts/preflight.py \
  --region "$CDK_DEFAULT_REGION" --repos your-org/service-a,your-org/infra
```

The preflight script is read-only. It verifies the toolchain, the deploy configuration, AWS
credentials, that OpenSearch Serverless and the Bedrock VPC endpoint service exist in the
region, that both Bedrock models (or inference profiles) exist and are not deprecated, and that
the token can read every repository. Add `--invoke-test` to make one tiny embedding call that
proves model access is enabled (costs a fraction of a cent).

### 2. Choose models and region

| Model | Default | Context key |
| --- | --- | --- |
| Embeddings | `amazon.titan-embed-text-v2:0` (1024 dims) | `model_id` |
| Extraction | `anthropic.claude-3-5-sonnet-20241022-v2:0` | `extraction_model_id` |

Bedrock model availability differs by region and older models are retired over time — **run
the preflight check and override `extraction_model_id` with a currently available Claude model
in your region**. Cross-region inference profile ids (e.g. `us.anthropic.…`) are supported; IAM
is scoped to that profile and its underlying model, and Bedrock may serve requests from other
regions in the profile's geography. To run without an LLM at all, pass
`-c semantic_index_enabled=false` (deterministic extraction still runs).

Changing the embedding model or dimensions after data is indexed requires a new index
(`-c index=<new-name>`).

#### Bedrock model access

New AWS accounts need up to three one-time steps before Claude models can be invoked. The
symptoms below appear as `extraction_error` codes in the worker logs and as
`extraction: {"retry_pending": …}` in `list_sources`; documents retry automatically once fixed.

1. **Model availability** — the model or inference profile must exist in the region and be
   offered to your account (`bedrock_model_not_found` / `bedrock_model_not_available`). The newest
   models may require contacting AWS.
2. **Anthropic use case form** — Bedrock console → Model catalog → an Anthropic model → *Submit
   use case details* (`bedrock_use_case_form_required`). Takes effect within ~15 minutes.
3. **AWS Marketplace subscription** — the first invocation in the account must come from a
   principal allowed `aws-marketplace:Subscribe` / `ViewSubscriptions`, e.g. an administrator
   sending one message in the Bedrock console Playground
   (`bedrock_marketplace_subscription_required`). Aquifer's roles intentionally do not get
   Marketplace permissions.

`scripts/preflight.py --invoke-test` and `deploy/aws/aquifer-aws check` make one tiny call
and print which step is missing.

### 3. GitHub token permissions

Create a **fine-grained personal access token** (or GitHub App installation token) limited to
the configured repositories with **read-only** access to:

- Contents (README and files)
- Issues
- Pull requests
- Metadata (granted automatically)

No write permissions are needed. The token is stored in Secrets Manager and read only by the
ingestion Lambdas.

### 4. Configure and deploy

```bash
cd infrastructure
cdk bootstrap                                    # once per account/region
cdk synth -c repos='["your-org/service-a","your-org/infra"]'   # optional review
cdk deploy -c repos='["your-org/service-a","your-org/infra"]'
```

Configuration is validated before synthesis; invalid values stop with a message explaining
what to change.

| Context key | Default | Purpose |
| --- | --- | --- |
| `repos` | — (required) | JSON array or comma list of exact `owner/repo` names. One trust domain. |
| `index` | `aquifer-context` | OpenSearch index name |
| `model_id` | Titan v2 | Embedding model |
| `extraction_model_id` | see above | LLM for extraction from prose |
| `semantic_index_enabled` | `true` | `false` disables LLM extraction (no Claude calls) |
| `aoss_standby_replicas` | `ENABLED` | `DISABLED` halves the OpenSearch idle floor (dev/test only; no redundancy) |
| `mcp_allowed_cidrs` | — (VPC CIDR only) | Extra private CIDRs (VPN, peered VPC) allowed to reach the MCP listener, in addition to the VPC CIDR. `0.0.0.0/0` is rejected. |
| `mcp_certificate_arn` | — | ACM certificate ARN → TLS listener on 443 instead of TCP/80 |
| `worker_max_concurrency` | `5` | Upper bound on parallel ingestion workers (GitHub/Bedrock rate) |
| `schedule_minutes` | `15` | Discovery interval |
| `log_retention_days` | `30` | CloudWatch Logs retention |

Repository-level file selection and other runtime settings are `AQUIFER_*` environment
variables (see `src/aquifer/core/config.py`), e.g. `AQUIFER_GITHUB__FILE_GLOBS`.

#### Stack outputs

Outputs contain identifiers only — never secret values:

| Output | Meaning |
| --- | --- |
| `McpUrl` | `http(s)://<internal-nlb>/mcp` — the MCP endpoint |
| `McpEndpoint` | Internal NLB DNS name |
| `McpAuthSecretArn` | Secret holding the MCP bearer tokens |
| `GitHubTokenSecretArn` | Secret to populate with the GitHub token |
| `DiscoveryFunctionName` | Invoke to start ingestion immediately |
| `IngestQueueUrl`, `DeadLetterQueueUrl` | Ingestion queue and DLQ |
| `CollectionEndpoint` | OpenSearch Serverless endpoint (VPC-only) |

### 5. Provide the GitHub token

```bash
aws secretsmanager put-secret-value --secret-id <GitHubTokenSecretArn> \
  --secret-string "$GITHUB_TOKEN"
```

Avoid putting tokens on the command line on shared machines; `--secret-string file://token.txt`
reads it from a file.

### 6. Start ingestion

Discovery runs every `schedule_minutes`. To start now:

```bash
aws lambda invoke --function-name <DiscoveryFunctionName> /dev/stdout
```

Discovery first validates the GitHub token; a rejected token fails the run with a clear error
and does not advance the watermark. Progress is visible in the worker's logs (`job_processed`
events) and in the queue depth. See [operations.md](./operations.md).

### 7. Get an MCP token and connect an agent

The stack generates one token named `default`:

```bash
aws secretsmanager get-secret-value --secret-id <McpAuthSecretArn> \
  --query SecretString --output text
# {"default": "<48 characters>"}
```

To issue one token per agent or team (the name is recorded as the principal in audit logs),
store a JSON object with more entries, e.g. `{"ci-agent": "…", "platform-team": "…"}` (each
token ≥ 32 characters), then restart the service (`aws ecs update-service --force-new-deployment
…`) so the task reads the new value. Removing an entry revokes that token after the restart.

The endpoint is reachable only from inside the VPC or from `mcp_allowed_cidrs`. Connect from a
machine on that network: [agent-integration.md](./agent-integration.md).

### 8. Uninstall

```bash
cd infrastructure
cdk destroy
```

This removes every resource in the stack, including the OpenSearch collection and its indexed
data, the state bucket and log groups. Secrets Manager schedules the two secrets for deletion
(default 30-day recovery window); delete them immediately with
`aws secretsmanager delete-secret --force-delete-without-recovery --secret-id <arn>` if you
prefer. The `cdk bootstrap` resources (CDKToolkit stack and its asset bucket/repository) are
shared by all CDK apps in the account and are not removed.

---

Operations, costs and troubleshooting: [operations.md](./operations.md). Security model:
[security.md](./security.md).
