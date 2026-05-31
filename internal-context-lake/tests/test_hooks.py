import pytest

from aquifer.core.hooks import InterceptorPipeline, load_interceptor
from aquifer.core.models import (
    Document,
    DocumentKind,
    QueryContext,
    SearchResult,
    SourceType,
)

FIXTURES = "tests._fixtures_interceptors"


def _doc() -> Document:
    return Document(
        id="github:org/repo:issue:1",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.ISSUE,
    )


def test_empty_pipeline_is_passthrough():
    p = InterceptorPipeline()
    doc = _doc()
    assert p.before_ingest(doc) is doc
    ctx = QueryContext(query="hi")
    assert p.before_query(ctx) is ctx
    results = [SearchResult(chunk_id="c", document_id="d", score=1.0, text="t")]
    assert p.after_query(results, ctx) is results
    # No interceptors ⇒ allowed.
    assert p.authorize(None, "search", "*") is True


def test_load_interceptor_colon_and_dotted_forms():
    a = load_interceptor(f"{FIXTURES}:TagInterceptor")
    b = load_interceptor(f"{FIXTURES}.TagInterceptor")
    assert type(a) is type(b)


def test_load_interceptor_rejects_non_interceptor():
    with pytest.raises(TypeError):
        load_interceptor("builtins:dict")


def test_pipeline_runs_hooks():
    p = InterceptorPipeline.from_paths([f"{FIXTURES}:TagInterceptor"])

    doc = p.before_ingest(_doc())
    assert "tagged" in doc.labels

    ctx = p.before_query(QueryContext(query="hi"))
    assert ctx.filters.get("tagged") is True

    results = [
        SearchResult(chunk_id="c1", document_id="d", score=0.9, text="a"),
        SearchResult(chunk_id="c2", document_id="d", score=0.8, text="b"),
    ]
    out = p.after_query(results, ctx)
    assert [r.chunk_id for r in out] == ["c2", "c1"]


def test_authorize_deny_overrides():
    p = InterceptorPipeline.from_paths([f"{FIXTURES}:DenyInterceptor"])
    assert p.authorize("alice", "search", "repo") is False
