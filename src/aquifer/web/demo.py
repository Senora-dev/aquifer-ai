"""The labeled demo workspace: the synthetic ``acme/*`` repositories, searchable before anything
private is connected.

It runs in its own in-memory index with its own scope, so demo content never mixes with the
deployment's index and the deployment's sources are never visible from it. Indexing uses the
deterministic extractors and the local lexical embedder only: the demo makes no model calls and
costs nothing. Its URLs are fictional; the UI says so and opens the in-app source view instead.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from aquifer.core.config import Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Connector, Interceptor
from aquifer.core.models import (
    Document,
    DocumentKind,
    ExtractionStatus,
    FetchJob,
    Reconciliation,
    Source,
    SourceType,
)

CORPUS_PATH = Path(__file__).with_name("demo_corpus.json")
NOTE = ("Synthetic demo data: the acme organization, its repositories, revisions and URLs are "
        "fictional.")
SUGGESTIONS = [
    "What depends on billing-api?",
    "Why did we move from memcached to redis?",
    "Why does the ledger use Postgres?",
    "Did we move invoice events to Kafka?",
]


def build_corpus(root: Path) -> dict:
    """Read the demo repositories with the local connector (used by the build script)."""
    from aquifer.local.connector import LocalRepoConnector

    connector = LocalRepoConnector.from_root(root)
    source = Source(id="demo", type=SourceType.GITHUB, config={"repos": connector.repos})
    docs = []
    for job in connector.discover(source):
        found, _, _ = connector.fetch_with_reconciliation(job)
        docs += [d.model_dump(mode="json", exclude={"fetched_at"}, exclude_none=True)
                 for d in found]
    return {"note": NOTE, "repos": connector.repos, "documents": docs}


class _CorpusConnector(Connector):
    source_type = SourceType.GITHUB.value

    def __init__(self, docs: list[Document]) -> None:
        self.docs = docs

    def discover(self, source: Source) -> Iterable[FetchJob]:
        for repo in sorted({d.repo for d in self.docs}):
            yield FetchJob(source_id="demo", source_type=SourceType.GITHUB, repo=repo,
                           kind=DocumentKind.FILE)

    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        return [d for d in self.docs if d.repo == job.repo], None

    def fetch_with_reconciliation(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        docs, successor = self.fetch(job)
        return docs, successor, None


class _StructuredOnly(Interceptor):
    """Deterministic extraction only, whatever the deployment's model settings are."""

    def before_ingest(self, document: Document) -> Document:
        from aquifer.semantic.structured import extract_structured, merge_metadata

        structured = extract_structured(document)
        merged = merge_metadata(structured, None)
        if merged is not None:
            document.semantic = merged
        document.extraction_status = (ExtractionStatus.OK.value if structured is not None
                                      else ExtractionStatus.DISABLED.value)
        return document


class DemoWorkspace:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._tools = None
        self.repos: list[str] = []
        self._summary: list[dict] = []

    @property
    def available(self) -> bool:
        return self.settings.web.demo_enabled and CORPUS_PATH.is_file()

    @property
    def tools(self):  # noqa: ANN201
        with self._lock:
            if self._tools is None:
                self._tools = self._build()
            return self._tools

    def summary(self) -> list[dict]:
        """Per repository: how many demo documents were indexed, by kind."""
        self.tools  # noqa: B018 - builds the index on first use
        return self._summary

    def _build(self):  # noqa: ANN202
        from aquifer.embedding.hashing import HashingEmbedder
        from aquifer.ingestion.pipeline import IngestionPipeline
        from aquifer.mcp.tools import ContextTools
        from aquifer.vectorstore.memory import InMemoryVectorStore

        corpus = json.loads(CORPUS_PATH.read_text())
        docs = [Document.model_validate(d) for d in corpus["documents"]]
        self.repos = list(corpus["repos"])
        settings = self.settings.model_copy(deep=True)
        settings.github.repo_allowlist = self.repos
        store = InMemoryVectorStore(None)
        embedder = HashingEmbedder(settings.embedding.dimensions)
        connector = _CorpusConnector(docs)
        pipeline = IngestionPipeline(connector=connector, embedder=embedder, vector_store=store,
                                     interceptors=InterceptorPipeline([_StructuredOnly()]),
                                     chunk_size=settings.ingestion.chunk_size,
                                     chunk_overlap=settings.ingestion.chunk_overlap)
        source = Source(id="demo", type=SourceType.GITHUB, config={"repos": self.repos})
        for job in connector.discover(source):
            pipeline.run_job(job)
        self._summary = [
            {"repo": repo,
             "documents": sum(1 for d in docs if d.repo == repo),
             "kinds": dict(sorted(Counter(DocumentKind(d.kind).value
                                          for d in docs if d.repo == repo).items()))}
            for repo in self.repos]
        return ContextTools(embedder=embedder, vector_store=store,
                            interceptors=InterceptorPipeline(), settings=settings)
