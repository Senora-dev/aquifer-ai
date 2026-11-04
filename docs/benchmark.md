# Retrieval Benchmark

`scripts/benchmark_retrieval.py` measures what an agent actually receives from Aquifer. It
builds a lake from repository directories with the production pipeline (connector → extraction
→ chunking → embedding → index) and runs scenario cases through the production MCP tool code.
Expected answers exist only in the case files; nothing in the retrieval path knows about the
benchmark.

```bash
python scripts/benchmark_retrieval.py                         # offline, deterministic (CI)
python scripts/benchmark_retrieval.py --out report.json       # full machine-readable report
python scripts/benchmark_retrieval.py --bedrock \             # Bedrock embeddings + LLM
  --price-embed-per-1k <usd> --price-llm-input-per-1k <usd>   # optional cost estimate
```

| Mode | Embedder | Extraction | Needs |
| --- | --- | --- | --- |
| offline (default) | Lexical hashing embedder | Deterministic parsers | Nothing — runs in CI |
| `--bedrock` | Titan v2 | Parsers + LLM; also runs the existing `tests/eval_data` LLM extraction harness | AWS credentials, Bedrock access (costs money) |

## Dataset

- **Corpus:** `examples/demo/repos/` — three synthetic repositories (`acme/payments`,
  `acme/platform`, `acme/legacy-billing`): docker-compose files, Backstage descriptors,
  Terraform, ADRs, READMEs, one issue and two PRs. 17 documents.
- **Cases:** `tests/benchmark/cases.json` — 45 cases in 10 scenarios:

| Scenario | Cases | What is checked |
| --- | --- | --- |
| explicit_dependencies | 7 | Documented dependencies of a service, both directions, with exact file + line |
| terraform_module_references | 6 | Every reference to a module source (version-exact), resource and module-output references |
| architecture_decision | 3 | The ADR that records a decision ranks top-3; its decision lines can be opened |
| cross_document | 4 | One relationship supported by several documents/repositories |
| similar_names | 5 | `billing-api` vs `billing-api-v2`; same name, different types kept distinct |
| stale_information | 4 | A removed dependency disappears after re-sync; updated text replaces old; state/updated_at exposed |
| missing_evidence | 4 | Unknown entities return `evidence_status: none`; out-of-scope repos/documents return nothing |
| deleted_or_renamed | 5 | Deleted files leave search and `get_document`; renamed files are re-pointed, old ids vanish |
| trace_to_source | 3 | `get_document` line excerpts contain the referenced text |
| repository_filter | 4 | `repo` filters never return other repositories |

Cases with `"mutations"` edit, delete or rename files in a copy of the corpus and re-sync before
querying. Two cases require LLM extraction (an issue stating a dependency in prose) and are
reported as **skipped** offline — never counted as passed.

- **Extraction golden file:** `tests/benchmark/extraction_golden.json` — the complete expected
  entities and relationships for the 9 structured files.

## Metrics

| Metric | Definition |
| --- | --- |
| recall@5 / recall@10 | Fraction of a search case's relevant documents among the top 5/10 distinct documents |
| precision@5 | Relevant documents among the top 5 distinct documents (low when a case has one relevant document by design) |
| MRR | Mean reciprocal rank of the first relevant document |
| edge_recall | Expected relationships returned, including the expected evidence file/line when specified |
| edge_precision (exhaustive cases) | For cases that list *all* true edges: returned edges that are true |
| provenance_coverage | Results/edges with repository + path/URL; file-backed ones also need line range and revision |
| freshness_metadata_coverage | Search results carrying `indexed_at` |
| unsupported_relationship_rate | Returned edges with no explicit reference and no verified quote |
| extraction P/R | Deterministic extraction vs the golden file (entities and relationships) |
| latency p50/p95 | In-process tool latency (in-memory store — **not** representative of OpenSearch/Bedrock) |
| estimated tokens | characters / 4 for embeddings and LLM input; dollars only if you pass prices |

## Results

Baseline = `main` at `483d2d6` before this release. Final = this branch, offline mode.

| Metric | Baseline | Final (offline) |
| --- | --- | --- |
| Cases passed / failed / skipped | not runnable¹ | **43 / 0 / 2** |
| recall@5 | not measurable¹ | 1.00 |
| recall@10 | not measurable¹ | 1.00 |
| precision@5 | not measurable¹ | 0.33 |
| MRR | not measurable¹ | 1.00 |
| edge recall | 0 — no deterministic extraction; files not ingested² | 1.00 |
| edge precision (exhaustive cases) | n/a | 1.00 |
| provenance coverage | 0 — responses had no path / revision / line fields² | 1.00 |
| freshness metadata coverage | 0 — no `indexed_at`; `get_document` had no timestamps² | 1.00 |
| unsupported relationship rate | not measurable (no origin/grounding) | 0.00 |
| extraction precision / recall (9 structured files) | 0 / 0 (capability absent)² | 1.00 / 1.00 (entities and relationships) |
| ingestion embedding tokens (17 docs) | — | ~1,300 |
| query embedding tokens | — | ~7 per query |
| `search_context` latency p50 / p95 | — | 0.80 / 1.32 ms (in-memory) |
| `find_related` latency p50 / p95 | — | 0.18 / 0.35 ms (in-memory) |

¹ The baseline could not run any offline evaluation: it had no in-process store or embedder,
did not ingest repository files, and its only harness (`scripts/eval_semantic_index.py`)
requires Bedrock. Its OpenSearch writes also relied on operations OpenSearch Serverless does not
support (see `docs/product-hardening-baseline.md`).
² Structural: the capability or field did not exist in the baseline code, so the value follows
from the code rather than from a run.

**Not measured in this release:** Bedrock-mode results (no AWS credentials were used), real-world
query latency against OpenSearch Serverless, and dollar costs. Run `--bedrock` in your account to
obtain them; the report includes the LLM extraction harness results in that mode.

## How to read these numbers

The corpus is **small, synthetic, and written alongside the implementation**. Perfect scores
here mean the system behaves as designed on these scenarios — they are a regression gate, not
evidence of accuracy on real repositories. In particular, search uses a lexical embedder offline,
whose behavior differs from Titan embeddings. Extending the corpus with realistic, independently
written cases (and running `--bedrock`) is the most valuable next step for this benchmark.

## CI gate

`tests/test_benchmark.py` runs the offline benchmark and fails if any case fails, if fewer than
30 cases run, or if recall@5 < 0.9, edge recall < 0.95, provenance coverage < 1.0 or the
unsupported-relationship rate > 0. CI uploads the JSON report as an artifact.

## Report format

Top-level keys (`report_version: 1`): `mode`, `embedder`, `extraction_mode`, `corpus`, `cases`,
`metrics`, `extraction` (with `per_file` differences), `llm_extraction` (Bedrock mode),
`latency_ms`, `cost`, `by_scenario`, and `results` — one entry per case with `status`, each
`check` and its outcome, per-case `metrics` and `latency_ms`.

## Adding cases

Add an object to `tests/benchmark/cases.json`:

```json
{
  "id": "dep-orders-db",
  "scenario": "explicit_dependencies",
  "tool": "find_related",
  "args": {"entity": "orders-db", "direction": "incoming"},
  "expect": {
    "edges": [{"direction": "incoming", "source": "orders-service", "type": "depends_on",
               "target": "orders-db",
               "evidence": {"repository": "acme/platform", "path": "docker-compose.yml",
                            "line_start": 5}}],
    "exhaustive": true
  }
}
```

Supported expectations: search — `relevant`, `relevant_in_top`, `only_repos`, `forbid_repos`,
`forbid_documents`; find_related — `edges`, `exhaustive`, `forbid_edges`, `forbid_targets`,
`evidence_status`, `min_candidates`, `only_repos`; get_document — `found`, `excerpt_contains`,
`text_contains`, `text_excludes`, `has_fields`; list_entities — `include`, `exclude`,
`no_case_duplicates`. Optional `mutations` (`write` / `delete` / `rename`) and
`"requires": "llm"`.
