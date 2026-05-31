"""Offline coverage for the eval harness: validates the dataset and the scoring logic.

Uses a fake indexer so CI exercises the harness without any Bedrock calls.
"""

from pathlib import Path

from aquifer.core.interfaces import SemanticIndexer
from aquifer.core.models import Entity, Relationship, SemanticMetadata
from aquifer.semantic.eval import evaluate, load_examples, score_metadata

DATA_DIR = Path(__file__).parent / "eval_data"


class FakeIndexer(SemanticIndexer):
    def extract(self, document):
        return SemanticMetadata(entities=[Entity(type="service", name="x")])


def test_dataset_loads_and_builds_valid_documents():
    examples = load_examples(DATA_DIR)
    assert len(examples) >= 8
    for ex in examples:
        # Every golden example must build a valid Document with neutral golden fields.
        assert ex.document.id
        assert ex.document.external_id
        assert "entities" in ex.golden
        assert "relationship_targets" in ex.golden


def test_score_metadata_computes_recall_and_misses():
    meta = SemanticMetadata(
        entities=[Entity(type="service", name="billing-api")],
        relationships=[Relationship(type="depends_on", target="redis")],
    )
    golden = {"entities": ["billing-api", "redis"], "relationship_targets": ["redis"]}
    score = score_metadata(meta, golden)

    assert score["entity_recall"] == 0.5
    assert score["missing_entities"] == ["redis"]
    assert score["relationship_recall"] == 1.0
    assert score["missing_relationship_targets"] == []


def test_score_metadata_recall_is_none_when_nothing_expected():
    score = score_metadata(SemanticMetadata(), {"entities": [], "relationship_targets": []})
    assert score["entity_recall"] is None
    assert score["relationship_recall"] is None


def test_evaluate_runs_and_selects_prompt():
    examples = load_examples(DATA_DIR)[:3]
    results = evaluate(FakeIndexer(), examples)
    assert len(results) == 3
    # Prompt selection is wired (e.g. github PRs → github-code, jira → jira-task).
    assert all(r.template for r in results)
    assert all("entity_recall" in r.score for r in results)
