# Product hardening baseline (pre `feature/engineering-context-v1`)

This document records the state of `main` at commit `483d2d6` **before** the Engineering
Context v1 changes. Every statement below was checked against the code (not only the docs).
Items marked *docs-verified* were checked against current AWS documentation because they cannot be
exercised without a live deployment.

## 1. Baseline results

| Check | Command | Result |
| --- | --- | --- |
| Unit tests | `pytest -q` | **87 passed** (0.85s) |
| Lint | `ruff check src/aquifer tests scripts` | **2 errors** (both in `tests/test_chunking.py`: E501, B905) |
| Format | `ruff format --check` | 14 files would be reformatted (format is not an enforced gate) |
| MCP server import | `python -c "from aquifer.mcp.server import create_server"` with a fresh `pip install '.[mcp]'` | **Fails** — `mcp>=1.2` resolves to `mcp` 2.3.0, which removed `mcp.server.fastmcp` |
| CDK synth | — | Not covered by any test; `aws-cdk-lib` is not in the dev extra |
| Retrieval benchmark | — | **None exists.** The only harness (`scripts/eval_semantic_index.py`) measures LLM extraction recall and requires Bedrock; it was not runnable offline |
| Live AWS | — | Not run (no deployment authorized) |

## 2. Capabilities that work (verified by code + tests)

- Domain model (`Document` / `Chunk` / `SearchResult`), deterministic document ids.
- GitHub connector: README, issues, PRs; page-at-a-time pagination through SQS successor jobs.
- Token-approximate chunking with overlap and overlap-aware document reassembly (`get_document`).
- Interceptor seam (`before/after_ingest`, `before/after_query`, `authorize`, deny-overrides).
- LLM semantic indexing via a per-source prompt registry; best-effort (failures logged).
- Five MCP tools implemented as framework-free methods in `aquifer/mcp/tools.py`, unit tested
  with fakes.
- CDK stack: VPC, NAT, Bedrock/Secrets/SQS interface endpoints, S3 gateway endpoint, AOSS
  collection with VPC-only network policy, Lambdas, SQS + DLQ, Fargate behind an internal NLB.

## 3. Broken or partially implemented

### P0 — would prevent a working deployment

| # | Defect | Evidence |
| --- | --- | --- |
| B1 | **MCP server cannot start.** `mcp>=1.2` installs `mcp` 2.x in `Dockerfile.mcp`; `mcp.server.fastmcp` no longer exists. Streamable HTTP (used by `server.main`) also needs `mcp>=1.8`. | Reproduced locally. |
| B2 | **`delete_by_document` uses `_delete_by_query`, which OpenSearch Serverless does not support.** The pipeline calls it before every upsert, so every document write fails. | *docs-verified*: "Supported operations and plugins in Amazon OpenSearch Serverless" lists no `_delete_by_query`. |
| B3 | **Bulk upsert sets a custom `_id`.** Classic AOSS vector collections reject custom document ids ("Document ID is not supported in create/index operation request"); only NextGen collections accept them. | *docs-verified*: AWS vector-search collection guide; AWS re:Post. |
| B4 | **Semantic-index region is never set by the stack.** `AQUIFER_SEMANTIC_INDEX__REGION` defaults to `us-east-1`; a stack in any other region sends document text to Bedrock in another region over the NAT (or fails). | `aquifer_stack.py` `base_env`. |

### P0/P1 — correctness and reliability

| # | Defect |
| --- | --- |
| B5 | `read_watermark` swallows **every** S3 error (throttling, AccessDenied) as "never ran", silently triggering a full re-backfill. |
| B6 | README fetch swallows **every** error (401, 403 rate-limit, 5xx) as "no README". A removed README is never removed from the index. |
| B7 | No retry/backoff for GitHub. Rate-limit responses (reset up to 1h) exhaust 5 SQS receives × 6 min and land in the DLQ; the rest of that pagination chain is lost. |
| B8 | One malformed item or one failed document fails the whole page; after 5 receives the page goes to the DLQ and **all later pages of that repo/kind are never enqueued**. |
| B9 | Issues are paged `sort=updated&direction=asc`: an item updated mid-pagination shifts the page window left and an unmodified-but-in-window item is skipped permanently. |
| B10 | Pulls have no server-side `since`; the connector walks **every** PR page on every 15-minute run and filters client-side (GitHub rate limit + Lambda cost). |
| B11 | READMEs are re-extracted (LLM) and re-embedded every run whether or not they changed. Repeated delivery of any job repeats all Bedrock work. |
| B12 | A single oversized whitespace-free token (minified/base64 blob) becomes one chunk and exceeds the Titan input limit → page fails → DLQ. |
| B13 | Delete-then-insert leaves a window in which a document has no chunks, and loses it entirely if the upsert fails. |
| B14 | No deletion propagation of any kind: repositories removed from the allowlist stay searchable forever; removed READMEs stay. |
| B15 | Renamed repositories: `httpx` does not follow GitHub's 301 by default; the job errors. |
| B16 | Semantic-indexing failures (e.g. Bedrock throttling) index the document **without** metadata and nothing records that, so it is never retried. |
| B17 | Bedrock clients use default retry config; no explicit timeouts. |
| B18 | SQS visibility timeout (6 min) is barely above the 5-min worker timeout (AWS recommends ≥ 6× for Lambda event sources). No DLQ alarm. |
| B19 | GitHub token cached for the container lifetime (rotation not picked up). An unpopulated secret (CDK generates a random value) causes opaque 401s on every job. |

### Retrieval quality

| # | Gap |
| --- | --- |
| R1 | No provenance: results carry a URL only — no path, revision, line range, source timestamps, or indexed time. `get_document` returns no timestamps at all. |
| R2 | `Relationship` has no source entity; "X depends_on Y" extracted from a multi-service document cannot express *which* entity depends on Y (direction is lost). |
| R3 | No distinction between explicit references and LLM-inferred relationships; LLM output is not checked against the text. |
| R4 | Entity/target matching is exact-case keyword matching (`Billing-API` ≠ `billing-api`); no `type:name` normalization. |
| R5 | `list_entities` with no filter samples `k` arbitrary chunks (match_all) — it is not an inventory. |
| R6 | `find_related` returns all relationships of a matched document, unordered, with no evidence. |
| R7 | `get_document` re-joins words with single spaces — all line structure (YAML, code, Markdown) is lost; `size: 1000` truncates silently; vectors are returned in `_source`. |
| R8 | No configuration/IaC ingestion; deterministic extraction of Terraform / YAML references is impossible because those files are never ingested. |
| R9 | `k` is unbounded and filter values are not type-checked. |

### Security

| # | Gap |
| --- | --- |
| S1 | **The MCP endpoint has no authentication.** Anything that can reach the NLB can read the whole lake. |
| S2 | The server never passes a principal to the tools, so `authorize(principal=…)` always sees `None`. |
| S3 | `find_related`, `list_entities` and `get_document` never call `before_query`/`after_query`, so a repo-scoping interceptor (the exact example in `docs/contributing.md`) is bypassed by three of five tools — a cross-source leak path. |
| S4 | No audit events. |
| S5 | IAM: `bedrock:InvokeModel` on `*`; AOSS data policy grants `aoss:*` to every role; discovery Lambda is granted AOSS data access it does not use. |
| S6 | One shared security group for Lambdas, Fargate and all endpoints, with unrestricted egress. |
| S7 | Retrieved text is returned without any marking that it is untrusted source data; document text is interpolated into the extraction prompt without delimiting. |
| S8 | Docs say the MCP endpoint is reachable over VPC peering / VPN, but the security group only admits the VPC CIDR. |

## 4. Test coverage at baseline

Covered: chunking, models, hooks, connector normalization/pagination (with a fake HTTP client),
pipeline ordering, worker routing, AOSS adapter request shapes (with a fake client), MCP tool
logic, semantic prompts/engine/interceptor, eval-harness scoring.

Not covered: the MCP server wiring, any authentication, the discovery handler, watermarks,
error/retry paths, deletion, CDK synthesis, end-to-end retrieval quality.

## 5. Security assumptions at baseline

- Network isolation (internal NLB + VPC-only AOSS) was the only access control.
- All configured repositories are implicitly one trust domain (nothing enforces it).
- "Zero data egress" holds only if the deployment region is `us-east-1` (see B4) and only for
  AWS-service traffic; the NAT permits arbitrary egress.

## 6. Planned modifications and rationale

1. **Fix P0 deployment blockers** (B1–B4): pin `mcp<2`; rewrite AOSS writes to use only
   documented operations (bulk index without `_id`; stale-chunk cleanup by search + bulk delete
   by `_id`); pass the extraction model/region from the stack.
2. **Make ingestion idempotent and convergent** (B5–B19): content-hash generations, skip
   unchanged documents, write-new-then-delete-stale, per-document error isolation, bounded
   retries with backoff, rate-limit-aware re-enqueue, deletion reconciliation, structured logs.
3. **Evidence-backed retrieval** (R1–R9): optional provenance + freshness fields, relationship
   source/origin/evidence, deterministic extraction for Terraform, docker-compose and Backstage
   descriptors (ingested via the existing GitHub connector, not a new connector), grounding check
   for LLM relationships, conservative entity keys, aggregation-based inventory.
4. **Security minimum** (S1–S8): bearer-token authentication at the MCP HTTP boundary (secret in
   Secrets Manager), principal propagation, all tools routed through the interceptor seam with a
   core repo-allowlist scope, audit log events, least-privilege IAM, split security groups.
5. **Benchmark**: an offline, deterministic retrieval benchmark that reuses `tests/eval_data` and
   the existing scoring style, plus an optional Bedrock mode.
6. **Docs**: README positioning, deployment/cost/operations, agent integration, demo.

Architecture that already works (interfaces, factory, interceptor seam, single CDK stack,
SQS fan-out) is kept as is.

## 7. Final results (end of `feature/engineering-context-v1`)

| Check | Baseline | Final |
| --- | --- | --- |
| `pytest -q` | 87 passed | 196 passed (incl. 17 CDK synth tests, real-HTTP MCP auth tests) |
| `ruff check src tests scripts infrastructure` | 2 errors | 0 |
| MCP server import / container start | fails (`mcp` 2.x) | builds; refuses to start without tokens; serves MCP with a token (verified with `docker run --read-only`) |
| CDK synth | untested | tested (security properties asserted) |
| Retrieval benchmark (offline) | not runnable | 43 passed / 0 failed / 2 skipped (LLM-only) of 45 |
| `pip-audit` (runtime deps) | — | no known vulnerabilities (only the local venv's `pip` tool was flagged; CI upgrades it) |
| Live AWS deployment | not run | not run (no deployment authorized) |

Defects B1–B19, R1–R9 and S1–S8 above are addressed by this release except where
`docs/security.md` and the README "Current limitations" state otherwise (notably: deleted
issues/PRs are not detected; one trust domain per deployment; plain TCP listener unless a
certificate is configured). Benchmark details: `docs/benchmark.md`.
