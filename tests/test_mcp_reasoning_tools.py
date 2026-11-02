"""find_related / list_entities against a real (in-memory) index built by the real pipeline."""

import pytest

from aquifer.core.config import GitHubSettings, McpSettings, Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import SemanticIndexer
from aquifer.core.models import (
    Document,
    DocumentKind,
    Entity,
    Relationship,
    SemanticMetadata,
    SourceType,
)
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.mcp.tools import AuthorizationError, ContextTools
from aquifer.semantic.interceptor import SemanticIndexInterceptor
from aquifer.vectorstore.memory import InMemoryVectorStore

COMPOSE = """services:
  billing-api:
    image: acme/billing
    depends_on:
      - payments-db
  checkout:
    depends_on:
      - billing-api
"""


class ScriptedIndexer(SemanticIndexer):
    """Stands in for the LLM: returns fixed metadata per document id."""

    def __init__(self, by_id):
        self.by_id = by_id

    def extract(self, document):
        return self.by_id.get(document.id, SemanticMetadata())


def _doc(repo, kind, ext, body, path=None, title=None):
    return Document(
        id=Document.make_id(SourceType.GITHUB, repo, kind, ext),
        source_type=SourceType.GITHUB, external_id=ext, kind=kind, repo=repo,
        title=title or path or ext, body=body, path=path,
        revision="c1" if path else None,
        url=f"https://github.com/{repo}/blob/c1/{path}" if path else f"https://x/{ext}",
    )


@pytest.fixture
def lake():
    store = InMemoryVectorStore()
    compose = _doc("acme/shop", DocumentKind.FILE, "compose", COMPOSE, path="docker-compose.yml")
    issue = _doc("acme/shop", DocumentKind.ISSUE, "7",
                 "Reports service now depends on Billing-API for invoices.\nThanks",
                 title="Reports needs invoices")
    hallucinated = _doc("acme/shop", DocumentKind.ISSUE, "8", "Unrelated text about caching.")
    other_repo = _doc("acme/legacy", DocumentKind.ISSUE, "1", "billing-api depends on oracle")
    indexer = ScriptedIndexer({
        issue.id: SemanticMetadata(
            entities=[Entity(type="service", name="reports")],
            relationships=[Relationship(type="depends_on", source="reports",
                                        target="billing-api")]),
        hallucinated.id: SemanticMetadata(
            relationships=[Relationship(type="depends_on", source="cache",
                                        target="billing-api")]),
        other_repo.id: SemanticMetadata(
            entities=[Entity(type="service", name="billing-api")],
            relationships=[Relationship(type="depends_on", source="billing-api",
                                        target="oracle")]),
    })
    seam = InterceptorPipeline([SemanticIndexInterceptor(indexer=indexer)])
    embedder = HashingEmbedder(64)
    pipe = IngestionPipeline(connector=None, embedder=embedder, vector_store=store,
                             interceptors=seam, chunk_size=200, chunk_overlap=20)
    for d in (compose, issue, hallucinated, other_repo):
        pipe.process_document(d)
    return store, embedder


def _tools(lake, repos=("acme/shop", "acme/legacy"), interceptors=None):
    store, embedder = lake
    settings = Settings(github=GitHubSettings(repo_allowlist=list(repos)),
                        mcp=McpSettings(default_k=10))
    return ContextTools(embedder=embedder, vector_store=store, interceptors=interceptors,
                        settings=settings)


def _edges(out):
    return {(e["direction"], e["source"], e["type"], e["target"]): e
            for e in out["relationships"]}


def test_find_related_returns_directional_edges_with_evidence(lake):
    out = _tools(lake).find_related("billing-api")
    edges = _edges(out)

    explicit_in = edges[("incoming", "checkout", "depends_on", "billing-api")]
    assert explicit_in["origins"] == ["explicit_reference"]
    ev = explicit_in["evidence"][0]
    assert (ev["path"], ev["revision"], ev["line_start"]) == ("docker-compose.yml", "c1", 8)
    assert ev["url"].endswith("docker-compose.yml#L8")

    outgoing = edges[("outgoing", "billing-api", "depends_on", "payments-db")]
    assert outgoing["support"]["explicit"] == 1

    # Case-insensitive match; LLM edge found in the text → grounded with a text_match line.
    llm = edges[("incoming", "reports", "depends_on", "billing-api")]
    assert llm["origins"] == ["llm_extracted"]
    assert llm["support"] == {"documents": 1, "explicit": 0, "llm_grounded": 1,
                              "llm_ungrounded": 0}
    assert llm["evidence"][0]["locator"] == "text_match"
    assert llm["evidence"][0]["line_start"] == 1

    # LLM edge whose target is not in the text is kept but clearly flagged.
    flagged = edges[("incoming", "cache", "depends_on", "billing-api")]
    assert flagged["support"]["llm_ungrounded"] == 1
    assert flagged["evidence"][0]["line_start"] is None
    assert out["evidence_status"] == "explicit"


def test_explicit_edges_rank_first(lake):
    out = _tools(lake).find_related("billing-api")
    assert out["relationships"][0]["support"]["explicit"] >= 1


def test_typed_query_and_direction(lake):
    out = _tools(lake).find_related("service:billing-api", direction="incoming")
    assert all(e["direction"] == "incoming" for e in out["relationships"])
    assert out["query"]["type"] == "service"


def test_repo_filter_prevents_cross_repository_results(lake):
    out = _tools(lake).find_related("billing-api", repo="acme/shop")
    assert {r["repo"] for r in out["related"]} == {"acme/shop"}
    assert ("outgoing", "billing-api", "depends_on", "oracle") not in _edges(out)


def test_allowlist_hides_repositories_outside_the_trust_domain(lake):
    out = _tools(lake, repos=("acme/shop",)).find_related("oracle")
    assert out["relationships"] == [] and out["related"] == []
    assert out["evidence_status"] == "none"


def test_missing_entity_returns_qualified_empty_result(lake):
    out = _tools(lake).find_related("does-not-exist")
    assert out["count"] == 0 and out["relationships"] == []
    assert out["evidence_status"] == "none"


def test_similar_names_are_not_merged(lake):
    store, embedder = lake
    pipe = IngestionPipeline(connector=None, embedder=embedder, vector_store=store,
                             chunk_size=200, chunk_overlap=20)
    pipe.process_document(_doc("acme/shop", DocumentKind.FILE, "c2",
                               "services:\n  billing-api-v2:\n    depends_on: [ledger]\n",
                               path="v2/docker-compose.yml"))
    out = _tools(lake).find_related("billing-api")
    assert all(e["target"] != "ledger" for e in out["relationships"])


def test_relationship_type_filter(lake):
    out = _tools(lake).find_related("billing-api", relationship_types=["mentions"])
    assert out["relationships"] == []


def test_find_related_authorization(lake):
    deny = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:DenyInterceptor"])
    with pytest.raises(AuthorizationError):
        _tools(lake, interceptors=deny).find_related("billing-api")


def test_find_related_runs_query_interceptors(lake):
    # The scoping interceptor from docs/contributing.md must apply to every tool, not just search.
    scope = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:RepoScopeInterceptor"])
    out = _tools(lake, interceptors=scope).find_related("billing-api")
    assert {r["repo"] for r in out["related"]} == {"acme/legacy"}


def test_list_entities_inventory_is_aggregated_and_typed(lake):
    out = _tools(lake).list_entities(entity_type="service", k=50)
    names = {(e["type"], e["name"]) for e in out["entities"]}
    assert ("service", "billing-api") in names and ("service", "checkout") in names
    assert all(e["type"] == "service" for e in out["entities"])
    billing = next(e for e in out["entities"] if e["name"] == "billing-api")
    assert billing["documents"] == 2  # compose file + legacy issue


def test_list_entities_for_a_document(lake):
    doc_id = Document.make_id(SourceType.GITHUB, "acme/shop", DocumentKind.FILE, "compose")
    out = _tools(lake).list_entities(document_id=doc_id)
    assert {e["name"] for e in out["entities"]} == {"billing-api", "payments-db", "checkout"}
    assert out["scope"]["document_id"] == doc_id


def test_list_entities_respects_scope(lake):
    out = _tools(lake, repos=("acme/legacy",)).list_entities(k=50)
    assert {e["name"] for e in out["entities"]} == {"billing-api"}


def test_list_sources_reports_index_freshness(lake):
    out = _tools(lake).list_sources()
    shop = next(s for s in out if s["repo"] == "acme/shop")
    assert shop["indexed_documents"] == 3
    assert "last_indexed_at" in shop
