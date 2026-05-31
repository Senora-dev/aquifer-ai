import pytest

from aquifer.core.config import GitHubSettings, McpSettings, Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Embedder, VectorStore
from aquifer.core.models import SearchResult, SourceType
from aquifer.mcp.tools import AuthorizationError, ContextTools


class FakeEmbedder(Embedder):
    dimensions = 3

    def embed_documents(self, texts):
        return [[0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class FakeStore(VectorStore):
    def __init__(self, results=None, doc_sources=None):
        self._results = results or []
        self._doc_sources = doc_sources or []
        self.query_args = None

    def ensure_index(self):
        pass

    def upsert(self, chunks):
        pass

    def delete_by_document(self, document_id):
        pass

    def query(self, vector, k, filters=None):
        self.query_args = {"vector": vector, "k": k, "filters": filters}
        return self._results

    def get_by_document(self, document_id):
        return self._doc_sources

    def search_by_metadata(self, filters, k):
        return []


def _settings():
    return Settings(
        github=GitHubSettings(repo_allowlist=["o/r", "o/r2"]),
        mcp=McpSettings(default_k=7),
    )


def _tools(store, interceptors=None):
    return ContextTools(
        embedder=FakeEmbedder(),
        vector_store=store,
        interceptors=interceptors,
        settings=_settings(),
    )


def test_search_context_uses_default_k_and_returns_dicts():
    result = SearchResult(
        chunk_id="github:o/r:issue:1#0",
        document_id="github:o/r:issue:1",
        score=0.9,
        text="hello",
        url="u",
        source_type=SourceType.GITHUB,
    )
    store = FakeStore(results=[result])
    out = _tools(store).search_context("how does login work?")

    assert store.query_args["k"] == 7  # default_k from settings
    assert isinstance(out[0], dict)
    assert out[0]["chunk_id"] == "github:o/r:issue:1#0"
    assert out[0]["source_type"] == "github"


def test_search_context_passes_explicit_k_and_filters():
    store = FakeStore(results=[])
    _tools(store).search_context("q", k=3, filters={"repo": "o/r"})
    assert store.query_args["k"] == 3
    assert store.query_args["filters"] == {"repo": "o/r"}


def test_get_document_reassembles_chunks_in_order():
    sources = [
        {"text": "first", "title": "Bug", "url": "u", "source_type": "github",
         "kind": "issue", "repo": "o/r"},
        {"text": "second", "title": "Bug", "url": "u"},
    ]
    out = _tools(FakeStore(doc_sources=sources)).get_document("github:o/r:issue:1")
    assert out["found"] is True
    assert out["text"] == "first\nsecond"
    assert out["chunk_count"] == 2
    assert out["repo"] == "o/r"


def test_get_document_not_found():
    out = _tools(FakeStore(doc_sources=[])).get_document("missing")
    assert out == {"document_id": "missing", "found": False}


def test_list_sources_from_config():
    out = _tools(FakeStore()).list_sources()
    assert {"source_type": "github", "repo": "o/r"} in out
    assert len(out) == 2


def test_authorization_denied_blocks_search():
    deny = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:DenyInterceptor"])
    with pytest.raises(AuthorizationError):
        _tools(FakeStore(), interceptors=deny).search_context("q")


def test_before_and_after_query_interceptors_apply():
    r1 = SearchResult(chunk_id="c1", document_id="d", score=0.9, text="a")
    r2 = SearchResult(chunk_id="c2", document_id="d", score=0.8, text="b")
    store = FakeStore(results=[r1, r2])
    tag = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:TagInterceptor"])
    out = _tools(store, interceptors=tag).search_context("q")

    # before_query injected a filter...
    assert store.query_args["filters"] == {"tagged": True}
    # ...and after_query reversed the results.
    assert [r["chunk_id"] for r in out] == ["c2", "c1"]
