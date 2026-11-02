"""Benchmark runner: build a lake, run cases, compute metrics, emit a machine-readable report."""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aquifer.core.config import GitHubSettings, McpSettings, SemanticIndexSettings, Settings
from aquifer.core.entities import normalize_name
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Embedder
from aquifer.core.models import Source, SourceType
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.local.connector import LocalRepoConnector
from aquifer.mcp.tools import ContextTools
from aquifer.semantic.interceptor import SemanticIndexInterceptor
from aquifer.semantic.structured import extract_structured
from aquifer.vectorstore.memory import InMemoryVectorStore

REPORT_VERSION = 1


# --- lake construction -------------------------------------------------------------------


class _MeteredEmbedder(Embedder):
    """Counts characters embedded so the report can estimate token volume."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.dimensions = inner.dimensions
        self.document_chars = 0
        self.query_chars = 0

    def embed_documents(self, texts):  # noqa: ANN001, ANN201
        self.document_chars += sum(len(t) for t in texts)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):  # noqa: ANN001, ANN201
        self.query_chars += len(text)
        return self.inner.embed_query(text)


class _MeteredIndexer:
    def __init__(self, inner) -> None:  # noqa: ANN001
        self.inner = inner
        self.calls = 0
        self.input_chars = 0

    def extract(self, document):  # noqa: ANN001, ANN201
        self.calls += 1
        self.input_chars += len(document.body or "") + len(document.title or "")
        return self.inner.extract(document)


@dataclass
class Lake:
    tools: ContextTools
    pipeline: IngestionPipeline
    connector: LocalRepoConnector
    embedder: _MeteredEmbedder
    indexer: _MeteredIndexer | None
    root: Path
    stats: dict = field(default_factory=dict)

    def sync(self) -> dict:
        source = Source(id="bench", type=SourceType.GITHUB, config={})
        totals: dict[str, int] = defaultdict(int)
        for job in self.connector.discover(source):
            r = self.pipeline.run_job(job)
            for k in ("fetched", "indexed", "skipped", "failed", "deleted"):
                totals[k] += getattr(r, k)
        return dict(totals)


def build_lake(corpus: Path, *, mode: str = "offline", workdir: Path | None = None) -> Lake:
    """Copy ``corpus`` (so cases can mutate it) and ingest it."""
    root = Path(tempfile.mkdtemp(prefix="aquifer-bench-", dir=workdir))
    shutil.copytree(corpus, root, dirs_exist_ok=True)
    connector = LocalRepoConnector.from_root(root)
    use_llm = mode == "bedrock"
    settings = Settings(
        github=GitHubSettings(repo_allowlist=connector.repos),
        mcp=McpSettings(default_k=10, max_k=50),
        semantic_index=SemanticIndexSettings(enabled=use_llm),
    )
    if use_llm:
        from aquifer.embedding.bedrock import BedrockEmbedder
        from aquifer.ingestion.factory import build_semantic_indexer

        inner: Embedder = BedrockEmbedder(settings.embedding)
        indexer = _MeteredIndexer(build_semantic_indexer(settings))
    else:
        from aquifer.embedding.hashing import HashingEmbedder

        inner = HashingEmbedder(512)
        indexer = None
    embedder = _MeteredEmbedder(inner)
    store = InMemoryVectorStore()
    interceptor = SemanticIndexInterceptor(indexer=indexer)
    pipeline = IngestionPipeline(
        connector=connector, embedder=embedder, vector_store=store,
        interceptors=InterceptorPipeline([interceptor]),
        chunk_size=settings.ingestion.chunk_size, chunk_overlap=settings.ingestion.chunk_overlap,
    )
    lake = Lake(tools=ContextTools(embedder, store, settings=settings), pipeline=pipeline,
                connector=connector, embedder=embedder, indexer=indexer, root=root)
    _with_settings(settings, lambda: lake.stats.update(lake.sync()))
    lake.settings = settings  # type: ignore[attr-defined]
    return lake


def _with_settings(settings: Settings, fn):  # noqa: ANN001, ANN202
    """The semantic interceptor reads global settings; scope them to this lake."""
    import aquifer.semantic.interceptor as icept

    original = icept.get_settings
    icept.get_settings = lambda: settings
    try:
        return fn()
    finally:
        icept.get_settings = original


# --- cases ---------------------------------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data["cases"] if isinstance(data, dict) else data


def _apply_mutations(lake: Lake, mutations: list[dict]) -> None:
    for m in mutations:
        target = lake.root / m["path"]
        if m["op"] == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(m["content"])
        elif m["op"] == "delete":
            target.unlink()
        elif m["op"] == "rename":
            dest = lake.root / m["to"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            target.rename(dest)
        else:
            raise ValueError(f"unknown mutation op {m['op']!r}")
    _with_settings(lake.settings, lake.sync)  # type: ignore[attr-defined]


def _edge_key(e: dict) -> tuple:
    return (e.get("direction"), normalize_name(e.get("source") or ""), e["type"],
            normalize_name(e["target"]))


def _unique_docs(results: list[dict]) -> list[str]:
    out: list[str] = []
    for r in results:
        if r["document_id"] not in out:
            out.append(r["document_id"])
    return out


def _has_provenance(ev: dict, file_backed: bool) -> bool:
    if not ev.get("repository") or not (ev.get("path") or ev.get("url")):
        return False
    if file_backed:
        return ev.get("line_start") is not None and ev.get("revision") is not None
    return True


@dataclass
class CaseResult:
    id: str
    scenario: str
    tool: str
    status: str  # passed | failed | skipped
    checks: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    latency_ms: float = 0.0


def run_case(lake: Lake, case: dict) -> CaseResult:
    res = CaseResult(id=case["id"], scenario=case["scenario"], tool=case["tool"],
                     status="passed")
    checks = res.checks

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})

    if case.get("mutations"):
        _apply_mutations(lake, case["mutations"])

    args = dict(case.get("args", {}))
    started = time.perf_counter()
    tool = getattr(lake.tools, case["tool"])
    out = tool(**args)
    res.latency_ms = round((time.perf_counter() - started) * 1000, 3)
    exp = case.get("expect", {})

    if case["tool"] == "search_context":
        docs = _unique_docs(out)
        relevant = set(exp.get("relevant", []))
        if relevant:
            res.metrics["recall@5"] = len(relevant & set(docs[:5])) / len(relevant)
            res.metrics["recall@10"] = len(relevant & set(docs[:10])) / len(relevant)
            top5 = docs[:5]
            res.metrics["precision@5"] = (len(relevant & set(top5)) / len(top5)) if top5 else 0.0
            rank = next((i + 1 for i, d in enumerate(docs) if d in relevant), None)
            res.metrics["reciprocal_rank"] = 1 / rank if rank else 0.0
            k_check = exp.get("relevant_in_top", 5)
            check(f"all relevant in top {k_check}", relevant <= set(docs[:k_check]),
                  {"top": docs[:k_check]})
        if "forbid_repos" in exp:
            check("no forbidden repos", not ({r["repo"] for r in out} & set(exp["forbid_repos"])))
        if "only_repos" in exp:
            check("only allowed repos", {r["repo"] for r in out} <= set(exp["only_repos"]))
        if "forbid_documents" in exp:
            check("no forbidden documents", not (set(docs) & set(exp["forbid_documents"])))
        res.metrics["provenance"] = [
            _has_provenance(r["evidence"], r.get("kind") == "file") for r in out
        ]
        res.metrics["fresh"] = [bool(r.get("indexed_at")) for r in out]

    elif case["tool"] == "find_related":
        returned = {_edge_key(e): e for e in out["relationships"]}
        expected = exp.get("edges", [])
        found = 0
        for e in expected:
            key = _edge_key(e)
            got = returned.get(key)
            ok = got is not None
            if ok and "evidence" in e:
                want = e["evidence"]
                ok = any(all(ev.get(k) == v for k, v in want.items()) for ev in got["evidence"])
            found += ok
            check(f"edge {key}", ok)
        if expected:
            res.metrics["edge_recall"] = found / len(expected)
        if exp.get("exhaustive") and returned:
            truth = {_edge_key(e) for e in expected}
            res.metrics["edge_precision"] = len(truth & set(returned)) / len(returned)
        for e in exp.get("forbid_edges", []):
            check(f"no edge {_edge_key(e)}", _edge_key(e) not in returned)
        for t in exp.get("forbid_targets", []):
            check(f"no target {t}", all(normalize_name(t) not in (k[1], k[3])
                                        for k in returned))
        if "evidence_status" in exp:
            check("evidence_status", out["evidence_status"] == exp["evidence_status"],
                  out["evidence_status"])
        if "min_candidates" in exp:
            check("distinct candidates", len(out["candidates"]) >= exp["min_candidates"],
                  out["candidates"])
        if "only_repos" in exp:
            check("only allowed repos", {r["repo"] for r in out["related"]}
                  <= set(exp["only_repos"]))
        res.metrics["edges_returned"] = len(returned)
        res.metrics["edges_unsupported"] = sum(
            1 for e in out["relationships"]
            if e["support"]["explicit"] == 0 and e["support"]["llm_grounded"] == 0
        )
        res.metrics["provenance"] = [
            any(_has_provenance(ev, bool(ev.get("path"))) and ev.get("line_start") is not None
                for ev in e["evidence"])
            for e in out["relationships"]
        ]

    elif case["tool"] == "get_document":
        check("found", out["found"] == exp.get("found", True))
        if exp.get("excerpt_contains"):
            text = (out.get("excerpt") or {}).get("text") or ""
            check("excerpt", exp["excerpt_contains"] in text, text)
        if exp.get("text_contains"):
            check("text", exp["text_contains"] in out.get("text", ""))
        if exp.get("text_excludes"):
            check("text excludes", exp["text_excludes"] not in out.get("text", ""))
        for f in exp.get("has_fields", []):
            check(f"field {f}", out.get(f) is not None)

    elif case["tool"] == "list_entities":
        names = {(e["type"], e["name"]) for e in out["entities"]}
        for t, n in exp.get("include", []):
            check(f"entity {t}:{n}", (t, n) in names)
        for t, n in exp.get("exclude", []):
            check(f"no entity {t}:{n}", (t, n) not in names)
        if exp.get("no_case_duplicates"):
            keys = [(e["type"], e["name"].lower(), e.get("namespace")) for e in out["entities"]]
            check("no duplicates", len(keys) == len(set(keys)))

    if any(not c["ok"] for c in checks):
        res.status = "failed"
    return res


# --- extraction scoring (deterministic extractors vs golden) -------------------------------


def score_extraction(corpus: Path, golden_path: Path) -> dict:
    golden = json.loads(Path(golden_path).read_text())
    connector = LocalRepoConnector.from_root(corpus)
    docs = {}
    for repo in connector.repos:
        from aquifer.core.models import DocumentKind, FetchJob

        job = FetchJob(source_id="x", source_type=SourceType.GITHUB, repo=repo,
                       kind=DocumentKind.FILE)
        for d in connector.fetch(job)[0]:
            docs[f"{repo}:{d.path}"] = d
    tp_e = fp_e = fn_e = tp_r = fp_r = fn_r = 0
    per_file = {}
    for key, want in golden["files"].items():
        meta = extract_structured(docs[key])
        got_e = {(e.type, normalize_name(e.name)) for e in (meta.entities if meta else [])}
        got_r = {(normalize_name(r.source or ""), r.type, normalize_name(r.target))
                 for r in (meta.relationships if meta else [])}
        want_e = {(t, normalize_name(n)) for t, n in want["entities"]}
        want_r = {(normalize_name(s), t, normalize_name(o)) for s, t, o in want["relationships"]}
        tp_e += len(got_e & want_e)
        fp_e += len(got_e - want_e)
        fn_e += len(want_e - got_e)
        tp_r += len(got_r & want_r)
        fp_r += len(got_r - want_r)
        fn_r += len(want_r - got_r)
        per_file[key] = {"unexpected_relationships": sorted(map(list, got_r - want_r)),
                         "missing_relationships": sorted(map(list, want_r - got_r))}

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "files": len(golden["files"]),
        "entity_precision": ratio(tp_e, tp_e + fp_e),
        "entity_recall": ratio(tp_e, tp_e + fn_e),
        "relationship_precision": ratio(tp_r, tp_r + fp_r),
        "relationship_recall": ratio(tp_r, tp_r + fn_r),
        "per_file": per_file,
    }


def _llm_extraction_eval(corpus: Path) -> dict:
    from aquifer.ingestion.factory import build_semantic_indexer
    from aquifer.semantic.eval import evaluate, load_examples

    data_dir = Path(corpus).resolve().parents[2] / "tests" / "eval_data"
    results = evaluate(build_semantic_indexer(), load_examples(data_dir))

    def avg(key: str) -> float | None:
        return _mean([r.score[key] for r in results if r.score.get(key) is not None])

    return {
        "examples": len(results),
        "entity_recall": avg("entity_recall"),
        "relationship_recall": avg("relationship_recall"),
        "entity_precision_lower_bound": avg("entity_precision_lower_bound"),
        "relationship_precision_lower_bound": avg("relationship_precision_lower_bound"),
    }


# --- the run ---------------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[idx], 3)


def run_benchmark(corpus: Path, cases_path: Path, *, mode: str = "offline",
                  golden_path: Path | None = None, prices: dict | None = None) -> dict:
    cases = load_cases(cases_path)
    base = build_lake(corpus, mode=mode)
    ingest_stats = dict(base.stats)
    ingest_chars = base.embedder.document_chars
    llm_calls = base.indexer.calls if base.indexer else 0
    llm_chars = base.indexer.input_chars if base.indexer else 0

    results: list[CaseResult] = []
    query_chars_before = base.embedder.query_chars
    for case in cases:
        if case.get("requires") == "llm" and mode != "bedrock":
            results.append(CaseResult(id=case["id"], scenario=case["scenario"],
                                      tool=case["tool"], status="skipped",
                                      checks=[{"check": "requires LLM extraction",
                                               "ok": True}]))
            continue
        lake = build_lake(corpus, mode=mode) if case.get("mutations") else base
        results.append(run_case(lake, case))
        if lake is not base:
            shutil.rmtree(lake.root, ignore_errors=True)
    queries = sum(1 for r in results if r.status != "skipped" and r.tool == "search_context")
    query_chars = base.embedder.query_chars - query_chars_before
    shutil.rmtree(base.root, ignore_errors=True)

    def collect(metric: str) -> list[float]:
        return [r.metrics[metric] for r in results if metric in r.metrics]

    provenance = [v for r in results for v in r.metrics.get("provenance", [])]
    fresh = [v for r in results for v in r.metrics.get("fresh", [])]
    edges_total = sum(collect("edges_returned"))
    edges_unsupported = sum(collect("edges_unsupported"))
    by_scenario: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        by_scenario[r.scenario][r.status] += 1
    latencies = defaultdict(list)
    for r in results:
        if r.status != "skipped":
            latencies[r.tool].append(r.latency_ms)

    est_tokens = {
        "ingestion_embedding_tokens": ingest_chars // 4,
        "ingestion_llm_calls": llm_calls,
        "ingestion_llm_input_tokens": llm_chars // 4,
        "per_query_embedding_tokens": (query_chars // 4 // queries) if queries else None,
    }
    cost = {"estimated_tokens": est_tokens, "method": "characters / 4 (approximation)"}
    if prices:
        usd = (est_tokens["ingestion_embedding_tokens"] / 1000 * prices.get("embed_per_1k", 0)
               + est_tokens["ingestion_llm_input_tokens"] / 1000
               * prices.get("llm_input_per_1k", 0))
        cost["prices_used"] = prices
        cost["ingestion_usd_excluding_llm_output"] = round(usd, 6)

    llm_extraction = None
    if mode == "bedrock":
        llm_extraction = _llm_extraction_eval(corpus)

    passed = sum(r.status == "passed" for r in results)
    failed = sum(r.status == "failed" for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    return {
        "report_version": REPORT_VERSION,
        "mode": mode,
        "embedder": "bedrock" if mode == "bedrock" else "hashing (lexical, offline)",
        "extraction_mode": "deterministic + LLM" if mode == "bedrock" else "deterministic only",
        "corpus": {"documents_indexed": ingest_stats.get("indexed"), **ingest_stats},
        "cases": {"total": len(results), "passed": passed, "failed": failed,
                  "skipped": skipped},
        "metrics": {
            "recall@5": _mean(collect("recall@5")),
            "recall@10": _mean(collect("recall@10")),
            "precision@5": _mean(collect("precision@5")),
            "mrr": _mean(collect("reciprocal_rank")),
            "edge_recall": _mean(collect("edge_recall")),
            "edge_precision_exhaustive_cases": _mean(collect("edge_precision")),
            "provenance_coverage": _mean([float(v) for v in provenance]),
            "freshness_metadata_coverage": _mean([float(v) for v in fresh]),
            "unsupported_relationship_rate": (round(edges_unsupported / edges_total, 4)
                                              if edges_total else None),
        },
        "extraction": score_extraction(corpus, golden_path) if golden_path else None,
        # The pre-existing LLM extraction harness (tests/eval_data), run only with Bedrock.
        "llm_extraction": llm_extraction,
        "latency_ms": {
            tool: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95), "n": len(v)}
            for tool, v in sorted(latencies.items())
        },
        "latency_note": "In-process tool latency against the in-memory store; not representative "
                        "of OpenSearch Serverless or Bedrock network latency.",
        "cost": cost,
        "by_scenario": {k: dict(v) for k, v in sorted(by_scenario.items())},
        "results": [r.__dict__ for r in results],
    }
