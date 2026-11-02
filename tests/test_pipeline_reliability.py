"""Idempotency, replacement, failure isolation and deletion propagation of the pipeline."""

from datetime import UTC, datetime, timedelta

import pytest

from aquifer.core.errors import TransientError
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Connector, Embedder, SemanticIndexer
from aquifer.core.models import (
    Document,
    DocumentKind,
    FetchJob,
    Reconciliation,
    SemanticMetadata,
    SourceType,
)
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.semantic.interceptor import SemanticIndexInterceptor
from aquifer.vectorstore.memory import InMemoryVectorStore


class CountingEmbedder(Embedder):
    dimensions = 64

    def __init__(self):
        self.inner = HashingEmbedder(64)
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)


class ListConnector(Connector):
    source_type = "github"

    def __init__(self, docs, reconciliation=None):
        self.docs = docs
        self.reconciliation = reconciliation

    def discover(self, source):
        return []

    def fetch(self, job):
        return self.docs, None

    def fetch_with_reconciliation(self, job):
        return self.docs, None, self.reconciliation


def _doc(n=1, body="billing-api calls payments-db", updated=None, kind=DocumentKind.ISSUE):
    return Document(
        id=Document.make_id(SourceType.GITHUB, "o/r", kind, str(n)),
        source_type=SourceType.GITHUB,
        external_id=f"o/r#{n}",
        kind=kind,
        repo="o/r",
        title=f"Issue {n}",
        body=body,
        updated_at=updated or datetime(2024, 1, 1, tzinfo=UTC),
    )


JOB = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r", kind=DocumentKind.ISSUE)


def _pipeline(connector, store, embedder=None, interceptors=None, chunk_size=100):
    return IngestionPipeline(
        connector=connector,
        embedder=embedder or CountingEmbedder(),
        vector_store=store,
        interceptors=interceptors,
        chunk_size=chunk_size,
        chunk_overlap=10,
    )


def test_reingesting_unchanged_content_is_skipped_without_embedding():
    store, emb = InMemoryVectorStore(), CountingEmbedder()
    pipe = _pipeline(ListConnector([_doc()]), store, emb)
    first = pipe.run_job(JOB)
    second = pipe.run_job(JOB)  # e.g. SQS redelivery or overlapping watermark
    assert (first.indexed, second.indexed, second.skipped) == (1, 0, 1)
    assert emb.calls == 1
    assert len(store) == 1  # no duplicates


def test_edited_document_replaces_all_old_chunks():
    store = InMemoryVectorStore()
    long_body = " ".join(f"word{i}" for i in range(400))
    _pipeline(ListConnector([_doc(body=long_body)]), store).run_job(JOB)
    assert len(store) > 1
    edited = _doc(body="short now", updated=datetime(2024, 2, 1, tzinfo=UTC))
    _pipeline(ListConnector([edited]), store).run_job(JOB)
    texts = [s["text"] for s in store.get_by_document(edited.id)]
    assert texts == ["Issue 1\n\nshort now"]


def test_failed_write_leaves_previous_generation_searchable():
    class FailingUpsert(InMemoryVectorStore):
        fail = False

        def upsert(self, chunks):
            if self.fail:
                raise TransientError("aoss 503")
            super().upsert(chunks)

    store = FailingUpsert()
    _pipeline(ListConnector([_doc()]), store).run_job(JOB)
    store.fail = True
    edited = _doc(body="new text", updated=datetime(2024, 2, 1, tzinfo=UTC))
    with pytest.raises(TransientError):
        _pipeline(ListConnector([edited]), store).run_job(JOB)
    # Never missing: the old version is still there.
    assert "billing-api" in store.get_by_document(edited.id)[0]["text"]


def test_out_of_order_older_version_is_ignored():
    store = InMemoryVectorStore()
    newer = _doc(body="v2", updated=datetime(2024, 3, 1, tzinfo=UTC))
    older = _doc(body="v1", updated=datetime(2024, 1, 1, tzinfo=UTC))
    _pipeline(ListConnector([newer]), store).run_job(JOB)
    result = _pipeline(ListConnector([older]), store).run_job(JOB)
    assert result.skipped == 1
    assert store.get_by_document(newer.id)[0]["text"].endswith("v2")


def test_permanent_document_failure_is_isolated_and_counted():
    class Exploding(Embedder):
        dimensions = 8

        def embed_documents(self, texts):
            if any("poison" in t for t in texts):
                raise ValueError("input too long")
            return [[1.0] * 8 for _ in texts]

        def embed_query(self, text):
            return [1.0] * 8

    store = InMemoryVectorStore()
    docs = [_doc(1), _doc(2, body="poison"), _doc(3)]
    result = _pipeline(ListConnector(docs), store, Exploding()).run_job(JOB)
    assert (result.indexed, result.failed) == (2, 1)
    assert result.failed_ids == [docs[1].id]


def test_transient_failure_fails_the_page_for_retry():
    class Throttled(Embedder):
        dimensions = 8

        def embed_documents(self, texts):
            raise TransientError("ThrottlingException")

        def embed_query(self, text):
            return [1.0] * 8

    with pytest.raises(TransientError):
        _pipeline(ListConnector([_doc()]), InMemoryVectorStore(), Throttled()).run_job(JOB)


def test_reconciliation_deletes_documents_that_no_longer_exist():
    store = InMemoryVectorStore()
    files = [_doc(n, kind=DocumentKind.FILE) for n in (1, 2, 3)]
    _pipeline(ListConnector(files), store).run_job(JOB)
    recon = Reconciliation(scope={"repo": "o/r", "kind": "file"},
                           live_document_ids={files[0].id, files[2].id})
    result = _pipeline(ListConnector([], recon), store).run_job(JOB)
    assert result.deleted == 1
    assert store.get_by_document(files[1].id) == []
    assert store.get_by_document(files[0].id) != []


def test_reconciliation_is_scoped():
    store = InMemoryVectorStore()
    _pipeline(ListConnector([_doc(1)]), store).run_job(JOB)  # an issue
    recon = Reconciliation(scope={"repo": "o/r", "kind": "file"}, live_document_ids=set())
    _pipeline(ListConnector([], recon), store).run_job(JOB)
    assert store.get_by_document(_doc(1).id) != []  # issues untouched


def test_document_that_became_empty_is_removed():
    store = InMemoryVectorStore()
    _pipeline(ListConnector([_doc()]), store).run_job(JOB)
    emptied = _doc(body="", updated=datetime(2024, 5, 1, tzinfo=UTC))
    emptied.title = ""
    _pipeline(ListConnector([emptied]), store).run_job(JOB)
    assert store.get_by_document(emptied.id) == []


class _FlakyIndexer(SemanticIndexer):
    def __init__(self):
        self.fail = True
        self.calls = 0

    def extract(self, document):
        self.calls += 1
        if self.fail:
            raise ValueError("No JSON object found in model output")
        return SemanticMetadata(summary="ok")


def test_failed_extraction_is_recorded_and_retried_on_next_run():
    store = InMemoryVectorStore()
    indexer = _FlakyIndexer()
    seam = InterceptorPipeline([SemanticIndexInterceptor(indexer=indexer)])
    pipe = _pipeline(ListConnector([_doc()]), store, interceptors=seam)

    pipe.run_job(JOB)
    assert store.get_by_document(_doc().id)[0]["extraction_status"] == "failed"

    indexer.fail = False
    result = pipe.run_job(JOB)  # same content, but extraction failed last time
    assert result.indexed == 1
    assert store.get_by_document(_doc().id)[0]["extraction_status"] == "ok"
    assert pipe.run_job(JOB).skipped == 1  # now stable


def test_chunks_carry_provenance_and_freshness():
    store = InMemoryVectorStore()
    doc = _doc(kind=DocumentKind.FILE, body="line one\nline two\nbilling-api")
    doc.path, doc.revision = "svc/config.yaml", "abc123"
    doc.fetched_at = datetime.now(UTC) - timedelta(seconds=5)
    _pipeline(ListConnector([doc]), store).run_job(JOB)
    rec = store.get_by_document(doc.id)[0]
    assert (rec["path"], rec["revision"], rec["line_start"], rec["line_end"]) == (
        "svc/config.yaml", "abc123", 1, 3)
    assert rec["indexed_at"] and rec["fetched_at"] and rec["content_hash"] and rec["generation"]


def test_transient_extraction_error_fails_the_page_for_retry():
    class Throttled(SemanticIndexer):
        def extract(self, document):
            raise TransientError("ThrottlingException")

    seam = InterceptorPipeline([SemanticIndexInterceptor(indexer=Throttled())])
    store = InMemoryVectorStore()
    with pytest.raises(TransientError):
        _pipeline(ListConnector([_doc()]), store, interceptors=seam).run_job(JOB)
    assert len(store) == 0
