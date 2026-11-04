# Cost and sizing

Aquifer's default deployment is a **single small server**. Its baseline infrastructure cost is
about **$20 a month** in eu-west-1 (Ireland). That baseline excludes model inference and
embeddings, which are billed per use and listed separately below. A larger, highly available
profile on OpenSearch Serverless is still available. It costs about **$320–$500 a month** before
any traffic.

Every price below is an **AWS list price for eu-west-1, read from the AWS Pricing API on
2026-11-04**. Figures marked *calculated* are estimates. Figures marked *measured* come from
running deployments; see [Measured usage](#measured-usage). A month is 730 hours.

## What a "small deployment" is

These are the limits the default profile is sized and tested for:

| Dimension | Small deployment |
| --- | --- |
| Repositories / connected sources | up to 50 repositories, plus up to 5 MCP sources |
| Indexed documents | up to ~5,000 documents (~25,000 chunks) |
| Changed documents | up to ~200 a day |
| Queries | up to ~5,000 tool calls a day, ~10 concurrent agents |
| Availability | one server in one Availability Zone; see [Tradeoffs](#tradeoffs) |

## Why the previous default cost ~$500 a month

The previous default was the CDK stack in `infrastructure/`. Recomputing its idle cost with
eu-west-1 prices confirms the order of magnitude in the old docs. Most of the cost is fixed
infrastructure that runs whether or not anyone uses Aquifer:

| Resource (previous default) | Unit price | Idle / month (*calculated*) |
| --- | --- | --- |
| OpenSearch Serverless, 2 OCUs minimum (standby replicas on) | $0.251 / OCU-hour | $366.46 |
| …or 1 OCU with `aoss_standby_replicas=DISABLED` | | $183.23 |
| NAT gateway | $0.048 / hour | $35.04 |
| NAT gateway public IPv4 address (missing from the old docs) | $0.005 / hour | $3.65 |
| 4 interface endpoints (Bedrock, Secrets Manager, SQS, OpenSearch) × 2 AZs | $0.011 / hour each | $64.24 |
| Internal Network Load Balancer | $0.0252 / hour | $18.40 |
| Fargate MCP task, 0.5 vCPU / 1 GB, ARM | $0.03238 / vCPU-h, $0.00356 / GB-h | $14.42 |
| Secrets Manager, 2 secrets | $0.40 each | $0.80 |
| **Total** | | **≈ $504** (≈ $320 with standby replicas off) |

The old docs used us-east-1 prices ($0.24/OCU-hour) and arrived at ~$480. *Measured:* the
AWS bill for the Terraform evaluation environment (standby replicas off) shows OpenSearch
Serverless charged at $0.251 per OCU-hour, which matches the list price.

Removing the cost requires changing the architecture, not shrinking it. OpenSearch
Serverless alone costs ~7× the target budget at its minimum size, and private networking (NAT +
endpoints + NLB) alone costs ~$120.

## Options compared

| Option | Baseline / month | Fit |
| --- | --- | --- |
| **Single small server: SQLite index on EBS, in-process vector search** (chosen) | **≈ $20** | All features with no AWS-specific services; the same container runs on existing infrastructure |
| Existing customer infrastructure (any Docker host or VM) | $0 extra infrastructure | Same container as above; you provide a persistent volume and TLS |
| Lightsail 2 GB instance | ≈ $12 | Cheapest, but Bedrock would need long-lived access keys instead of an instance role |
| Lambda + S3 Vectors, no VPC | ≈ $2–5 | Lowest idle cost. S3 Vectors has no aggregations and a capped top-k, so `find_related`, `list_entities`, the retry sweep and deletion reconciliation would need a second store. More services to operate |
| EC2 + RDS PostgreSQL with pgvector | ≈ $35–45 | Fits the budget but adds a managed database to operate and pay for |
| OpenSearch Serverless (previous default) | ≈ $320–504 | Large scale and high availability; kept as the optional *scale profile* |

The chosen design keeps the smallest number of moving parts: one process, one disk, no load
balancer, no NAT, no VPC endpoints and no second database.

## Chosen architecture (small profile)

```
 agent ──HTTPS or SSM tunnel──▶ EC2 t4g.small (public subnet, no inbound ports by default)
                                 └─ aquifer serve  (one process)
                                     ├─ MCP server (streamable HTTP, bearer tokens)
                                     ├─ scheduler + job queue (SQLite: retries, backoff, dead letters)
                                     ├─ ingestion workers (GitHub connector, extraction, embedding)
                                     └─ index (SQLite on an encrypted EBS volume; vectors in memory)
                                 ├─ instance role → Bedrock (embeddings, extraction)
                                 ├─ SSM Parameter Store (GitHub token, MCP tokens; SecureString)
                                 ├─ CloudWatch Logs (30-day retention)
                                 └─ daily EBS snapshots of the data volume (7 kept)
```

- The data volume is separate from the root volume, so the server can be replaced without losing
  the index.
- The index is derived data. If the volume and its snapshots were both lost, a full re-sync
  rebuilds it from the sources; that re-sync costs embeddings and extraction (see below).
- Access to the MCP endpoint is private by default, through an SSM port-forward to your laptop;
  this needs no open ports and costs nothing. Optionally, a domain name gives you public HTTPS:
  Caddy obtains a Let's Encrypt certificate, ports 80/443 are opened, and requests still need
  bearer tokens.
- The instance reaches GitHub and Bedrock over its public IPv4 address, so it needs no NAT
  gateway and no VPC endpoints.

### Itemized baseline (eu-west-1, *calculated*)

| Component | Configuration | Unit price | Per month |
| --- | --- | --- | --- |
| Compute | EC2 t4g.small (2 vCPU burstable, 2 GiB), on-demand, CPU credits `standard` | $0.0184 / hour | $13.43 |
| Root volume | gp3, 8 GiB, encrypted | $0.088 / GB-month | $0.70 |
| Data volume | gp3, 10 GiB, encrypted (SQLite index, queue, state) | $0.088 / GB-month | $0.88 |
| Public IPv4 | 1 Elastic IP (stable address; needed for egress without NAT) | $0.005 / hour | $3.65 |
| Backups | daily EBS snapshots, 7 kept (incremental; ≤ 10 GiB) | $0.05 / GB-month | ≤ $0.50 |
| Logs | CloudWatch Logs, ~0.5 GB ingested, 30-day retention | $0.57 / GB + $0.03 / GB-month | ≈ $0.30 |
| Secrets | SSM Parameter Store standard SecureString with the AWS-managed key | free | $0.00 |
| Artifact bucket | S3, one wheel (<5 MB) | $0.023 / GB-month | < $0.01 |
| Networking | VPC, subnet, internet gateway, security group | free | $0.00 |
| Load balancer, NAT, VPC endpoints | not used | — | $0.00 |
| Data transfer out | MCP responses; well under the 100 GB/month free tier | $0.09 / GB above free tier | $0.00 |
| Third-party fees | none (Let's Encrypt is free; Caddy is open source) | — | $0.00 |
| **Baseline total** | | | **≈ $19.50** |

Cheaper variants:
- t4g.micro (1 GiB) gives ≈ $12.80 a month, for up to ~5,000 chunks.
- A 1-year Savings Plan on t4g.small gives ≈ $16.

Upgrading to t4g.medium (4 GiB, ≈ $33 a month) leaves room for ~100,000 chunks. That figure is
extrapolated from the measured memory use (~18 MiB per 1,000 chunks) and was not tested.

### Usage-based costs (outside the baseline)

| Cost | Driver | Unit price (eu-west-1) | Small-deployment estimate |
| --- | --- | --- | --- |
| Embeddings (Titan Text Embeddings v2) | each new or changed chunk, once; each query, once | $0.026 / million tokens | First ingest: ~15M tokens ≈ $0.40. Steady state: < $0.50 / month |
| LLM extraction, Claude Sonnet 4.6 (EU inference profile) | one call per new or changed README, issue, PR or ADR: ~2,000 input + ~400 output tokens | $3.30 / M input, $16.50 / M output | ≈ $0.013 per document. First 4,000 documents ≈ $53. 50 changes a day ≈ $20 / month; 200 a day ≈ $80 / month |
| LLM extraction, Claude Haiku 4.5 (EU), **the default chosen by `./aquifer-aws configure`** | same | $1.10 / M input, $5.50 / M output | ≈ $0.0044 per document: one third of Sonnet. First 4,000 documents ≈ $18; 50 changes a day ≈ $6.60 / month; 200 a day ≈ $26 / month |
| LLM extraction disabled | — | — | $0. Deterministic Terraform, compose and Backstage extraction and search still work |
| Burst CPU | not possible with `standard` credits; the instance slows down instead | — | $0 |
| GitHub API | within the free REST rate limit (5,000 requests/hour per token) | free | $0 |

Unchanged documents are skipped by content hash, so steady-state model cost tracks the volume
of *changes*, not the size of the corpus. Configuration files never call the LLM.

## Tradeoffs

| Area | Small profile | Scale profile (CDK, OpenSearch Serverless) |
| --- | --- | --- |
| Availability | One instance in one AZ. EC2 automatic recovery restarts it on host failure (minutes). No redundancy during an AZ outage | Multi-AZ OpenSearch, Fargate behind an NLB |
| Recovery | RPO ≤ 24 h from snapshots, or rebuild from sources. RTO: minutes (reboot) to ~30 min (restore a snapshot) | Managed by OpenSearch |
| Scale | Measured on t4g.small: 25,000 chunks comfortably, 50,000 workable; vectors are held in memory | Millions of chunks |
| Query latency | Measured on t4g.small at baseline CPU: 4 ms search at 5,000 chunks, 22 ms at 25,000, ~160 ms at 50,000; plus ~150 ms for the Bedrock query embedding | Network round trips to OpenSearch plus the same embedding |
| Ingestion throughput | 2 worker threads. Bound by Bedrock latency, not CPU; a new instance starts with no CPU credits, so the first large sync runs at baseline CPU | Many Lambda workers in parallel |
| Cost ceiling | `standard` CPU credits: the instance slows down rather than bill surplus credits, so the bill cannot exceed the on-demand price | Scales (and bills) with load |
| Network exposure | No inbound ports by default (SSM tunnel); optional public HTTPS with bearer tokens | Internal NLB inside a private VPC |
| Operations | One machine: OS updates (`dnf upgrade` through SSM) | Managed services, more resources |

Choose the scale profile when you need multi-AZ availability, well over ~50,000 chunks, or
private-only networking with VPC endpoints.

## Measured usage

The small profile was deployed to eu-west-1 on 2026-11-04 (23 resources) and then destroyed.
It ingested three private GitHub repositories of synthetic test data, with LLM extraction by
Claude Haiku 4.5.

**Billing.** Cost Explorer had not yet reported charges for the test window when the
environment was destroyed; it typically lags by up to 24 hours. The baseline above is therefore
*calculated* from list prices for the resources Terraform created. These are fixed hourly or
per-GB charges with no hidden components:
- the instance, volumes and IPv4 address are billed per hour
- snapshots and logs are billed per GB stored

**Resource use, *measured* on t4g.small:**

| What | Result |
| --- | --- |
| Memory at idle (whole instance / Aquifer process), 17 documents indexed | 338 MiB of 1,840 MiB / 105 MiB |
| CPU at idle | < 1% (load 0.1) |
| Synthetic index, 5,000 chunks × 1,024 dims (~3 KB text each) | peak 169 MiB; 44 MiB on disk; search p50 4.1 ms, p95 4.3 ms; repo-filtered 7 ms; cold load 0.3 s |
| Synthetic index, 25,000 chunks | peak 478 MiB; 203 MiB on disk; search p50 21.6 ms, p95 22.3 ms; repo-filtered 33 ms; `find_related` filter 37 ms; cold load 1.5 s |
| Synthetic index, 50,000 chunks | peak 896 MiB; 401 MiB on disk; search p50 159 ms; filtered 167–225 ms; cold load 8.7 s |
| CPU credit balance during these runs | 0, so the instance ran at its 20% baseline; the latencies above are worst case |
| MCP tool latency from a laptop through the SSM tunnel (25 e2e calls) | p50 101 ms, max 301 ms |
| Logs | ~5.9 KB per incremental sync of 3 repositories, i.e. ~17 MB/month at a 15-minute interval |
| Data volume after ingestion | 2.8 MB used of 10 GiB |

**Functional checks on the deployed server:**
- 25/25 end-to-end MCP checks, including LLM relationship status
- 23/23 update and deletion checks with real GitHub commits
- a full re-sync re-indexed 0 documents
- the index survived an instance replacement and a reboot

**Model cost evidence.** With `--bedrock`, the retrieval benchmark (20 documents, 11 LLM calls)
used ~1,400 embedding tokens and ~660 tokens of document text per run. The fixed extraction
prompt adds ~1,400 tokens per call. That is the basis for the ~2,000-input-token per-document
estimate above.
