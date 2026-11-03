import pytest

from aquifer.core.config import VectorStoreSettings
from aquifer.core.errors import TransientError
from aquifer.core.models import (
    Chunk,
    DocumentKind,
    Entity,
    Relationship,
    SourceType,
)
from aquifer.vectorstore.opensearch import AossVectorStore


class FakeIndices:
    def __init__(self, existing=False):
        self._existing = existing
        self.created = []
        self.mappings = []

    def exists(self, *, index):
        return self._existing

    def create(self, *, index, body):
        self.created.append((index, body))
        self._existing = True

    def put_mapping(self, *, index, body):
        self.mappings.append((index, body))


class FakeOpenSearch:
    """Mimics the AOSS operations the adapter uses. ``search_hits`` is returned for searches;
    ``scan_hits`` (if given) answers the generation-cleanup / delete scans."""

    def __init__(self, existing=False, search_hits=None, bulk_errors=False, scan_hits=None,
                 aggregations=None, bulk_status=500):
        self.indices = FakeIndices(existing=existing)
        self._search_hits = search_hits or []
        self._scan_hits = scan_hits
        self._bulk_errors = bulk_errors
        self._bulk_status = bulk_status
        self._aggs = aggregations or {}
        self.bulk_bodies = []
        self.search_bodies = []

    @property
    def bulk_body(self):
        return self.bulk_bodies[0] if self.bulk_bodies else None

    @property
    def search_body(self):
        return self.search_bodies[-1] if self.search_bodies else None

    def bulk(self, *, body):
        self.bulk_bodies.append(body)
        if self._bulk_errors:
            return {"errors": True,
                    "items": [{"index": {"error": "boom", "status": self._bulk_status}}]}
        return {"errors": False, "items": []}

    def delete_by_query(self, *, index, body):  # pragma: no cover - must never be called
        raise AssertionError("AOSS does not support _delete_by_query")

    def search(self, *, index, body):
        self.search_bodies.append(body)
        if body.get("size") == 0:
            return {"aggregations": self._aggs}
        if self._scan_hits is not None and "from" in body:
            hits = self._scan_hits if body["from"] == 0 else []
            return {"hits": {"hits": hits}}
        return {"hits": {"hits": self._search_hits}}


def _store(client):
    settings = VectorStoreSettings(endpoint="https://x.aoss.amazonaws.com", index="ctx")
    return AossVectorStore(settings=settings, dimensions=4, client=client)


def _chunk(idx=0, generation="g2", updated_at=None):
    return Chunk(
        chunk_id=f"github:o/r:issue:1#{idx}",
        document_id="github:o/r:issue:1",
        index=idx,
        text="hello world",
        embedding=[0.1, 0.2, 0.3, 0.4],
        source_type=SourceType.GITHUB,
        kind=DocumentKind.ISSUE,
        repo="o/r",
        title="Bug",
        url="https://github.com/o/r/issues/1",
        generation=generation,
        updated_at=updated_at,
    )


def test_ensure_index_creates_when_missing():
    client = FakeOpenSearch(existing=False)
    _store(client).ensure_index()
    assert len(client.indices.created) == 1
    _, body = client.indices.created[0]
    assert body["settings"]["index"]["knn"] is True
    props = body["mappings"]["properties"]
    assert props["vector"]["dimension"] == 4
    for field in ("chunk_id", "generation", "entity_keys", "relationship_target_keys", "path"):
        assert props[field] == {"type": "keyword"}
    assert props["indexed_at"] == {"type": "date"}


def test_ensure_index_adds_new_fields_to_existing_index():
    client = FakeOpenSearch(existing=True)
    _store(client).ensure_index()
    assert client.indices.created == []
    _, body = client.indices.mappings[0]
    # New keyword fields are added; the vector and dynamic object fields are left untouched.
    assert body["properties"]["generation"] == {"type": "keyword"}
    assert "vector" not in body["properties"]
    assert "entities" not in body["properties"]


def test_upsert_never_sends_custom_ids():
    # AOSS Classic vector collections reject custom document ids on index/create.
    client = FakeOpenSearch()
    _store(client).upsert([_chunk(0), _chunk(1)])
    body = client.bulk_body
    assert len(body) == 4
    assert body[0] == {"index": {"_index": "ctx"}}
    assert body[1]["chunk_id"] == "github:o/r:issue:1#0"
    assert body[1]["vector"] == [0.1, 0.2, 0.3, 0.4]
    assert body[1]["source_type"] == "github"


def test_upsert_flattens_semantic_metadata_for_indexing():
    chunk = _chunk(0)
    chunk.summary = "adds caching"
    chunk.entities = [
        Entity(type="service", name="Billing-API"),
        Entity(type="datastore", name="redis"),
    ]
    chunk.relationships = [
        Relationship(type="depends_on", target="Redis", source="billing-api",
                     origin="explicit_reference")
    ]

    client = FakeOpenSearch()
    _store(client).upsert([chunk])
    source = client.bulk_body[1]

    assert source["summary"] == "adds caching"
    # Raw flattened arrays are kept for compatibility...
    assert source["entity_names"] == ["Billing-API", "redis"]
    assert source["entity_types"] == ["service", "datastore"]
    assert source["relationship_targets"] == ["Redis"]
    assert source["relationship_types"] == ["depends_on"]
    # ...plus case-normalized keys for matching.
    assert source["entity_keys"] == ["service:billing-api", "datastore:redis"]
    assert source["relationship_target_keys"] == ["redis"]
    assert source["relationship_source_keys"] == ["billing-api"]
    assert source["relationship_origins"] == ["explicit_reference"]
    assert source["entities"][0]["name"] == "Billing-API"
    assert source["relationships"][0]["origin"] == "explicit_reference"


def test_query_filters_on_enriched_fields_and_excludes_vectors():
    client = FakeOpenSearch(search_hits=[])
    _store(client).query([0.0] * 4, k=5, filters={"relationship_targets": "redis"})
    body = client.search_body
    knn = body["query"]["knn"]["vector"]
    assert knn["filter"]["bool"]["filter"] == [{"term": {"relationship_targets": "redis"}}]
    assert body["_source"] == {"excludes": ["vector"]}


def test_upsert_empty_is_noop():
    client = FakeOpenSearch()
    _store(client).upsert([])
    assert client.bulk_body is None


def test_upsert_raises_on_errors():
    client = FakeOpenSearch(bulk_errors=True, bulk_status=400)
    with pytest.raises(RuntimeError):
        _store(client).upsert([_chunk()])


def test_upsert_throttling_is_transient():
    client = FakeOpenSearch(bulk_errors=True, bulk_status=429)
    with pytest.raises(TransientError):
        _store(client).upsert([_chunk()])


def test_delete_by_document_searches_then_bulk_deletes_by_id():
    client = FakeOpenSearch(scan_hits=[{"_id": "sys-1", "_source": {}},
                                       {"_id": "sys-2", "_source": {}}])
    _store(client).delete_by_document("github:o/r:issue:1")
    scan = client.search_bodies[0]
    assert scan["query"] == {"bool": {"filter": [{"term": {"document_id": "github:o/r:issue:1"}}]}}
    assert client.bulk_body == [
        {"delete": {"_index": "ctx", "_id": "sys-1"}},
        {"delete": {"_index": "ctx", "_id": "sys-2"}},
    ]


def test_replace_document_writes_new_generation_before_deleting_old():
    hits = [
        {"_id": "old", "_source": {"generation": "g1", "updated_at": "2024-01-01T00:00:00+00:00"}},
        {"_id": "legacy", "_source": {}},  # indexed before generations existed
        {"_id": "new", "_source": {"generation": "g2"}},
    ]
    client = FakeOpenSearch(scan_hits=hits)
    _store(client).replace_document("github:o/r:issue:1", [_chunk(0, "g2")])
    # First bulk = the new chunks; second = deletion of every other generation.
    assert client.bulk_bodies[0][0] == {"index": {"_index": "ctx"}}
    deleted = {a["delete"]["_id"] for a in client.bulk_bodies[1]}
    assert deleted == {"old", "legacy"}


def test_replace_document_keeps_a_newer_concurrent_generation():
    from datetime import datetime

    hits = [{"_id": "newer", "_source": {"generation": "g9",
                                         "updated_at": "2025-01-01T00:00:00+00:00"}}]
    client = FakeOpenSearch(scan_hits=hits)
    older = _chunk(0, "g2", updated_at=datetime.fromisoformat("2024-01-01T00:00:00+00:00"))
    _store(client).replace_document("github:o/r:issue:1", [older])
    assert len(client.bulk_bodies) == 1  # nothing deleted


def test_bulk_delete_404_is_not_an_error():
    client = FakeOpenSearch(scan_hits=[{"_id": "gone", "_source": {}}], bulk_errors=True,
                            bulk_status=404)
    _store(client).delete_by_document("d")  # does not raise


def test_query_builds_knn_and_parses_hits():
    hits = [
        {
            "_id": "sys-1",
            "_score": 0.87,
            "_source": {
                "chunk_id": "github:o/r:issue:1#0",
                "document_id": "github:o/r:issue:1",
                "text": "hello",
                "title": "Bug",
                "url": "u",
                "source_type": "github",
                "kind": "issue",
                "repo": "o/r",
                "line_start": 3,
                "indexed_at": "2024-05-01T00:00:00+00:00",
            },
        }
    ]
    client = FakeOpenSearch(search_hits=hits)
    results = _store(client).query([0.1, 0.2, 0.3, 0.4], k=5, filters={"repo": "o/r"})

    assert client.search_body["size"] == 5
    knn = client.search_body["query"]["knn"]["vector"]
    assert knn["k"] == 5
    assert knn["filter"]["bool"]["filter"] == [{"term": {"repo": "o/r"}}]

    assert len(results) == 1
    # The logical chunk id comes from the stored field, not the system _id.
    assert results[0].chunk_id == "github:o/r:issue:1#0"
    assert results[0].score == 0.87
    assert results[0].repo == "o/r"
    assert results[0].line_start == 3
    assert results[0].indexed_at is not None


def test_legacy_hits_fall_back_to_the_hit_id():
    hits = [{"_id": "github:o/r:issue:1#0", "_score": 1.0, "_source": {"document_id": "d"}}]
    results = _store(FakeOpenSearch(search_hits=hits)).query([0.0] * 4, k=1)
    assert results[0].chunk_id == "github:o/r:issue:1#0"


def test_query_rejects_unknown_filter_field():
    client = FakeOpenSearch()
    with pytest.raises(ValueError):
        _store(client).query([0.0] * 4, k=3, filters={"secret": "x"})


def test_query_rejects_query_dsl_values():
    client = FakeOpenSearch()
    with pytest.raises(ValueError):
        _store(client).query([0.0] * 4, k=3, filters={"repo": {"script": "x"}})


def test_get_by_document_returns_sources_and_sorts_by_index():
    hits = [
        {"_id": "d#0", "_source": {"text": "a", "chunk_index": 0}},
        {"_id": "d#1", "_source": {"text": "b", "chunk_index": 1}},
    ]
    client = FakeOpenSearch(search_hits=hits)
    sources = _store(client).get_by_document("github:o/r:issue:1")

    assert [s["text"] for s in sources] == ["a", "b"]
    body = client.search_body
    assert body["query"]["term"]["document_id"] == "github:o/r:issue:1"
    assert body["sort"] == [{"chunk_index": {"order": "asc"}}]
    assert body["_source"] == {"excludes": ["vector"]}


def test_facet_uses_terms_aggregation_with_document_cardinality():
    aggs = {"values": {"buckets": [{"key": "service:billing-api", "doc_count": 7,
                                     "docs": {"value": 2}}]}}
    client = FakeOpenSearch(aggregations=aggs)
    out = _store(client).facet("entity_keys", {"repo": "o/r"}, size=50)
    assert out == [("service:billing-api", 2)]
    body = client.search_body
    assert body["size"] == 0
    assert body["aggs"]["values"]["terms"] == {"field": "entity_keys", "size": 50}


def test_list_document_ids_uses_aggregation():
    aggs = {"ids": {"buckets": [{"key": "a"}, {"key": "b"}]}}
    out = _store(FakeOpenSearch(aggregations=aggs)).list_document_ids({"repo": "o/r"})
    assert out == {"a", "b"}


class _IndexMissing(Exception):
    status_code = 404
    error = "index_not_found_exception"


class MissingIndexClient(FakeOpenSearch):
    def search(self, *, index, body):
        raise _IndexMissing("no such index [ctx]")


def test_reads_before_the_index_exists_are_empty():
    store = _store(MissingIndexClient())
    assert store.facet("repo") == []
    assert store.query([0.0] * 4, k=3) == []
    assert store.get_by_document("d") == []
    assert store.list_document_ids({"repo": "o/r"}) == set()
    assert store.max_value("indexed_at") is None


def test_other_search_errors_still_raise():
    class Boom(FakeOpenSearch):
        def search(self, *, index, body):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _store(Boom()).query([0.0] * 4, k=3)


def test_concurrent_index_creation_race_is_tolerated_and_waits_for_visibility():
    class Exists(Exception):
        error = "resource_already_exists_exception"

    class RacingIndices(FakeIndices):
        def __init__(self):
            super().__init__(existing=False)
            self.exists_calls = 0

        def exists(self, *, index):
            self.exists_calls += 1
            return self.exists_calls >= 4  # not visible for the first few checks

        def create(self, *, index, body):
            raise Exists("index [ctx/abc] already exists")

    client = FakeOpenSearch()
    client.indices = RacingIndices()
    sleeps = []
    store = AossVectorStore(settings=VectorStoreSettings(endpoint="x", index="ctx"),
                            dimensions=4, client=client, sleep=sleeps.append)
    store.ensure_index()
    assert client.indices.exists_calls == 4 and len(sleeps) == 2


def test_index_creation_other_errors_raise():
    class Bad(FakeIndices):
        def create(self, *, index, body):
            raise RuntimeError("mapper_parsing_exception")

    client = FakeOpenSearch()
    client.indices = Bad(existing=False)
    with pytest.raises(RuntimeError):
        _store(client).ensure_index()
