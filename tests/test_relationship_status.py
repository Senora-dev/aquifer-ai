"""What a source mentions vs claims vs proposes vs what is current (regression tests).

The LLM is replaced by a scripted indexer returning what a correct extraction looks like, so
these tests pin down Aquifer's own classification and aggregation. Extraction quality of the real
model is measured by the benchmark in ``--bedrock`` mode.
"""

from datetime import UTC, datetime

import pytest

from aquifer.core.config import GitHubSettings, McpSettings, Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import SemanticIndexer
from aquifer.core.models import (
    Document,
    DocumentKind,
    Relationship,
    SemanticMetadata,
    SourceType,
)
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.mcp.tools import ContextTools
from aquifer.semantic.interceptor import SemanticIndexInterceptor
from aquifer.semantic.status import aggregate, classify
from aquifer.vectorstore.memory import InMemoryVectorStore

REPO = "acme/payments"


def _doc(kind, ext, body, *, state=None, updated="2024-03-01T00:00:00+00:00", title=None,
         path=None, merged_at=None):
    return Document(
        id=Document.make_id(SourceType.GITHUB, REPO, kind, ext), source_type=SourceType.GITHUB,
        external_id=ext, kind=kind, repo=REPO, title=title or ext, body=body, state=state,
        path=path, revision="c1" if path else None,
        updated_at=datetime.fromisoformat(updated),
        metadata={"merged_at": merged_at} if merged_at else {},
    )


def _rel(target, assertion, quote="", source="billing-api"):
    return Relationship(type="depends_on", source=source, target=target, assertion=assertion,
                        quote=quote)


# --- classification table -----------------------------------------------------------------

@pytest.mark.parametrize(("kind", "state", "assertion", "expected"), [
    # merged PR: what the change did
    (DocumentKind.PR, "merged", "added", "claimed"),
    (DocumentKind.PR, "merged", "removed", "removed"),
    (DocumentKind.PR, "merged", "proposed", "proposed"),
    # open PR: nothing is in effect yet
    (DocumentKind.PR, "open", "added", "proposed"),
    (DocumentKind.PR, "open", "removed", "proposed"),
    (DocumentKind.PR, "open", "present", "proposed"),
    # closed without merging
    (DocumentKind.PR, "closed", "added", "not_merged"),
    (DocumentKind.PR, "closed", "removed", "not_merged"),
    # issues / docs
    (DocumentKind.ISSUE, "open", "present", "claimed"),
    (DocumentKind.ISSUE, "open", "negated", "absent"),
    (DocumentKind.README, None, "discussed", "discussed"),
    (DocumentKind.FILE, None, "proposed", "proposed"),
    # an unknown assertion is never a claim
    (DocumentKind.ISSUE, "open", "maybe-ish", "discussed"),
])
def test_status_from_assertion_and_document(kind, state, assertion, expected):
    rel = classify(_rel("x", assertion), _doc(kind, "1", "body", state=state))
    assert rel.status == expected


def test_parsed_configuration_is_the_only_current_status():
    explicit = Relationship(type="depends_on", target="x", origin="explicit_reference")
    assert classify(explicit, _doc(DocumentKind.FILE, "f", "x", path="c.yml")).status == "current"
    llm = _rel("x", "present")
    assert classify(llm, _doc(DocumentKind.FILE, "f", "x", path="c.yml")).status == "claimed"


def test_merged_pr_statement_time_is_the_merge_time():
    rel = classify(_rel("x", "added"), _doc(DocumentKind.PR, "2", "b", state="merged",
                                            merged_at="2024-02-03T12:00:00Z"))
    assert rel.as_of == datetime(2024, 2, 3, 12, tzinfo=UTC)


# --- aggregation across evidence ---------------------------------------------------------

def test_current_configuration_is_not_overridden_by_a_document_claim():
    status, conflicting = aggregate([
        {"status": "current", "as_of": "2024-01-01"},
        {"status": "removed", "as_of": "2025-01-01"},  # newer PR says removed
    ])
    assert (status, conflicting) == ("current", True)


def test_latest_claim_wins_without_configuration():
    assert aggregate([{"status": "claimed", "as_of": "2024-01-01"},
                      {"status": "removed", "as_of": "2024-06-01"}]) == ("removed", True)
    assert aggregate([{"status": "removed", "as_of": "2024-01-01"},
                      {"status": "claimed", "as_of": "2024-06-01"}]) == ("claimed", True)


def test_proposals_and_discussion_never_become_claims():
    assert aggregate([{"status": "discussed"}, {"status": "proposed"}]) == ("proposed", False)
    assert aggregate([{"status": "not_merged"}]) == ("not_merged", False)


# --- end to end through ingestion and find_related ------------------------------------------

COMPOSE = "services:\n  billing-api:\n    depends_on:\n      - payments-db\n      - redis\n"
PR_MOVE = ("billing-api now uses redis for its session cache instead of memcached.\n"
           "The memcached cluster can be decommissioned after this ships.")
PR_PROPOSAL = "Draft: billing-api would call fraud-check synchronously before each charge."
PR_REJECTED = "Switch billing-api to kafka for invoice events."
ISSUE_NEGATION = "To be clear: billing-api does not depend on ledger-db."
ADR_DISCUSSION = "Should billing-api use dynamodb? One option is dynamodb for idempotency keys."


class ScriptedIndexer(SemanticIndexer):
    """What a correct extraction of each document looks like."""

    def __init__(self):
        self.by_ext = {
            "move": [_rel("memcached", "removed", PR_MOVE.splitlines()[0]),
                     _rel("redis", "added", PR_MOVE.splitlines()[0])],
            "proposal": [_rel("fraud-check", "proposed", PR_PROPOSAL)],
            "rejected": [_rel("kafka", "added", PR_REJECTED)],
            "negation": [_rel("ledger-db", "negated", ISSUE_NEGATION)],
            "adr": [_rel("dynamodb", "discussed", ADR_DISCUSSION.split("?")[0] + "?")],
            "old-claim": [_rel("memcached", "present", "billing-api caches sessions in "
                                                       "memcached")],
        }

    def extract(self, document):
        rels = [r.model_copy() for r in self.by_ext.get(document.external_id, [])]
        return SemanticMetadata(relationships=rels)


@pytest.fixture
def tools():
    store = InMemoryVectorStore()
    embedder = HashingEmbedder(64)
    pipe = IngestionPipeline(
        connector=None, embedder=embedder, vector_store=store,
        interceptors=InterceptorPipeline([SemanticIndexInterceptor(indexer=ScriptedIndexer())]),
        chunk_size=200, chunk_overlap=20)
    docs = [
        _doc(DocumentKind.FILE, "compose", COMPOSE, path="docker-compose.yml"),
        _doc(DocumentKind.ISSUE, "old-claim", "billing-api caches sessions in memcached",
             state="open", updated="2023-05-01T00:00:00+00:00"),
        _doc(DocumentKind.PR, "move", PR_MOVE, state="merged",
             merged_at="2024-02-03T12:00:00Z", updated="2024-02-03T12:00:00+00:00"),
        _doc(DocumentKind.PR, "proposal", PR_PROPOSAL, state="open"),
        _doc(DocumentKind.PR, "rejected", PR_REJECTED, state="closed"),
        _doc(DocumentKind.ISSUE, "negation", ISSUE_NEGATION, state="open"),
        _doc(DocumentKind.README, "adr", ADR_DISCUSSION),
    ]
    for d in docs:
        pipe.process_document(d)
    settings = Settings(github=GitHubSettings(repo_allowlist=[REPO]), mcp=McpSettings())
    return ContextTools(embedder, store, settings=settings), pipe, store


def _by_target(out):
    return {e["target"]: e for e in out["relationships"]}


def test_removed_dependency_is_never_presented_as_current(tools):
    t, _, _ = tools
    edges = _by_target(t.find_related("billing-api", direction="outgoing"))
    memcached = edges["memcached"]
    # An older issue claimed it; the newer merged PR removed it.
    assert memcached["status"] == "removed"
    assert memcached["conflicting_evidence"] is True
    assert memcached["support"]["removed"] == 1 and memcached["support"]["claimed"] == 1
    removal = next(e for e in memcached["evidence"] if e["status"] == "removed")
    assert removal["assertion"] == "removed" and removal["source_state"] == "merged"
    assert removal["quote_verified"] is True and removal["as_of"].startswith("2024-02-03")


def test_replacement_adds_the_new_dependency(tools):
    t, _, _ = tools
    redis = _by_target(t.find_related("billing-api", direction="outgoing"))["redis"]
    # Parsed configuration lists redis; the merged PR agrees.
    assert redis["status"] == "current" and redis["conflicting_evidence"] is False
    assert set(redis["origins"]) == {"explicit_reference", "llm_extracted"}


@pytest.mark.parametrize(("target", "status"), [
    ("fraud-check", "proposed"),     # open PR
    ("kafka", "not_merged"),         # closed, unmerged PR
    ("ledger-db", "absent"),         # explicit negation
    ("dynamodb", "discussed"),       # hypothetical
])
def test_non_facts_keep_their_status(tools, target, status):
    t, _, _ = tools
    assert _by_target(t.find_related("billing-api"))[target]["status"] == status


def test_claims_contradicting_current_configuration_are_flagged(tools):
    t, _, _ = tools
    edges = _by_target(t.find_related("billing-api", direction="outgoing"))
    # docker-compose lists billing-api's dependencies; a proposal for another target is not
    # part of it.
    assert edges["fraud-check"].get("absent_from_current_config") is True
    assert "absent_from_current_config" not in edges["payments-db"]


def test_statuses_filter_returns_only_current_configuration(tools):
    t, _, _ = tools
    out = t.find_related("billing-api", direction="outgoing", statuses=["current"])
    assert {e["target"] for e in out["relationships"]} == {"payments-db", "redis"}
    assert out["evidence_status"] == "current"


def test_current_edges_are_listed_first(tools):
    t, _, _ = tools
    statuses = [e["status"] for e in t.find_related("billing-api")["relationships"]]
    assert statuses[0] == "current"
    assert statuses.index("current") < statuses.index("proposed")


def test_removal_in_configuration_wins_over_old_merged_claim(tools):
    """When the configuration drops a dependency, an old PR that added it stays a claim."""
    t, pipe, _ = tools
    newer = COMPOSE.replace("      - redis\n", "      - valkey\n")
    pipe.process_document(_doc(DocumentKind.FILE, "compose", newer, path="docker-compose.yml",
                               updated="2024-09-01T00:00:00+00:00"))
    edges = _by_target(t.find_related("billing-api", direction="outgoing"))
    assert edges["valkey"]["status"] == "current"
    redis = edges["redis"]
    assert redis["status"] == "claimed"  # only the merged PR states it now
    assert redis.get("absent_from_current_config") is True
    assert not any(e.get("path") == "docker-compose.yml" for e in redis["evidence"])
