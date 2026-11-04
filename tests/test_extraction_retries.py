"""Reliable extraction retries and the end-to-end data lifecycle."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from aquifer.core.config import (
    GitHubSettings,
    IngestionSettings,
    McpSettings,
    SemanticIndexSettings,
    Settings,
)
from aquifer.core.errors import SourceAuthError
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Embedder, SemanticIndexer
from aquifer.core.models import (
    Document,
    DocumentKind,
    FetchJob,
    Reconciliation,
    Relationship,
    SemanticMetadata,
    SourceType,
)
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.ingestion import aws, discovery_handler, worker_handler
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.mcp.tools import ContextTools
from aquifer.semantic import interceptor as interceptor_module
from aquifer.semantic.failures import classify_extraction_error
from aquifer.semantic.interceptor import SemanticIndexInterceptor
from aquifer.vectorstore.memory import InMemoryVectorStore

REPO = "acme/payments"
JOB = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo=REPO, kind=DocumentKind.ISSUE)


class ClientError(Exception):
    """Shape of botocore's ClientError."""

    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


MARKETPLACE = ClientError("AccessDeniedException",
                          "Model access is denied due to IAM user or service role is not "
                          "authorized to perform the required AWS Marketplace actions")


class CountingEmbedder(Embedder):
    dimensions = 32

    def __init__(self):
        self.inner = HashingEmbedder(32)
        self.texts = 0

    def embed_documents(self, texts):
        self.texts += len(texts)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)


class ControllableIndexer(SemanticIndexer):
    def __init__(self):
        self.error: Exception | None = None
        self.calls = 0

    def extract(self, document):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SemanticMetadata(relationships=[Relationship(
            type="depends_on", source="billing-api", target="payments-db", assertion="present",
            quote="billing-api calls payments-db")])


class ListConnector:
    source_type = "github"

    def __init__(self, docs, recon=None):
        self.docs = docs
        self.recon = recon

    def discover(self, source):
        return []

    def fetch_with_reconciliation(self, job):
        return list(self.docs), None, self.recon


def _doc(n=1, body="billing-api calls payments-db\nsecond line", updated="2024-01-01"):
    return Document(
        id=Document.make_id(SourceType.GITHUB, REPO, DocumentKind.ISSUE, str(n)),
        source_type=SourceType.GITHUB, external_id=f"{REPO}#{n}", kind=DocumentKind.ISSUE,
        repo=REPO, title=f"Issue {n}", body=body, state="open",
        url=f"https://github.com/{REPO}/issues/{n}",
        updated_at=datetime.fromisoformat(updated).replace(tzinfo=UTC),
    )


@pytest.fixture
def lake(monkeypatch):
    settings = Settings(
        semantic_index=SemanticIndexSettings(enabled=True, retry_base_s=60, retry_max_s=3600,
                                             max_attempts=4, max_content_attempts=2),
        github=GitHubSettings(repo_allowlist=[REPO]),
        mcp=McpSettings(),
        ingestion=IngestionSettings(queue_url="q", retry_job_size=2),
    )
    monkeypatch.setattr(interceptor_module, "get_settings", lambda: settings)
    store, embedder, indexer = InMemoryVectorStore(), CountingEmbedder(), ControllableIndexer()

    def pipeline(docs=(), recon=None):
        return IngestionPipeline(
            connector=ListConnector(docs, recon), embedder=embedder, vector_store=store,
            interceptors=InterceptorPipeline([SemanticIndexInterceptor(indexer=indexer)]),
            chunk_size=100, chunk_overlap=10)

    return {"store": store, "embedder": embedder, "indexer": indexer, "pipeline": pipeline,
            "settings": settings,
            "tools": ContextTools(embedder, store, settings=settings)}


def _state(lake, doc_id):
    return lake["store"].get_by_document(doc_id)[0]


def _make_due(lake, doc_id):
    """Simulate the passage of time until the next retry is due."""
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    for rec in lake["store"]._records.values():
        if rec["document_id"] == doc_id:
            rec["next_retry_at"] = past


# --- failure classification ----------------------------------------------------------------

@pytest.mark.parametrize(("exc", "code", "kind"), [
    (MARKETPLACE, "bedrock_marketplace_subscription_required", "configuration"),
    (ClientError("ResourceNotFoundException",
                 "Model use case details have not been submitted for this account."),
     "bedrock_use_case_form_required", "configuration"),
    (ClientError("AccessDeniedException", "anthropic.x is not available for this account."),
     "bedrock_model_not_available", "configuration"),
    (ClientError("ResourceNotFoundException", "Model not found."), "bedrock_model_not_found",
     "configuration"),
    (ClientError("ThrottlingException", "Too many requests"), "transient_error", "transient"),
    (ClientError("ValidationException", "Input is too long for requested model."),
     "bedrock_rejected_input", "content"),
    (ValueError("No JSON object found in model output"), "unparseable_model_output", "content"),
    (type("TokenRetrievalError", (Exception,), {})("Token has expired and refresh failed"),
     "aws_credentials_unavailable", "configuration"),
])
def test_extraction_errors_have_actionable_codes(exc, code, kind):
    failure = classify_extraction_error(exc)
    assert (failure.code, failure.kind) == (code, kind)
    assert failure.message


# --- retries -------------------------------------------------------------------------------

def test_failed_extraction_recovers_automatically_without_full_resync(lake):
    lake["indexer"].error = MARKETPLACE
    lake["pipeline"]([_doc()]).run_job(JOB)
    rec = _state(lake, _doc().id)
    assert (rec["extraction_status"], rec["extraction_error"]) == (
        "retry_pending", "bedrock_marketplace_subscription_required")
    assert rec["relationships"] == []  # nothing invented while extraction is missing

    # The operator fixes access. The source does NOT change and is NOT fetched again.
    lake["indexer"].error = None
    _make_due(lake, _doc().id)
    due = lake["store"].documents_due_for_retry(datetime.now(UTC))
    assert due == [_doc().id]
    embedded_before = lake["embedder"].texts
    result = lake["pipeline"]().reprocess(due)
    assert result.indexed == 1
    rec = _state(lake, _doc().id)
    assert rec["extraction_status"] == "ok" and rec["extraction_attempts"] == 0
    assert rec["relationships"][0]["target"] == "payments-db"
    assert lake["embedder"].texts == embedded_before  # unchanged text: vectors reused
    assert lake["store"].documents_due_for_retry(datetime.now(UTC)) == []


def test_retries_wait_for_backoff_and_double(lake):
    lake["indexer"].error = MARKETPLACE
    pipe = lake["pipeline"]([_doc()])
    pipe.run_job(JOB)
    first = datetime.fromisoformat(_state(lake, _doc().id)["next_retry_at"])
    # Re-fetching the unchanged document before it is due does not retry (no Bedrock call).
    calls = lake["indexer"].calls
    assert pipe.run_job(JOB).skipped == 1 and lake["indexer"].calls == calls
    assert lake["store"].documents_due_for_retry(datetime.now(UTC)) == []
    _make_due(lake, _doc().id)
    lake["pipeline"]().reprocess([_doc().id])
    rec = _state(lake, _doc().id)
    second = datetime.fromisoformat(rec["next_retry_at"])
    assert rec["extraction_attempts"] == 2
    # 60s then 120s (base 60, doubling)
    assert timedelta(seconds=100) < second - datetime.now(UTC) <= timedelta(seconds=120)
    assert first < second


def test_content_failures_stop_after_their_budget(lake):
    lake["indexer"].error = ValueError("No JSON object found in model output")
    lake["pipeline"]([_doc()]).run_job(JOB)
    _make_due(lake, _doc().id)
    lake["pipeline"]().reprocess([_doc().id])  # attempt 2 of max_content_attempts=2
    rec = _state(lake, _doc().id)
    assert (rec["extraction_status"], rec["extraction_attempts"]) == ("failed", 2)
    assert rec["next_retry_at"] is None
    # Terminal: not swept, and an unchanged re-fetch does not call the model again.
    assert lake["store"].documents_due_for_retry(datetime.now(UTC)) == []
    calls = lake["indexer"].calls
    assert lake["pipeline"]([_doc()]).run_job(JOB).skipped == 1
    assert lake["indexer"].calls == calls


def test_changed_content_resets_the_retry_budget(lake):
    lake["indexer"].error = ValueError("bad json")
    lake["pipeline"]([_doc()]).run_job(JOB)
    _make_due(lake, _doc().id)
    lake["pipeline"]().reprocess([_doc().id])
    assert _state(lake, _doc().id)["extraction_status"] == "failed"
    lake["indexer"].error = None
    lake["pipeline"]([_doc(body="new text about billing-api calls payments-db",
                           updated="2024-02-01")]).run_job(JOB)
    rec = _state(lake, _doc().id)
    assert (rec["extraction_status"], rec["extraction_attempts"]) == ("ok", 0)


def test_legacy_failed_documents_get_one_retry(lake):
    lake["indexer"].error = None
    lake["pipeline"]([_doc()]).run_job(JOB)
    for rec in lake["store"]._records.values():  # indexed before retry tracking existed
        rec.update(extraction_status="failed", extraction_error=None, next_retry_at=None)
    assert lake["store"].documents_due_for_retry(datetime.now(UTC)) == [_doc().id]


def test_reprocess_skips_documents_whose_content_changed_meanwhile(lake):
    lake["indexer"].error = MARKETPLACE
    lake["pipeline"]([_doc()]).run_job(JOB)
    _make_due(lake, _doc().id)
    lake["indexer"].error = None
    lake["pipeline"]([_doc(body="edited", updated="2024-05-01")]).run_job(JOB)
    result = lake["pipeline"]().reprocess([_doc().id])
    assert result.indexed == 0
    assert "edited" in _state(lake, _doc().id)["text"]


def test_discovery_sweep_queues_due_documents_and_reports_backlog(lake, monkeypatch):
    lake["indexer"].error = MARKETPLACE
    lake["pipeline"]([_doc(1), _doc(2), _doc(3)]).run_job(JOB)
    for n in (1, 2, 3):
        _make_due(lake, _doc(n).id)
    sent = []
    monkeypatch.setattr(aws, "enqueue_job", lambda url, job, **kw: sent.append(job))
    report = discovery_handler.enqueue_due_retries(lake["settings"], lake["store"])
    assert report["due_for_retry"] == 3 and report["retry_jobs"] == 2  # job size 2
    assert report["by_error"] == {"bedrock_marketplace_subscription_required": 3}
    assert all(j.job_type == "reprocess" and j.repo == REPO for j in sent)
    assert sorted(i for j in sent for i in j.document_ids) == sorted(_doc(n).id for n in (1, 2, 3))


def test_worker_runs_reprocess_jobs(monkeypatch):
    calls = []

    class Pipe:
        def reprocess(self, ids, include_failed=False):
            calls.append(ids)

    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, s: Pipe())
    job = FetchJob(source_id="retry", source_type=SourceType.GITHUB, repo=REPO,
                   kind=DocumentKind.ISSUE, job_type="reprocess", document_ids=["a", "b"])
    assert worker_handler.handler({"Records": [{"body": job.model_dump_json()}]})["processed"] == 1
    assert calls == [["a", "b"]]


def test_worker_rereads_a_rotated_token_after_401(monkeypatch):
    attempts = []

    class Pipe:
        def process_job(self, job):
            attempts.append(dict(worker_handler._token_cache))
            if len(attempts) == 1:
                raise SourceAuthError("GitHub rejected the token (401)")
            return None

    worker_handler._token_cache["arn"] = (0.0, "old-token")
    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, s: Pipe())
    monkeypatch.setattr(worker_handler.aws, "enqueue_job", lambda *a, **k: None)
    worker_handler.handler({"Records": [{"body": JOB.model_dump_json()}]})
    assert len(attempts) == 2
    assert attempts[1] == {}  # the cached (revoked) token was dropped before the retry


# --- lifecycle ----------------------------------------------------------------------------

def test_lifecycle_unchanged_reingestion_does_no_extraction_or_embedding(lake):
    pipe = lake["pipeline"]([_doc(1), _doc(2)])
    assert pipe.run_job(JOB).indexed == 2
    calls, texts = lake["indexer"].calls, lake["embedder"].texts
    assert pipe.run_job(JOB).skipped == 2
    assert (lake["indexer"].calls, lake["embedder"].texts) == (calls, texts)


def test_lifecycle_deleted_document_takes_its_relationships_with_it(lake):
    lake["pipeline"]([_doc(1)]).run_job(JOB)
    assert lake["tools"].find_related("payments-db")["relationships"]
    recon = Reconciliation(scope={"repo": REPO, "kind": "issue"}, live_document_ids=set())
    lake["pipeline"]([], recon).run_job(JOB)
    out = lake["tools"].find_related("payments-db")
    assert out["relationships"] == [] and out["evidence_status"] == "none"
    assert lake["tools"].get_document(_doc(1).id)["found"] is False


def test_lifecycle_update_replaces_relationships_and_provenance(lake):
    lake["pipeline"]([_doc(1)]).run_job(JOB)
    lake["indexer"].error = None

    class Moved(SemanticIndexer):
        def extract(self, document):
            return SemanticMetadata(relationships=[Relationship(
                type="depends_on", source="billing-api", target="ledger-db",
                assertion="present", quote="billing-api now calls ledger-db")])

    pipe = IngestionPipeline(
        connector=ListConnector([_doc(1, body="intro\nbilling-api now calls ledger-db",
                                      updated="2024-03-01")]),
        embedder=lake["embedder"], vector_store=lake["store"],
        interceptors=InterceptorPipeline([SemanticIndexInterceptor(indexer=Moved())]),
        chunk_size=100, chunk_overlap=10)
    pipe.run_job(JOB)
    assert lake["tools"].find_related("payments-db")["relationships"] == []
    edge = lake["tools"].find_related("ledger-db")["relationships"][0]
    ev = edge["evidence"][0]
    assert (ev["line_start"], ev["locator"], ev["as_of"][:10]) == (2, "quote", "2024-03-01")
    assert ev["url"] == f"https://github.com/{REPO}/issues/1"


def test_lifecycle_concurrent_ingestion_creates_no_duplicates(lake):
    docs = [_doc(n) for n in range(1, 6)]
    errors = []

    def run():
        try:
            lake["pipeline"](docs).run_job(JOB)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for d in docs:
        chunks = lake["store"].get_by_document(d.id)
        generations = {c["generation"] for c in chunks}
        # At most one surviving generation per document (older ones were cleaned up), and
        # readers always see a single consistent copy.
        assert len(generations) == 1, generations
        assert lake["tools"].get_document(d.id)["chunk_count"] == len(chunks)
    out = lake["tools"].find_related("payments-db")
    assert out["relationships"][0]["support"]["documents"] == 5


def test_interleaved_writers_of_the_same_document_converge_on_one_generation():
    """A writes, B writes, A cleans up, B cleans up — the worst interleaving."""
    from aquifer.core.chunking import chunk_document

    store = InMemoryVectorStore()
    doc = _doc(1)

    def chunks(tag):
        out = chunk_document(doc, 100, 10)
        for c in out:
            c.embedding, c.generation, c.content_hash = [1.0], tag, "h"
        return out

    a, b = chunks("gen-a"), chunks("gen-b")
    b[0].indexed_at = a[0].indexed_at + timedelta(milliseconds=1)
    for c in b:
        c.indexed_at = b[0].indexed_at
    store.upsert(a)
    store.replace_document(doc.id, b)  # B upserts and cleans up (A is older -> deleted)
    store.replace_document(doc.id, a)  # A re-runs its cleanup: B outranks A -> kept
    gens = {r["generation"] for r in store.get_by_document(doc.id)}
    assert gens == {"gen-b"} or gens == {"gen-a", "gen-b"}
    from aquifer.core.chunking import newest_generation

    assert {r["generation"] for r in newest_generation(store.get_by_document(doc.id))} == {
        "gen-b"}
    # A later writer of the same version (any order) never leaves the document empty.
    store.replace_document(doc.id, chunks("gen-c"))
    assert store.get_by_document(doc.id)


def test_older_version_retry_never_deletes_newer_content():
    store = InMemoryVectorStore()
    new_doc, old_doc = _doc(1, body="new", updated="2024-06-01"), _doc(1, body="old")
    from aquifer.core.chunking import chunk_document

    def chunks(doc, tag):
        out = chunk_document(doc, 100, 10)
        for c in out:
            c.embedding, c.generation = [1.0], tag
        return out

    store.replace_document(new_doc.id, chunks(new_doc, "new"))
    store.replace_document(old_doc.id, chunks(old_doc, "old"))  # stale retry written later
    from aquifer.core.chunking import newest_generation

    survivors = newest_generation(store.get_by_document(new_doc.id))
    assert {r["generation"] for r in survivors} == {"new"}
    assert "new" in survivors[0]["text"]


def test_operator_can_retry_failed_documents_with_a_fresh_budget(lake, monkeypatch):
    lake["indexer"].error = ValueError("bad json")
    lake["pipeline"]([_doc()]).run_job(JOB)
    _make_due(lake, _doc().id)
    lake["pipeline"]().reprocess([_doc().id])
    assert _state(lake, _doc().id)["extraction_status"] == "failed"
    # The normal sweep leaves it alone ...
    assert lake["pipeline"]().reprocess([_doc().id]).indexed == 0
    # ... the operator fixes the cause and asks for a retry.
    lake["indexer"].error = None
    sent = []
    monkeypatch.setattr(aws, "enqueue_job", lambda url, job, **kw: sent.append(job))
    assert discovery_handler.enqueue_failed_retries(lake["settings"], lake["store"]) == 1
    assert sent[0].job_type == "reprocess_failed"
    lake["pipeline"]().reprocess(sent[0].document_ids, include_failed=True)
    assert _state(lake, _doc().id)["extraction_status"] == "ok"
