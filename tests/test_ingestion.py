from aquifer.core.hooks import InterceptorPipeline, load_interceptor
from aquifer.core.interfaces import Connector, Embedder, SemanticIndexer, VectorStore
from aquifer.core.models import (
    Document,
    DocumentKind,
    Entity,
    FetchJob,
    Relationship,
    SemanticMetadata,
    SourceType,
)
from aquifer.ingestion.pipeline import IngestionPipeline
from aquifer.semantic.interceptor import SemanticIndexInterceptor


class FakeEmbedder(Embedder):
    dimensions = 3

    def embed_documents(self, texts):
        return [[float(i), 0.0, 0.0] for i, _ in enumerate(texts)]

    def embed_query(self, text):
        return [0.0, 0.0, 0.0]


class FakeStore(VectorStore):
    def __init__(self):
        self.calls = []
        self.upserted = []

    def ensure_index(self):
        self.calls.append("ensure_index")

    def upsert(self, chunks):
        self.calls.append("upsert")
        self.upserted.extend(chunks)

    def delete_by_document(self, document_id):
        self.calls.append(("delete", document_id))

    def query(self, vector, k, filters=None):
        return []

    def get_by_document(self, document_id):
        return []

    def search_by_metadata(self, filters, k):
        return []


class FakeConnector(Connector):
    source_type = "github"

    def __init__(self, docs, successor=None):
        self._docs = docs
        self._successor = successor

    def discover(self, source):
        return []

    def fetch(self, job):
        return self._docs, self._successor


def _doc(body="some body text", title="Title"):
    return Document(
        id="github:o/r:issue:1",
        source_type=SourceType.GITHUB,
        external_id="o/r#1",
        kind=DocumentKind.ISSUE,
        repo="o/r",
        title=title,
        body=body,
    )


def _pipeline(connector, store, interceptors=None):
    return IngestionPipeline(
        connector=connector,
        embedder=FakeEmbedder(),
        vector_store=store,
        interceptors=interceptors,
        chunk_size=100,
        chunk_overlap=10,
    )


def test_process_document_deletes_then_upserts_with_embeddings():
    store = FakeStore()
    _pipeline(FakeConnector([]), store).process_document(_doc())

    # Delete must precede upsert so stale chunks never linger.
    assert store.calls == [("delete", "github:o/r:issue:1"), "upsert"]
    assert len(store.upserted) == 1
    assert store.upserted[0].embedding == [0.0, 0.0, 0.0]


def test_process_document_skips_empty():
    store = FakeStore()
    _pipeline(FakeConnector([]), store).process_document(_doc(body="", title=""))
    assert store.calls == []  # nothing to embed or upsert


def test_process_job_returns_successor():
    successor = FetchJob(
        source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
        kind=DocumentKind.ISSUE, cursor="2",
    )
    store = FakeStore()
    out = _pipeline(FakeConnector([_doc()], successor), store).process_job(
        FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r", kind=DocumentKind.ISSUE)
    )
    assert out is successor
    assert len(store.upserted) == 1


def test_before_ingest_interceptor_is_applied():
    store = FakeStore()
    interceptors = InterceptorPipeline.from_paths(["tests._fixtures_interceptors:TagInterceptor"])
    _pipeline(FakeConnector([]), store, interceptors).process_document(_doc())
    assert "tagged" in store.upserted[0].labels


class _FakeIndexer(SemanticIndexer):
    def extract(self, document):
        return SemanticMetadata(
            entities=[Entity(type="service", name="billing-api")],
            relationships=[Relationship(type="depends_on", target="redis")],
        )


def test_semantic_metadata_flows_through_ingest_into_indexed_chunks():
    store = FakeStore()
    interceptors = InterceptorPipeline([SemanticIndexInterceptor(indexer=_FakeIndexer())])
    _pipeline(FakeConnector([]), store, interceptors).process_document(_doc())

    # before_ingest indexed the document → chunks carry the neutral, queryable metadata.
    assert store.upserted[0].entities[0].name == "billing-api"
    assert store.upserted[0].relationships[0].target == "redis"


def test_semantic_interceptor_loads_by_dotted_path():
    # The path wired into the CDK worker env must resolve without touching AWS.
    icept = load_interceptor("aquifer.semantic.interceptor:SemanticIndexInterceptor")
    assert isinstance(icept, SemanticIndexInterceptor)


def test_fetchjob_json_roundtrip():
    job = FetchJob(
        source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
        kind=DocumentKind.PR, cursor="3",
    )
    restored = FetchJob.model_validate_json(job.model_dump_json())
    assert restored == job
