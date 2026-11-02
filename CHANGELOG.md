# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
