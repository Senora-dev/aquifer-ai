from aquifer.core.config import SemanticIndexSettings, Settings
from aquifer.core.interfaces import SemanticIndexer
from aquifer.core.models import (
    Document,
    DocumentKind,
    Entity,
    Relationship,
    SemanticMetadata,
    SourceType,
)
from aquifer.semantic import interceptor as interceptor_module
from aquifer.semantic.interceptor import SemanticIndexInterceptor


class FakeIndexer(SemanticIndexer):
    def __init__(self, metadata=None, error=None):
        self._metadata = metadata
        self._error = error
        self.calls = 0

    def extract(self, document):
        self.calls += 1
        if self._error:
            raise self._error
        return self._metadata


def _doc():
    return Document(
        id="github:o/r:pr:1",
        source_type=SourceType.GITHUB,
        external_id="o/r#1",
        kind=DocumentKind.PR,
        title="t",
        body="b",
    )


def test_before_ingest_attaches_semantic_metadata():
    meta = SemanticMetadata(
        summary="s",
        entities=[Entity(type="service", name="billing-api")],
        relationships=[Relationship(type="depends_on", target="redis")],
    )
    icept = SemanticIndexInterceptor(indexer=FakeIndexer(metadata=meta))
    out = icept.before_ingest(_doc())
    assert out.semantic is meta
    assert out.semantic.entities[0].name == "billing-api"


def test_before_ingest_is_graceful_on_failure():
    icept = SemanticIndexInterceptor(indexer=FakeIndexer(error=RuntimeError("bedrock down")))
    out = icept.before_ingest(_doc())
    # Ingestion continues; the document is simply un-indexed semantically.
    assert out.semantic is None


def test_disabled_skips_indexing(monkeypatch):
    fake = FakeIndexer(metadata=SemanticMetadata(summary="s"))
    monkeypatch.setattr(
        interceptor_module,
        "get_settings",
        lambda: Settings(semantic_index=SemanticIndexSettings(enabled=False)),
    )
    out = SemanticIndexInterceptor(indexer=fake).before_ingest(_doc())
    assert out.semantic is None
    assert fake.calls == 0
