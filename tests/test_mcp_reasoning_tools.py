import pytest

from aquifer.core.config import GitHubSettings, McpSettings, Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Embedder, VectorStore
from aquifer.core.models import DocumentKind, Entity, Relationship, SearchResult, SourceType
from aquifer.mcp.tools import AuthorizationError, ContextTools


class FakeEmbedder(Embedder):
    dimensions = 3

    def embed_documents(self, texts):
        return [[0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0, 0.0, 0.0]


class RoutingStore(VectorStore):
    """Returns canned results by which filter key is present; records every call."""

    def __init__(self, *, edges=None, mentions=None, by_document=None, by_type=None):
        self._edges = edges or []
        self._mentions = mentions or []
        self._by_document = by_document or []
        self._by_type = by_type or []
        self.calls = []

    def ensure_index(self):
        pass

    def upsert(self, chunks):
        pass

    def delete_by_document(self, document_id):
        pass

    def query(self, vector, k, filters=None):
        return []

    def get_by_document(self, document_id):
        return []

    def search_by_metadata(self, filters, k):
        self.calls.append(filters)
        if "relationship_targets" in filters:
            return self._edges
        if "document_id" in filters:
            return self._by_document
        if "entity_names" in filters:
            return self._mentions
        if "entity_types" in filters:
            return self._by_type
        return []


def _result(doc_id, *, title="", relationships=None, entities=None):
    return SearchResult(
        chunk_id=f"{doc_id}#0",
        document_id=doc_id,
        score=0.0,
        text="",
        title=title,
        url=f"https://x/{doc_id}",
        source_type=SourceType.GITHUB,
        kind=DocumentKind.PR,
        relationships=relationships or [],
        entities=entities or [],
    )


def _tools(store, interceptors=None):
    settings = Settings(github=GitHubSettings(repo_allowlist=["o/r"]), mcp=McpSettings(default_k=5))
    return ContextTools(FakeEmbedder(), store, interceptors=interceptors, settings=settings)


# --- find_related (retrieve relationships) --------------------------------

def test_find_related_merges_edges_and_mentions():
    edge_doc = _result(
        "github:o/r:pr:1",
        title="Add cache",
        relationships=[Relationship(type="depends_on", target="redis")],
    )
    # Same doc also surfaces as a mention, plus a second mention-only doc.
    mention_dup = _result("github:o/r:pr:1", title="Add cache")
    mention_doc = _result("github:o/r:readme:x", title="Redis usage")

    store = RoutingStore(edges=[edge_doc], mentions=[mention_dup, mention_doc])
    out = _tools(store).find_related("redis")

    assert out["entity"] == "redis"
    assert out["count"] == 2
    by_id = {item["document_id"]: item for item in out["related"]}
    # The PR matched both ways; the README only as a mention.
    assert by_id["github:o/r:pr:1"]["via"] == ["entity", "relationship"]
    assert by_id["github:o/r:readme:x"]["via"] == ["entity"]
    assert by_id["github:o/r:pr:1"]["relationships"][0]["target"] == "redis"


def test_find_related_queries_edges_then_entity_names():
    store = RoutingStore(edges=[], mentions=[])
    _tools(store).find_related("redis", relationship_types=["depends_on"])
    # First call is the edge query (target + type); second is the entity-name mention query.
    assert store.calls[0]["relationship_targets"] == "redis"
    assert store.calls[0]["relationship_types"] == ["depends_on"]
    assert store.calls[1] == {"entity_names": "redis"}


def test_find_related_authorization_denied():
    deny = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:DenyInterceptor"])
    with pytest.raises(AuthorizationError):
        _tools(RoutingStore(), interceptors=deny).find_related("redis")


# --- list_entities (neutral inventory) ------------------------------------

def test_list_entities_for_document_dedupes():
    e_service = Entity(type="service", name="billing-api")
    e_store = Entity(type="datastore", name="redis")
    # Two chunks of the same document repeat an entity → must dedupe.
    r1 = _result("doc1", entities=[e_service, e_store])
    r2 = _result("doc1", entities=[e_service])
    store = RoutingStore(by_document=[r1, r2])

    out = _tools(store).list_entities(document_id="doc1")
    assert out["count"] == 2
    names = {e["name"] for e in out["entities"]}
    assert names == {"billing-api", "redis"}
    assert store.calls[0]["document_id"] == "doc1"


def test_list_entities_filters_by_type():
    svc = Entity(type="service", name="billing-api")
    ds = Entity(type="datastore", name="redis")
    store = RoutingStore(by_type=[_result("doc1", entities=[svc, ds])])

    out = _tools(store).list_entities(entity_type="service")
    assert store.calls[0]["entity_types"] == "service"
    # Only the service survives the per-entity type filter.
    assert out["entities"] == [{"type": "service", "name": "billing-api"}]


def test_list_entities_authorization_denied():
    deny = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:DenyInterceptor"])
    with pytest.raises(AuthorizationError):
        _tools(RoutingStore(), interceptors=deny).list_entities(document_id="doc1")
