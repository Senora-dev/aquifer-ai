# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — MCP Integrator

### Added
- **MCP Integrator** (`aquifer sources …`, docs/integrator.md): connects to external MCP
  servers and imports their content into the shared pipeline on a schedule.
  - Providers: preconfigured ones (GitHub verified end to end; Atlassian, Linear, Notion,
    Sentry, Slack with auth metadata verified) or any URL.
  - Auth: bearer tokens, or OAuth 2.1 with PKCE and dynamic registration. Tokens are refreshed
    unattended; failures surface as `needs_reauth`.
  - Plans: from provider templates, or derived deterministically from one sample call
    (`propose`). `preview` shows sample documents, coverage and who can read the content.
  - Runs: page-by-page jobs with checkpoints, rate-limit postponement, retries and dead
    letters.
  - Deletions: only from complete listings, confirmed per item by the source.
  - Provenance in `get_document` (`imported_via`); sources and their coverage in
    `list_sources`.
- Safety: read-only gate (annotations and tool names), approved-tools enforcement at call
  time, private-network guard (also for OAuth endpoints), sanitized remote text, 5 MB result
  limit.
- `aquifer-aws`: `sources`, `source-token`, `authorize`, `test-integrator`. The seed script
  enables discussions.
- Document kinds `ticket`, `page`, `release`, `message`, `record`; source type `mcp`; extraction
  prompts for release notes, tickets and pages.

### Fixed
- OAuth requested the scopes the server advertised (Linear: `read write`) instead of the
  configured least-privilege scope (`read`). Registration and authorization now keep the
  configured scope.
- A paused source's (or a disabled scope's) documents could have been readable when no GitHub
  repositories were configured; the scope guard now denies everything when sources are managed
  and none is enabled.

## [Unreleased] — Affordable single-server deployment

The default deployment now costs **about $20 a month** (eu-west-1, before model usage) instead
of ~$500. The OpenSearch Serverless stack remains as the optional scale profile.

### Added
- `aquifer serve`: one process runs the MCP server, a scheduler and ingestion workers. Its
  durable SQLite job queue offers leases (interrupted runs are redelivered), exponential
  backoff, dead letters, deduplication of periodic jobs and rate-limit postponement.
- `aquifer` CLI: `status`, `sync [--full]`, `retry-failed`, `dead-letters`, `init`.
- SQLite index (`vector_store.backend=sqlite`): the same records and semantics as the other
  stores (identical benchmark results), transactional replacement, numpy top-k search,
  multi-process reload.
- Secret references: `ssm:`, `file:` and `env:` in addition to Secrets Manager ids.
- `deploy/aws`: Terraform for one t4g.small server with an encrypted data volume, daily
  snapshots, CloudWatch Logs, SSM parameters and optional HTTPS (Caddy). `aquifer-aws` drives
  check, plan, up, deploy, sync, status, connect, tests, rotation, OS upgrades and down.
- `Dockerfile.server` and `deploy/docker` for running on existing infrastructure.
- `docs/cost.md`: the verified cost of the previous default, an itemized estimate, measured
  resource usage and the tradeoffs.
- Benchmark `--store sqlite`.

### Changed
- `./aquifer-aws configure` prefers Claude Haiku 4.5 for extraction. It costs about a third of
  Sonnet per document; benchmark: 52/52 cases, relationship recall 0.86.
- Discovery and the worker run on a pluggable runtime (`ingestion/runtime.py`): SQS, S3 and
  Secrets Manager on Lambda; the SQLite queue on a single server.
- The Terraform evaluation environment for the OpenSearch stack (`deploy/terraform`) is
  replaced by `deploy/aws`.

### Fixed
- One malformed item in an LLM response (e.g. a relationship with `"target": null`, seen with
  Claude Haiku 4.5) no longer fails the whole document's extraction; that item is dropped and
  counted (`llm_output_items_dropped`).

## [Unreleased] — Context correctness and extraction reliability

Found by deploying Engineering Context v1 to AWS and ingesting real GitHub repositories.

### Fixed
- **Removed dependencies were reported as dependencies.** The PR extraction prompt asked for
  "depends_on (dependencies added or removed)", so "moved the cache from memcached to redis"
  produced `billing-api depends_on memcached`. Relationships now carry an `assertion` and a
  derived `status`; a removal is `removed`, never a dependency.
- Failed LLM extraction of issues/PRs was only retried when the item changed or on a full
  re-sync. It is now retried automatically with exponential backoff from the content already
  in the index.
- After a GitHub token rotation, warm workers kept using the cached (revoked) token for up to
  15 minutes. A 401 now drops the cache and re-reads the secret once.
- Two concurrent writers of the same document could, in an unlucky interleaving, delete each
  other's generation. Replacement now uses a deterministic rank and keeps exactly one.
- An extraction-format change left unchanged issues/PRs with outdated relationships (they are
  fetched incrementally). Watermarks record the format and force one full sync on change.
- The benchmark's `--bedrock` mode ignored `AQUIFER_SEMANTIC_INDEX__*` settings and could
  report results without any successful LLM extraction; such runs are now marked invalid.

### Added
- Relationship `assertion` (present, added, removed, proposed, discussed, negated), `status`
  (current, claimed, removed, absent, proposed, not_merged, discussed), `as_of`,
  `quote_verified` and `mention_found` (replacing `grounded`, which only meant "mentioned").
- `find_related`: per-edge overall status (configuration wins; otherwise latest claim),
  `conflicting_evidence`, `absent_from_current_config`, `statuses` filter, current edges first;
  `support` counts per status. `evidence_status` values are now statuses.
- Extraction processing state per document (`extraction_status`, `extraction_attempts`,
  `extraction_error`, `next_retry_at`), retry sweep in discovery, reprocess jobs that rebuild
  documents from the index and reuse embeddings, `{"retry_failed": true}` manual recovery,
  actionable error codes (Bedrock Marketplace subscription, use-case form, model availability,
  access, credentials), backlog in `list_sources` and discovery logs.
- `aquifer-test check` explains Bedrock setup problems; `aquifer-test retry-failed`.
- Docs: Bedrock benchmark results, evaluation environment in the README, GitHub token rotation
  and retry operations, and a clear line between planned push ingestion and synchronization.
- Benchmark status scenarios and regression tests for additions, removals, replacements,
  negations, proposals, merged/unmerged PRs, conflicting evidence, retries and lifecycle.

## [Unreleased] — Engineering Context v1

Aquifer now returns engineering facts **with their evidence** and runs reliably on OpenSearch
Serverless. Tool names and required parameters are unchanged; new fields and parameters are
optional.

### Fixed
- **OpenSearch Serverless writes.** The store used `_delete_by_query` (not supported by AOSS)
  and custom document ids (rejected by Classic vector collections). Writes now use system ids and
  search-then-bulk-delete, and replace documents by writing the new generation first.
- **MCP server could not start** on a fresh install: `mcp>=1.2` resolved to `mcp` 2.x, which
  removed `FastMCP`. Pinned to `mcp>=1.8,<2`.
- **Extraction region**: the stack never passed the region/model to the extractor, which
  defaulted to `us-east-1` from every region.
- Errors no longer swallowed: S3 watermark read (only `NoSuchKey` means "never ran"), README
  fetch (only 404 means "no README"), GitHub 401/403.
- Issues/PRs paged newest-first so mid-run updates cannot shift unread items out of the window;
  PR paging stops at the watermark instead of walking every PR each run.
- A malformed item or failed document no longer fails its whole page (and every later page).
- Oversized tokens are split so no chunk exceeds the embedding input limit.
- `get_document` reassembles the exact original text (line breaks included), returns only the
  newest generation, and no longer pulls vectors.
- `list_entities` without a document scope is now an aggregated inventory (it sampled `k`
  arbitrary chunks).
- `find_related`, `list_entities` and `get_document` now run the `before_query`/`after_query`
  interceptor hooks (previously only `search_context` did).

### Added
- **Evidence & provenance** on results: repository, path, commit revision, line range,
  line-anchored permalinks, `updated_at`, `indexed_at`, `fetched_at`, PR/issue `state`.
- **Deterministic extraction** for Terraform, docker-compose and Backstage descriptors
  (`explicit_reference`, exact lines); repository files ingested from a pinned commit via the
  GitHub connector (configurable globs, size/count caps, blob-SHA skip).
- Relationship `source`, `source_type`, `target_type`, `origin`, `grounded`, `evidence`; LLM
  relationships are checked against the text and never labelled explicit.
- `find_related`: direction, typed entities, `repo` filter, per-edge support counts, distinct
  same-name `candidates`, `evidence_status`.
- `get_document`: `line_start`/`line_end` excerpt. `list_sources`: indexed counts and
  `last_indexed_at`.
- **Idempotent, convergent ingestion**: skip unchanged content, deletion reconciliation (files,
  READMEs), purge of de-listed repositories, bounded retries, rate-limit-aware re-enqueue,
  extraction status with retry, structured JSON logs.
- **Authentication**: bearer tokens on the MCP endpoint (Secrets Manager), principal passed to
  interceptors, audit events, repository-scope guard on every tool.
- **Deployment hardening**: config validation, least-privilege IAM and AOSS data policies,
  per-role security groups with HTTPS-only egress, optional TLS listener, non-root read-only MCP
  container, DLQ and worker-error alarms, log retention, 30-minute SQS visibility timeout,
  optional `aoss_standby_replicas=DISABLED`, `scripts/preflight.py`.
- **Local mode** (`python -m aquifer.local`) — run the whole product without AWS.
- **Retrieval benchmark** (`scripts/benchmark_retrieval.py`) with 45 cases and a CI gate.
- CI workflow, issue/PR templates, `SECURITY.md`, `CONTRIBUTING.md`, deployment, operations,
  security and agent-integration docs, a three-minute demo.

### Changed
- Documentation describes Aquifer as **source-available** (BSL 1.1), not open source. The
  license itself is unchanged.
- `search_context` and other tools bound `k` (default max 50) and reject non-scalar filter
  values.
