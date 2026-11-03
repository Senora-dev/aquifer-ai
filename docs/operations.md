# Operating Aquifer

## Observability

All components write structured JSON log lines (one event per line) to CloudWatch Logs. They
contain identifiers and counts only — never document bodies, queries or credentials.

| Event | Where | Meaning |
| --- | --- | --- |
| `job_processed` | worker | One page: `fetched`, `indexed`, `skipped` (unchanged), `failed`, `deleted`, `duration_ms` |
| `document_failed` | worker | A document failed permanently (`document_id`, `error` type); the rest of the page continued |
| `job_rate_limited` | worker | Source rate limit; job re-enqueued with `delay_s` |
| `retry` | worker | A bounded in-process retry (`operation`, `attempt`, `delay_s`) |
| `reconciled` | worker | Deletions propagated for a scope (`deleted` count) |
| `repository_renamed` | worker | A configured repository now redirects to a new name — update `repos` |
| `files_capped`, `file_too_large`, `tree_truncated` | worker | File selection limits were hit (see config) |
| `semantic_extraction_failed` | worker | LLM extraction failed permanently for a document; recorded as `extraction_status=failed` and retried on next fetch |
| `discovery_complete`, `repo_purged` | discovery | Jobs enqueued; documents of de-listed repositories removed |
| `mcp_tool_call` | MCP (`aquifer.audit` logger) | Tool, principal, outcome (`allowed`/`denied`), result counts, latency |
| `mcp_auth_failed` | MCP | Missing or invalid bearer token (client IP, path) |

**Alarms** (created by the stack, no notification target attached — subscribe an SNS topic or
your paging tool to them):

- `DlqNotEmpty` — at least one ingestion job failed 5 times and is in the dead-letter queue.
- `WorkerErrors` — the worker Lambda reported errors in a 15-minute window.

ECS Container Insights is enabled for the MCP service (CPU, memory, task health).

Useful CloudWatch Logs Insights query:

```
fields @timestamp, event, repo, kind, indexed, skipped, failed, deleted
| filter event in ["job_processed", "document_failed", "job_rate_limited"]
| sort @timestamp desc
```

### Redriving the DLQ

After fixing the cause (token, model access, a bug), move messages back to the main queue with
SQS's *Start DLQ redrive* (console) or `aws sqs start-message-move-task --source-arn <dlq-arn>`.
Re-processing is safe: unchanged documents are skipped and writes are idempotent.

## Freshness semantics

- `indexed_at` — when Aquifer wrote the chunk. `fetched_at` — when the content was read.
- `updated_at` — the source's own modification time (issues/PRs). Files do not have one; their
  `revision` is the commit the file was read at, and `revision_committed_at` is that commit's
  date (not the date the file last changed).
- `list_sources` reports `last_indexed_at` per repository: the last time Aquifer wrote content,
  **not** a guarantee that the index matches GitHub right now.
- Discovery runs every `schedule_minutes` (15 by default). Expect changes to appear within one
  interval plus processing time, and up to ~60 seconds of OpenSearch Serverless refresh delay.

## Cost

Prices below are **us-east-1 list prices at the time of writing**, rounded; confirm with the
[AWS Pricing Calculator](https://calculator.aws/) for your region. "Idle" means deployed with no
ingestion or queries.

| Resource | Why it exists | Approx. idle cost / month |
| --- | --- | --- |
| OpenSearch Serverless collection | The index | **~$350** (minimum 2 OCUs × $0.24/OCU-hour); **~$175** with `aoss_standby_replicas=DISABLED` (1 OCU); plus $0.02/GB-month storage |
| NAT gateway (1) | HTTPS egress to GitHub, ECR, CloudWatch Logs | ~$33 + $0.045/GB processed |
| Interface VPC endpoints (Bedrock runtime, Secrets Manager, SQS, OpenSearch) | Keep AWS traffic off the internet | ~$15 each across 2 AZs (~$60) |
| Network Load Balancer (internal) | Stable MCP endpoint | ~$16 + LCU usage |
| Fargate task (0.5 vCPU, 1 GB) | MCP server, always on | ~$18 |
| Secrets Manager (2 secrets), CloudWatch alarms, logs | Credentials, alarms | ~$2 + log ingestion |
| Lambda, SQS, EventBridge, S3 | Ingestion | Near zero when idle |
| **Total idle** | | **≈ $480** (≈ $305 with standby replicas disabled) |

Usage-based costs:

- **Bedrock embeddings** — every indexed chunk once, every query once. Unchanged documents are
  skipped by content hash, so steady-state cost tracks the volume of *changes*.
- **Bedrock LLM extraction** — one call per new/changed README, issue, PR or ADR (input is
  truncated to `max_input_chars`, default 12,000). Terraform/compose/Backstage files use the free
  deterministic parsers instead (unless `llm_for_structured_files` is enabled). Set
  `-c semantic_index_enabled=false` to remove this cost entirely.
- **OpenSearch** scales OCUs above the minimum under indexing/search load.

`python scripts/benchmark_retrieval.py --bedrock --price-embed-per-1k … --price-llm-input-per-1k …`
reports token volumes for the benchmark corpus and, if you pass current prices, a dollar
estimate.

**Reducing cost:** use `aoss_standby_replicas=DISABLED` for non-production; delete the stack
when not in use (`cdk destroy`); keep `schedule_minutes` reasonable; disable LLM extraction if
deterministic extraction and search are enough. No component in the stack is optional at
runtime except LLM extraction.

## Upgrades

`cdk deploy` with a newer version rolls the Lambdas and the MCP service (ECS circuit breaker
rolls back a failing deployment). New index fields are added to an existing index
automatically. Changes that alter chunking or extraction bump `INDEX_FORMAT_VERSION`, which makes
each document re-index the next time it is fetched; to force a full rebuild, deploy with a new
`-c index=<name>` and delete the old index afterwards.

## Troubleshooting

| Symptom | Likely cause → fix |
| --- | --- |
| `cdk synth` prints `Aquifer configuration error: …` | Fix the named context value (see [deployment.md](./deployment.md)) |
| Docker errors during deploy | Start the Docker daemon; CDK bundles assets in containers |
| Discovery fails with `GitHub rejected the token (401)` | Populate `GitHubTokenSecretArn` with a valid token |
| `GitHub denied access (403)` / `SourceNotFoundError` for a repo | Token lacks access to that repository, or it was deleted/renamed — check `repository_renamed` logs and update `repos` |
| Worker errors with `AccessDeniedException` from Bedrock | Enable model access in the Bedrock console for both models in this region, or fix `extraction_model_id` |
| `ValidationException` / model not found | Model id not available in this region — run `scripts/preflight.py` |
| Messages accumulate in the DLQ | Read the worker logs for the job, fix, then redrive |
| MCP client gets `401` | Missing/incorrect `Authorization: Bearer` header; tokens rotate only after the service restarts |
| MCP client times out connecting | It is not on an allowed network: run it inside the VPC, or add its private CIDR via `mcp_allowed_cidrs` and route through VPN/peering |
| MCP task keeps restarting with `refusing to start: No MCP auth tokens configured` | The auth secret is empty or not JSON — restore `{"default": "<token>"}` |
| Search returns nothing for a repo | It is not in `repos`, ingestion has not run yet, or every job failed — check `list_sources` (`indexed_documents`) and the DLQ |
| `extraction_status: failed` on documents | LLM output could not be parsed; retried automatically next time the document is fetched |
