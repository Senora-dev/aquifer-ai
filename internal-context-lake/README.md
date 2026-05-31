# Aquifer AI

An open-source, **in-VPC Context Lake** — neutral **infrastructure for AI agents**, not a
developer portal and not a reasoning engine. Aquifer is headless: it aggregates engineering
context (GitHub first) into an OpenSearch vector store and exposes a standard **MCP API** that
AI agents query directly. All inside your own VPC, deployable as a single CDK stack.

> **Agents are the users; Aquifer stays neutral.** No UI, ever. And no verdicts: Aquifer provides
> objective, organized context (entities, relationships, keys) and lets the **agent** do the
> reasoning. We aim to be the best **Search & Retrieval** layer in the industry — not a
> decision-maker.

## Why

AI agents are only as good as the context they can reach. Aquifer is a single, reliable
**Search & Retrieval** layer over your engineering sources: it ingests, embeds, and—at ingestion
time—extracts **neutral, objective metadata** (typed entities like `service:billing-api` and
`jira_key:PROJ-400`, plus factual relationships such as `depends_on`/`references`/`part_of`) and
indexes it as queryable fields. An agent can then retrieve exactly the context it needs to reason
on its own — e.g. gather everything related to a service before deciding whether to deploy — with
zero data leaving your network and **no external AI processing** beyond your own Bedrock.

Aquifer draws **no conclusions and makes no value judgments**. It does not score, rank by risk,
or answer "can I deploy?" — it returns the facts; the agent decides.

> **Status:** a functional, neutral Context Lake. It ingests, embeds, and serves context over
> MCP; **semantic indexing** (in the `before_ingest` hook) extracts objective entities and
> relationships via a modular per-source prompt registry and indexes them as queryable fields;
> and the MCP tools let agents search and traverse that metadata.

## MCP tools

Agents reach the lake through five neutral retrieval tools (no verdicts, no interpretation):

- `search_context(query, k?, filters?)` — semantic k-NN search with metadata filters.
- `get_document(document_id)` — a full document, chunks reassembled.
- `list_sources()` — the configured sources.
- `find_related(entity, relationship_types?, k?)` — **retrieve relationships**: items whose
  extracted relationships point at an entity, plus items that reference it, merged and annotated
  by how they matched.
- `list_entities(document_id?, entity_type?)` — a neutral **inventory** of the distinct
  `{type, name}` entities extracted (e.g. services, datastores, Jira keys).

## Architecture (baseline)

```
EventBridge ─► Discovery Lambda ─► SQS ─► Worker Lambda ─► Bedrock (embed) ─► OpenSearch Serverless
                                                                                        ▲
AI agents ──MCP (HTTP/SSE)──► Fargate MCP server ──embed query + k-NN search────────────┘
```

- **Vector store:** Amazon OpenSearch Serverless (k-NN vectors)
- **Compute:** Lambda for ingestion (SQS fan-out), Fargate for the persistent MCP server
- **Embeddings:** Amazon Bedrock via a VPC (PrivateLink) endpoint
- **Connectors:** GitHub (issues, PRs, READMEs, discussions); more are additive via the
  `Connector` interface

## Documentation

Full documentation lives in [`docs/`](./docs):

- [Architecture](./docs/architecture.md) — the single-stack CDK design and pipeline.
- [Concepts](./docs/concepts.md) — Context Lake, Semantic Indexing, the Neutrality Principle, MCP.
- [Getting Started](./docs/getting-started.md) — deploy the stack into your AWS account.
- [Contributing](./docs/contributing.md) — add a `SourceType`/connector or an interceptor.

## Layout

```
src/aquifer/        # Python package: core, connectors, adapters, ingestion Lambdas, MCP server
infrastructure/     # AWS CDK (Python) — the single deployable stack
scripts/            # dev tooling (e.g. the semantic-indexing eval harness)
tests/              # unit + fixture tests (incl. tests/eval_data/ golden artifacts)
```

## Tuning semantic indexing

Extraction quality is governed by the modular prompts in `aquifer/semantic/prompts.py`. To tune
them, run the eval harness over the golden artifacts in `tests/eval_data/` — it prints the
selected prompt, the generated neutral metadata JSON, and recall against the expected objective
fields (entity names, relationship targets):

```bash
python scripts/eval_semantic_index.py            # all examples (calls your own Bedrock)
python scripts/eval_semantic_index.py --name jira --verbose
```

The scoring is intentionally neutral — it measures whether we extracted the right *facts*, not
any judgment. Add more `*.json` examples to `tests/eval_data/` to grow coverage.

## Modularity & the enterprise seam

The core is intentionally lean. Cross-cutting concerns (SSO, RBAC, audit logs) are **not** in
this baseline — instead, core exposes an `Interceptor` seam (`before/after_ingest`,
`before/after_query`, `authorize`) with pass-through no-op implementations. Enterprise features
ship as a separate package that registers interceptors; **core never imports enterprise code**.

## Quickstart (planned)

```bash
pip install -e ".[dev]"        # core + adapters + tooling
pytest                          # run the test suite
cd infrastructure && cdk synth  # synthesize the single stack
cdk deploy                      # bring up the whole Context Lake
```

## License

**Business Source License 1.1 (BSL 1.1)** — see the [`LICENSE`](LICENSE) file.
Converts to Apache 2.0 on the Change Date.
