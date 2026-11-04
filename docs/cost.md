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

Upgrading to t4g.medium (4 GiB, for up to ~100,000 chunks) gives ≈ $33.

### Usage-based costs (outside the baseline)

| Cost | Driver | Unit price (eu-west-1) | Small-deployment estimate |
| --- | --- | --- | --- |
| Embeddings (Titan Text Embeddings v2) | each new or changed chunk, once; each query, once | $0.026 / million tokens | First ingest: ~15M tokens ≈ $0.40. Steady state: < $0.50 / month |
| LLM extraction, Claude Sonnet 4.6 (EU inference profile) | one call per new or changed README, issue, PR or ADR: ~2,000 input + ~400 output tokens | $3.30 / M input, $16.50 / M output | ≈ $0.013 per document. First 4,000 documents ≈ $53. 50 changes a day ≈ $20 / month; 200 a day ≈ $80 / month |
| LLM extraction, Claude Haiku 4.5 (EU) | same | $1.10 / M input, $5.50 / M output | ≈ $0.0044 per document: one third of Sonnet |
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
| Scale | ~25,000 chunks on t4g.small, ~100,000 on t4g.medium; vectors are held in memory | Millions of chunks |
| Query latency | In-process scan: a few ms per 10,000 chunks, plus a ~150 ms Bedrock query embedding | Network round trips to OpenSearch plus the same embedding |
| Ingestion throughput | 2 worker threads; CPU-burstable. A first sync of 5,000 documents takes roughly an hour, dominated by Bedrock latency | Many Lambda workers in parallel |
| Network exposure | No inbound ports by default (SSM tunnel); optional public HTTPS with bearer tokens | Internal NLB inside a private VPC |
| Operations | One machine: OS updates (`dnf upgrade` through SSM) | Managed services, more resources |

Choose the scale profile when you need multi-AZ availability, more than ~100,000 chunks, or
private-only networking with VPC endpoints.

## Measured usage

*To be filled in from a deployed small profile (see the deployment PR): instance memory and CPU
at idle and during ingestion, query latency, log volume, and the AWS bill once Cost Explorer
reports it.*
