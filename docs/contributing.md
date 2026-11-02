# Extending Aquifer

Aquifer is built to be extended without forking the core. This guide explains the modular
interfaces and walks through the common extensions: a **source connector**, an
**interceptor**, a **structured extractor** and a **vector store**. Development setup and PR
process: [../CONTRIBUTING.md](../CONTRIBUTING.md).

Two rules govern every contribution:

1. **Neutrality.** Connectors and prompts extract *objective facts only* — entities,
   relationships, keys. No judgments, scores, severities, or verdicts. See
   [concepts.md](./concepts.md#neutrality-principle).
2. **Provenance is never invented.** Set `path`, `revision`, line numbers and timestamps only
   from what the source reports; leave them `None` otherwise.
3. **Lean core.** The core depends on interfaces, never on concrete adapters or optional
   add-on code.

## The interfaces

All extension points are abstract interfaces in `aquifer/core/interfaces.py`:

| Interface | Responsibility |
| --- | --- |
| `Connector` | Pull from a source and normalize to `Document` |
| `Embedder` | Text → vector |
| `VectorStore` | Persist and search chunks + metadata |
| `Inferencer` | Structured-output LLM client |
| `SemanticIndexer` | Extract neutral `SemanticMetadata` from a `Document` |
| `Interceptor` | Cross-cutting hooks (`before/after_ingest`, `before/after_query`, `authorize`) |

Concrete implementations are wired at the edges in `aquifer/ingestion/factory.py`. The core never
imports them directly.

## Adding a new SourceType / Connector

```mermaid
flowchart TD
    A["1. Add SourceType enum<br/>core/models.py"] --> B["2. Implement Connector<br/>connectors/your_source.py"]
    B --> C["3. Register in factory<br/>ingestion/factory.py: build_connector"]
    C --> D["4. Build Source on schedule<br/>ingestion/discovery_handler.py"]
    D --> E["5. Register prompt template<br/>semantic/prompts.py: default_registry"]
    E --> F["6. Tests + eval_data examples"]
```

**1. Declare the source type.** Add a member to `SourceType` in `aquifer/core/models.py`
(e.g. `CONFLUENCE = "confluence"`).

**2. Implement the connector.** Create `aquifer/connectors/your_source.py` implementing
`Connector`:

```python
class YourConnector(Connector):
    source_type = SourceType.CONFLUENCE.value

    def discover(self, source: Source) -> Iterable[FetchJob]:
        # enumerate the initial fetch jobs (one per space/kind, etc.)
        ...

    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        # fetch ONE bounded page; normalize to Document; return a successor job or None
        ...
```

Keep connectors **pure and AWS-free** (use an injectable HTTP client) so they are unit-testable
without network or cloud access — follow `aquifer/connectors/github.py` as the reference.
Normalize each item with `Document.make_id(...)` and an appropriate `DocumentKind`, and fill the
provenance fields the source genuinely provides.

To propagate deletions, override `fetch_with_reconciliation` and return a `Reconciliation`
(scope filters + the complete set of live document ids) **only when the listing is complete** —
the pipeline deletes indexed documents in that scope that are not live. Classify errors with
`aquifer.core.errors` (`TransientError`, `RateLimitedError`, `SourceAuthError`,
`SourceNotFoundError`) so retries and failure isolation work.

**3. Register it in the factory.** Add a branch to `build_connector` in
`aquifer/ingestion/factory.py` mapping your `SourceType` to the connector.

**4. Make discovery create the Source.** Extend `_build_sources` in
`aquifer/ingestion/discovery_handler.py` so the scheduled Discovery Lambda constructs a `Source`
for the new type (with its config and incremental watermark).

**5. Register a semantic prompt.** Add a `PromptTemplate` in `aquifer/semantic/prompts.py` via
`default_registry()`, keyed by `(source_type, kind)` or at the source level. The registry
resolves with graceful fallback (exact → per-source → generic), so a single source-level template
is enough to start. Keep the instructions strictly neutral (extract facts, not verdicts).

**6. Add tests and golden examples.** Add fixture-based connector tests under `tests/` and, to
tune extraction, drop a few `*.json` artifacts into `tests/eval_data/` for the eval harness.

No changes to chunking, embedding, the vector store, or the MCP tools are required — they operate
on the source-agnostic `Document`/`Chunk` model.

## Adding an Interceptor

Interceptors are the seam for cross-cutting behavior (e.g. redaction, tenant scoping, and — in
enterprise builds — RBAC and audit). They are loaded by dotted path from configuration, so the
core never imports them. Interceptors receive the authenticated principal (the MCP token's
name) in `authorize` and in `QueryContext.principal`, and their `before_query`/`after_query`
hooks apply to **every** retrieval tool.

```python
from aquifer.core.interfaces import Interceptor

class TenantScopeInterceptor(Interceptor):
    def before_query(self, ctx):
        ctx.filters = {**ctx.filters, "repo": _tenant_repos(ctx.principal)}
        return ctx
```

Register it via the `AQUIFER_INTERCEPTORS` environment variable (a JSON array of
`"module:Class"` paths). They run in order; `authorize` is deny-overrides. The semantic indexer
itself is just an interceptor — see `aquifer/semantic/interceptor.py` for a complete example.

## Adding a structured extractor

Deterministic extractors live in `aquifer/semantic/structured/` and are dispatched by file path
in `extract_structured`. An extractor takes the document text and an `EvidenceFactory` and
returns `SemanticMetadata` whose entities and relationships have `origin="explicit_reference"`
and evidence built with `ev.span(start, end)` or `ev.lines(a, b)`. Rules:

- Only read keys or syntax the format defines; ignore everything else rather than guessing.
- Never emit a relationship because two names appear in the same file.
- Add the file pattern to the default `file_globs` (GitHub and local connectors) if it should be
  ingested by default, plus golden entries in `tests/benchmark/extraction_golden.json`.

## Adding a vector store

Implement `VectorStore` (`aquifer/core/interfaces.py`) and wire it in
`aquifer/ingestion/factory.py`. Use `aquifer/vectorstore/schema.py` (`chunk_to_source`,
`source_to_result`, `validate_filters`) so records and filter semantics match the built-in
stores. Override the optional methods (`replace_document`, `get_indexed_state`,
`list_document_ids`, `facet`, `max_value`) for write-then-delete replacement, skip-unchanged,
deletion propagation and aggregations; `InMemoryVectorStore` is a compact reference.

## Development workflow

```bash
pip install -e ".[dev]"                          # add ",cdk" + Node.js for CDK synth tests
pytest -q                                        # unit, integration, MCP and CDK tests (no AWS)
ruff check src tests scripts infrastructure      # lint
python scripts/benchmark_retrieval.py            # retrieval benchmark (offline)
python -m aquifer.local ingest examples/demo/repos   # try changes end to end
python scripts/eval_semantic_index.py            # tune LLM prompts (calls your Bedrock)
```

Please ensure `pytest` and `ruff` pass, and that new behavior is covered by tests that run
offline (use injected fakes for AWS/LLM clients and the in-memory store, as the existing tests
do).

## Licensing

Aquifer is source-available under the Business Source License 1.1 (see `LICENSE` and
`LICENSE_TERMS.md`). By contributing you agree your contributions are licensed under the same
terms.

---

**Support.** For commercial inquiries, professional advisory, or partnership discussions, contact
**[Senora.dev/contact](https://Senora.dev/contact)**. Aquifer.ai is an infrastructure layer you
run in your own VPC, not a SaaS.
