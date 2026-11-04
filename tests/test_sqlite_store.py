"""The SQLite store (single-server profile) behaves exactly like the reference store, and is
durable and thread-safe."""

import multiprocessing
import threading
from datetime import UTC, datetime, timedelta

import pytest

from aquifer.core.models import Chunk, DocumentKind, SourceType
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.vectorstore.memory import InMemoryVectorStore
from aquifer.vectorstore.sqlite import SqliteVectorStore

EMB = HashingEmbedder(64)
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _chunk(doc="d1", i=0, text="billing-api calls payments-db", gen="g1", updated=T0,
           repo="o/r", **extra):
    return Chunk(
        chunk_id=f"{doc}:{gen}:{i}", document_id=doc, index=i, text=text,
        embedding=EMB.embed_documents([text])[0], source_type=SourceType.GITHUB,
        kind=DocumentKind.FILE, repo=repo, title=doc, url="", generation=gen,
        updated_at=updated, indexed_at=updated, content_hash=f"h-{gen}", **extra)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryVectorStore()
    return SqliteVectorStore(tmp_path / "index.db")


# --- contract: identical behavior on both stores ------------------------------------------

def test_query_ranks_by_similarity_and_filters(store):
    store.upsert([_chunk("a", text="billing-api calls payments-db"),
                  _chunk("b", text="the ledger runs on postgres", repo="o/other")])
    hits = store.query(EMB.embed_query("payments-db billing"), k=2)
    assert [h.document_id for h in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score
    only = store.query(EMB.embed_query("payments-db"), k=5, filters={"repo": "o/other"})
    assert [h.document_id for h in only] == ["b"]


def test_replace_document_keeps_exactly_the_newest_generation(store):
    store.replace_document("d1", [_chunk(gen="g1", i=0), _chunk(gen="g1", i=1, text="x")])
    newer = T0 + timedelta(days=1)
    store.replace_document("d1", [_chunk(gen="g2", updated=newer, text="new text")])
    rows = store.get_by_document("d1")
    assert [r["generation"] for r in rows] == ["g2"]
    # A late write of older content does not delete newer content.
    store.replace_document("d1", [_chunk(gen="g0", updated=T0 - timedelta(days=1))])
    assert "g2" in {r["generation"] for r in store.get_by_document("d1")}


def test_delete_and_reconcile(store):
    store.upsert([_chunk("a"), _chunk("b"), _chunk("c", repo="o/x")])
    assert store.delete_by_filter({"repo": "o/x"}) == 1
    store.delete_documents(["a"])
    assert store.list_document_ids({"repo": "o/r"}) == {"b"}
    assert store.query(EMB.embed_query("billing"), k=5)[0].document_id == "b"


def test_state_vectors_retry_and_facets(store):
    due = T0 - timedelta(hours=1)
    store.upsert([_chunk("a", extraction_status="retry_pending", next_retry_at=due,
                         extraction_attempts=2, extraction_error="transient_error"),
                  _chunk("b", extraction_status="ok")])
    state = store.get_indexed_state(["a", "b", "missing"])
    assert set(state) == {"a", "b"}
    assert state["a"].extraction_attempts == 2
    assert store.documents_due_for_retry(T0) == ["a"]
    assert dict(store.facet("extraction_status")) == {"retry_pending": 1, "ok": 1}
    vectors = store.get_document_vectors("a")
    assert list(vectors) == ["billing-api calls payments-db"]
    assert vectors["billing-api calls payments-db"] == pytest.approx(
        EMB.embed_documents(["billing-api calls payments-db"])[0], abs=1e-6)


# --- SQLite-specific: durability, multi-process, concurrency ------------------------------

def test_index_survives_a_restart(tmp_path):
    path = tmp_path / "index.db"
    first = SqliteVectorStore(path)
    first.replace_document("d1", [_chunk()])
    first.close()
    reopened = SqliteVectorStore(path)
    assert reopened.query(EMB.embed_query("payments-db"), k=1)[0].document_id == "d1"
    assert reopened.stats()["documents"] == 1


def test_a_failed_write_changes_nothing(tmp_path, monkeypatch):
    store = SqliteVectorStore(tmp_path / "index.db")
    store.replace_document("d1", [_chunk(gen="g1")])

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_delete_rids", boom)
    with pytest.raises(RuntimeError):
        store.replace_document("d1", [_chunk(gen="g2", updated=T0 + timedelta(days=1))])
    monkeypatch.undo()
    # Neither memory nor disk holds the half-written generation.
    assert {r["generation"] for r in store.get_by_document("d1")} == {"g1"}
    assert {r["generation"] for r in SqliteVectorStore(tmp_path / "index.db")
            .get_by_document("d1")} == {"g1"}


def _write_from_another_process(path):  # module-level so it can be pickled
    SqliteVectorStore(path).upsert([_chunk("from-cli")])


def test_reads_see_writes_from_another_process(tmp_path):
    path = tmp_path / "index.db"
    store = SqliteVectorStore(path)
    store.upsert([_chunk("a")])
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_write_from_another_process, args=(str(path),))
    p.start()
    p.join(60)
    assert p.exitcode == 0
    assert store.list_document_ids({"repo": "o/r"}) == {"a", "from-cli"}


def test_concurrent_queries_during_ingestion(tmp_path):
    store = SqliteVectorStore(tmp_path / "index.db")
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                store.query(EMB.embed_query("billing"), k=5)
                store.facet("repo")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    for n in range(200):
        store.replace_document(f"d{n % 20}", [_chunk(f"d{n % 20}", gen=f"g{n}",
                                                      updated=T0 + timedelta(seconds=n))])
    stop.set()
    for t in readers:
        t.join()
    assert not errors
    assert store.stats()["documents"] == 20 and store.stats()["chunks"] == 20


def test_dimension_mismatch_is_rejected(tmp_path):
    store = SqliteVectorStore(tmp_path / "index.db")
    store.upsert([_chunk()])
    bad = _chunk("x").model_copy(update={"embedding": [1.0, 0.0]})
    with pytest.raises(ValueError, match="dimensions"):
        store.upsert([bad])
    assert store.list_document_ids({"repo": "o/r"}) == {"d1"}


def test_fast_top_k_matches_the_reference_ranking(tmp_path):
    """Unfiltered search uses a numpy top-k; ranking (ties, vectorless chunks) must not change."""
    ref, fast = InMemoryVectorStore(), SqliteVectorStore(tmp_path / "index.db")
    texts = ["alpha beta", "alpha beta", "beta gamma", "gamma delta", "alpha", "zeta"] * 5
    chunks = [_chunk(f"d{i}", text=t, gen=f"g{i}") for i, t in enumerate(texts)]
    chunks.append(_chunk("novec").model_copy(update={"embedding": []}))
    for store in (ref, fast):
        store.upsert(chunks)
    for k in (1, 3, 7, 40):
        for query in ("alpha beta", "gamma", "unrelated words"):
            v = EMB.embed_query(query)
            assert ([(h.chunk_id, h.score) for h in fast.query(v, k)]
                    == [(h.chunk_id, h.score) for h in ref.query(v, k)]), (k, query)


def test_a_stale_write_landing_after_a_newer_one_does_not_survive(store):
    """Found as a rare flake under 4 concurrent writers: the newer writer finished (and cleaned
    up) before the older writer's insert. Only the newest generation may remain."""
    newer = T0 + timedelta(days=1)
    store.replace_document("d1", [_chunk(gen="new", updated=newer, text="new")])
    store.replace_document("d1", [_chunk(gen="old", updated=T0, text="old")])
    assert {r["generation"] for r in store.get_by_document("d1")} == {"new"}
