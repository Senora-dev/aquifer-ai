import pytest

from aquifer.core.config import VectorStoreSettings
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

    def exists(self, *, index):
        return self._existing

    def create(self, *, index, body):
        self.created.append((index, body))
        self._existing = True


class FakeOpenSearch:
    def __init__(self, existing=False, search_hits=None, bulk_errors=False):
        self.indices = FakeIndices(existing=existing)
        self._search_hits = search_hits or []
        self._bulk_errors = bulk_errors
        self.bulk_body = None
        self.search_body = None
        self.deleted = []

    def bulk(self, *, body):
        self.bulk_body = body
        if self._bulk_errors:
            return {"errors": True, "items": [{"index": {"error": "boom"}}]}
        return {"errors": False, "items": []}

    def delete_by_query(self, *, index, body):
        self.deleted.append((index, body))

    def search(self, *, index, body):
        self.search_body = body
        return {"hits": {"hits": self._search_hits}}


def _store(client):
    settings = VectorStoreSettings(endpoint="https://x.aoss.amazonaws.com", index="ctx")
    return AossVectorStore(settings=settings, dimensions=4, client=client)


def _chunk(idx=0):
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
    )


def test_ensure_index_creates_when_missing():
    client = FakeOpenSearch(existing=False)
    _store(client).ensure_index()
    assert len(client.indices.created) == 1
    _, body = client.indices.created[0]
    assert body["settings"]["index"]["knn"] is True
    assert body["mappings"]["properties"]["vector"]["dimension"] == 4


def test_ensure_index_noop_when_present():
    client = FakeOpenSearch(existing=True)
    _store(client).ensure_index()
    assert client.indices.created == []


def test_upsert_builds_bulk_actions():
    client = FakeOpenSearch()
    _store(client).upsert([_chunk(0), _chunk(1)])
    body = client.bulk_body
    # Two action/source pairs.
    assert len(body) == 4
    assert body[0]["index"]["_id"] == "github:o/r:issue:1#0"
    assert body[1]["vector"] == [0.1, 0.2, 0.3, 0.4]
    assert body[1]["source_type"] == "github"


def test_upsert_flattens_semantic_metadata_for_indexing():
    chunk = _chunk(0)
    chunk.summary = "adds caching"
    chunk.entities = [
        Entity(type="service", name="billing-api"),
        Entity(type="datastore", name="redis"),
    ]
    chunk.relationships = [Relationship(type="depends_on", target="redis")]

    client = FakeOpenSearch()
    _store(client).upsert([chunk])
    source = client.bulk_body[1]

    assert source["summary"] == "adds caching"
    # Flattened keyword arrays for efficient filtering...
    assert source["entity_names"] == ["billing-api", "redis"]
    assert source["entity_types"] == ["service", "datastore"]
    assert source["relationship_targets"] == ["redis"]
    assert source["relationship_types"] == ["depends_on"]
    # ...plus the full structured objects for retrieval.
    assert source["entities"] == [
        {"type": "service", "name": "billing-api"},
        {"type": "datastore", "name": "redis"},
    ]
    assert source["relationships"] == [
        {"type": "depends_on", "target": "redis", "description": ""}
    ]


def test_query_filters_on_enriched_fields():
    client = FakeOpenSearch(search_hits=[])
    # Graph traversal: "find items related to redis" → term on relationship_targets.
    _store(client).query([0.0] * 4, k=5, filters={"relationship_targets": "redis"})
    knn = client.search_body["query"]["knn"]["vector"]
    assert knn["filter"]["bool"]["filter"] == [{"term": {"relationship_targets": "redis"}}]


def test_upsert_empty_is_noop():
    client = FakeOpenSearch()
    _store(client).upsert([])
    assert client.bulk_body is None


def test_upsert_raises_on_errors():
    client = FakeOpenSearch(bulk_errors=True)
    with pytest.raises(RuntimeError):
        _store(client).upsert([_chunk()])


def test_delete_by_document():
    client = FakeOpenSearch()
    _store(client).delete_by_document("github:o/r:issue:1")
    index, body = client.deleted[0]
    assert index == "ctx"
    assert body["query"]["term"]["document_id"] == "github:o/r:issue:1"


def test_query_builds_knn_and_parses_hits():
    hits = [
        {
            "_id": "github:o/r:issue:1#0",
            "_score": 0.87,
            "_source": {
                "document_id": "github:o/r:issue:1",
                "text": "hello",
                "title": "Bug",
                "url": "u",
                "source_type": "github",
                "kind": "issue",
                "repo": "o/r",
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
    assert results[0].chunk_id == "github:o/r:issue:1#0"
    assert results[0].score == 0.87
    assert results[0].repo == "o/r"


def test_query_rejects_unknown_filter_field():
    client = FakeOpenSearch()
    with pytest.raises(ValueError):
        _store(client).query([0.0] * 4, k=3, filters={"secret": "x"})


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
