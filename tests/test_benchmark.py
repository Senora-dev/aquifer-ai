"""The offline retrieval benchmark runs in CI as a regression gate."""

import json
from pathlib import Path

from aquifer.benchmark import run_benchmark
from aquifer.local.connector import LocalRepoConnector

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples/demo/repos"


def test_offline_benchmark_has_no_failures_and_meets_floors():
    report = run_benchmark(CORPUS, ROOT / "tests/benchmark/cases.json",
                           golden_path=ROOT / "tests/benchmark/extraction_golden.json")
    failed = [r["id"] for r in report["results"] if r["status"] == "failed"]
    assert failed == []
    assert report["cases"]["total"] >= 30
    m = report["metrics"]
    assert m["recall@5"] >= 0.9
    assert m["edge_recall"] >= 0.95
    assert m["provenance_coverage"] == 1.0
    assert m["unsupported_relationship_rate"] == 0.0
    assert report["extraction"]["relationship_precision"] == 1.0
    # LLM-only cases are skipped offline, never counted as passed.
    skipped = {r["id"] for r in report["results"] if r["status"] == "skipped"}
    assert "llm-issue-dependency" in skipped
    json.dumps(report, default=str)  # machine-readable


def test_local_connector_reports_declared_provenance(tmp_path):
    repo = tmp_path / "svc"
    (repo / ".aquifer").mkdir(parents=True)
    (repo / ".aquifer/source.json").write_text(json.dumps(
        {"repo": "acme/svc", "revision": "abc", "url_base": "https://github.com/acme/svc"}))
    (repo / "main.tf").write_text('module "m" { source = "./x" }\n')
    (repo / "notes.txt").write_text("not ingested")
    conn = LocalRepoConnector.from_root(tmp_path)
    assert conn.repos == ["acme/svc"]
    from aquifer.core.models import DocumentKind, FetchJob, SourceType

    job = FetchJob(source_id="s", source_type=SourceType.GITHUB, repo="acme/svc",
                   kind=DocumentKind.FILE)
    docs, _, recon = conn.fetch_with_reconciliation(job)
    assert [d.path for d in docs] == ["main.tf"]
    assert docs[0].revision == "abc"
    assert docs[0].url == "https://github.com/acme/svc/blob/abc/main.tf"
    assert recon.live_document_ids == {docs[0].id}


def test_local_connector_without_metadata_invents_nothing(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/main.tf").write_text("x")
    conn = LocalRepoConnector.from_root(tmp_path)
    from aquifer.core.models import DocumentKind, FetchJob, SourceType

    job = FetchJob(source_id="s", source_type=SourceType.GITHUB, repo="local/svc",
                   kind=DocumentKind.FILE)
    doc = conn.fetch(job)[0][0]
    assert doc.revision is None and doc.url == ""
