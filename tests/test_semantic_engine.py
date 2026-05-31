from aquifer.core.config import SemanticIndexSettings
from aquifer.core.interfaces import Inferencer
from aquifer.core.models import Document, DocumentKind, SourceType
from aquifer.semantic.engine import LlmSemanticIndexer


class FakeInferencer(Inferencer):
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def infer_json(self, system, user, schema=None):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._payload


def _doc(kind=DocumentKind.PR, source=SourceType.GITHUB):
    return Document(
        id="d",
        source_type=source,
        external_id="o/r#1",
        kind=kind,
        repo="o/r",
        title="Add Redis cache to billing-api",
        body="Introduces a dependency on redis.",
    )


def _indexer(payload):
    return LlmSemanticIndexer(
        FakeInferencer(payload),
        settings=SemanticIndexSettings(max_input_chars=5000),
    )


def test_extract_parses_neutral_metadata():
    payload = {
        "summary": "Adds Redis caching to billing-api.",
        "entities": [
            {"type": "service", "name": "billing-api"},
            {"type": "datastore", "name": "redis"},
        ],
        "topics": ["caching"],
        "relationships": [
            {"type": "depends_on", "target": "redis", "description": "new cache dependency"},
            {"type": "modifies", "target": "billing-api"},
        ],
    }
    meta = _indexer(payload).extract(_doc())

    assert [e.name for e in meta.entities] == ["billing-api", "redis"]
    assert meta.entities[0].type == "service"
    assert meta.relationships[0].type == "depends_on"
    assert meta.relationships[1].target == "billing-api"
    # Missing description defaults to empty.
    assert meta.relationships[1].description == ""


def test_extract_handles_minimal_payload_with_defaults():
    meta = _indexer({"summary": "small"}).extract(_doc())
    assert meta.summary == "small"
    assert meta.entities == []
    assert meta.relationships == []


def test_extract_selects_prompt_by_source_and_kind():
    # A PR uses the github-code instructions...
    fake = FakeInferencer({"summary": "s"})
    LlmSemanticIndexer(fake, settings=SemanticIndexSettings()).extract(_doc(kind=DocumentKind.PR))
    assert "pull request or source change" in fake.calls[0]["user"]

    # ...a Jira item uses the jira-task instructions.
    fake_jira = FakeInferencer({"summary": "s"})
    LlmSemanticIndexer(fake_jira, settings=SemanticIndexSettings()).extract(
        _doc(kind=DocumentKind.ISSUE, source=SourceType.JIRA)
    )
    assert "Jira work item" in fake_jira.calls[0]["user"]
