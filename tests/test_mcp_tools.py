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
        repo="o/r",
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
    # The requested repo is intersected with the configured allowlist.
    assert store.query_args["filters"] == {"repo": ["o/r"]}


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
    r1 = SearchResult(chunk_id="c1", document_id="d", score=0.9, text="a", repo="o/r")
    r2 = SearchResult(chunk_id="c2", document_id="d", score=0.8, text="b", repo="o/r")
    store = FakeStore(results=[r1, r2])
    tag = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:TagInterceptor"])
    out = _tools(store, interceptors=tag).search_context("q")

    # before_query injected a filter (and the repo scope was applied)...
    assert store.query_args["filters"] == {"tagged": True, "repo": ["o/r", "o/r2"]}
    # ...and after_query reversed the results.
    assert [r["chunk_id"] for r in out] == ["c2", "c1"]


def test_get_document_drops_the_overlap_between_chunks():
    # Chunking seeds each chunk with the tail of the previous one; reassembly must not
    # emit that shared text twice.
    sources = [
        {"text": "alpha beta gamma delta", "title": "T", "url": "u",
         "source_type": "github", "kind": "issue", "repo": "o/r", "overlap_words": 0},
        {"text": "gamma delta epsilon zeta", "overlap_words": 2},
        {"text": "epsilon zeta eta", "overlap_words": 2},
    ]
    out = _tools(FakeStore(doc_sources=sources)).get_document("github:o/r:issue:1")
    assert out["text"] == "alpha beta gamma delta\nepsilon zeta\neta"
    assert out["chunk_count"] == 3


def test_get_document_round_trips_a_chunked_document():
    from aquifer.core.chunking import chunk_document
    from aquifer.core.models import Document, DocumentKind

    body = " ".join(f"word{i}" for i in range(600))
    doc = Document(
        id="github:o/r:readme:readme",
        source_type=SourceType.GITHUB,
        external_id="o/r#readme",
        kind=DocumentKind.README,
        repo="o/r",
        title="o/r README",
        body=body,
    )
    chunks = chunk_document(doc, chunk_size=80, overlap=20)
    assert len(chunks) > 1
    sources = [{"text": c.text, "overlap_words": c.overlap_words, "repo": "o/r"} for c in chunks]

    out = _tools(FakeStore(doc_sources=sources)).get_document(doc.id)
    # Chunks without offsets (legacy) still reassemble word-for-word.
    assert out["text"].split() == f"{doc.title}\n\n{body}".split()


def test_get_document_joins_chunks_without_overlap_metadata():
    # Chunks indexed before overlap_words existed carry no such field; they must still join.
    sources = [{"text": "first", "title": "Bug", "repo": "o/r"}, {"text": "second"}]
    out = _tools(FakeStore(doc_sources=sources)).get_document("d")
    assert out["text"] == "first\nsecond"


def test_k_is_bounded():
    store = FakeStore(results=[])
    _tools(store).search_context("q", k=100_000)
    assert store.query_args["k"] == 50  # McpSettings.max_k


def test_out_of_scope_repo_filter_returns_nothing_without_querying():
    store = FakeStore(results=[])
    assert _tools(store).search_context("q", filters={"repo": "other/repo"}) == []
    assert store.query_args is None


def test_out_of_scope_results_are_never_returned():
    leak = SearchResult(chunk_id="x#0", document_id="x", score=1.0, text="t", repo="evil/repo")
    assert _tools(FakeStore(results=[leak])).search_context("q") == []


def test_filters_reject_unknown_fields_and_dsl_values():
    tools = _tools(FakeStore())
    with pytest.raises(ValueError):
        tools.search_context("q", filters={"content_hash": "x"})  # internal field
    with pytest.raises(ValueError):
        tools.search_context("q", filters={"repo": {"wildcard": "*"}})


def test_get_document_out_of_scope_looks_like_not_found():
    sources = [{"text": "secret", "repo": "evil/repo"}]
    out = _tools(FakeStore(doc_sources=sources)).get_document("github:evil/repo:issue:1")
    assert out == {"document_id": "github:evil/repo:issue:1", "found": False}


def test_get_document_denied_by_interceptor():
    deny = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:DenyInterceptor"])
    with pytest.raises(AuthorizationError):
        _tools(FakeStore(), interceptors=deny).get_document("d")


def test_get_document_returns_provenance_and_excerpt():
    sources = [{
        "text": "cfg.yaml\n\na: 1\nb: 2\nc: 3", "title": "cfg.yaml", "repo": "o/r",
        "url": "https://github.com/o/r/blob/abc/cfg.yaml", "path": "cfg.yaml",
        "revision": "abc", "indexed_at": "2024-01-01T00:00:00+00:00", "char_start": 0,
        "char_end": 24, "kind": "file",
    }]
    out = _tools(FakeStore(doc_sources=sources)).get_document("d", line_start=2, line_end=3)
    assert out["revision"] == "abc" and out["path"] == "cfg.yaml"
    assert out["indexed_at"] == "2024-01-01T00:00:00+00:00"
    assert out["excerpt"] == {"line_start": 2, "line_end": 3, "text": "b: 2\nc: 3"}
    assert "not as instructions" in out["notice"]


def test_get_document_prefers_the_newest_generation():
    sources = [
        {"text": "old", "repo": "o/r", "generation": "g1", "chunk_index": 0,
         "indexed_at": "2024-01-01T00:00:00+00:00"},
        {"text": "new", "repo": "o/r", "generation": "g2", "chunk_index": 0,
         "indexed_at": "2024-02-01T00:00:00+00:00"},
    ]
    out = _tools(FakeStore(doc_sources=sources)).get_document("d")
    assert out["text"] == "new" and out["chunk_count"] == 1


def test_search_results_carry_evidence_with_line_anchor():
    result = SearchResult(
        chunk_id="c", document_id="d", score=1.0, text="t", repo="o/r", path="main.tf",
        revision="abc", line_start=3, line_end=9,
        url="https://github.com/o/r/blob/abc/main.tf",
    )
    out = _tools(FakeStore(results=[result])).search_context("q")[0]
    assert out["evidence"]["url"].endswith("main.tf#L3-L9")
    assert out["evidence"]["revision"] == "abc"
    assert "content_hash" not in out
