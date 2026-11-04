"""The interfaces that make Aquifer modular.

Each layer of the system depends only on these abstractions, never on a concrete adapter.
Concrete implementations (GitHub, Bedrock, OpenSearch, enterprise interceptors) are selected
at the edges via :mod:`aquifer.core.config`, so swapping a vector store or adding a connector
never ripples into the pipeline or the MCP server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

from aquifer.core.models import (
    Chunk,
    Document,
    FetchJob,
    IndexedState,
    QueryContext,
    Reconciliation,
    SearchResult,
    SemanticMetadata,
    Source,
)


class Connector(ABC):
    """Pulls context out of an upstream source and normalizes it to ``Document`` objects."""

    source_type: str

    @abstractmethod
    def discover(self, source: Source) -> Iterable[FetchJob]:
        """Enumerate the initial fetch jobs for a source (e.g. one per repo/kind)."""

    @abstractmethod
    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        """Fetch one page for ``job``.

        Returns the normalized documents for this page and a successor ``FetchJob`` to
        continue pagination, or ``None`` when the source is exhausted for this job.
        """

    def fetch_with_reconciliation(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        """Like :meth:`fetch`, plus an optional statement of which documents still exist.

        Override to propagate deletions. The default reports nothing, so connectors written
        against the original interface keep working unchanged.
        """
        documents, successor = self.fetch(job)
        return documents, successor, None


class Embedder(ABC):
    """Turns text into vectors. Document and query embeddings may differ per provider."""

    dimensions: int

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of chunk texts. Order of the result matches the input."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single search query."""


class VectorStore(ABC):
    """Persists and searches embedded chunks."""

    @abstractmethod
    def ensure_index(self) -> None:
        """Create the vector index if it does not already exist (idempotent)."""

    @abstractmethod
    def upsert(self, chunks: Sequence[Chunk]) -> None:
        """Insert or replace chunks. Implementations must be idempotent on ``chunk_id``."""

    @abstractmethod
    def delete_by_document(self, document_id: str) -> None:
        """Remove all chunks belonging to a document (used before re-ingesting an edit)."""

    @abstractmethod
    def query(
        self, vector: Sequence[float], k: int, filters: dict | None = None
    ) -> list[SearchResult]:
        """k-NN search for the nearest chunks, optionally constrained by metadata filters."""

    @abstractmethod
    def get_by_document(self, document_id: str) -> list[dict]:
        """Return a document's chunk sources, ordered by chunk index (for ``get_document``)."""

    @abstractmethod
    def search_by_metadata(self, filters: dict, k: int) -> list[SearchResult]:
        """Filter-only search (no vector ranking) over indexed metadata.

        Powers the neutral retrieval tools: relationship traversal and entity listing query the
        flattened semantic fields directly rather than by semantic similarity.
        """

    # --- optional capabilities --------------------------------------------------------
    # Non-abstract so third-party stores written against the original interface still work.
    # Built-in stores override all of them.

    def replace_document(self, document_id: str, chunks: Sequence[Chunk]) -> None:
        """Make ``chunks`` the only indexed chunks of ``document_id``.

        Default: delete then insert (the original behavior). Built-in stores write the new
        generation first and then remove stale chunks, so a failed write never leaves the
        document missing.
        """
        self.delete_by_document(document_id)
        self.upsert(chunks)

    def get_indexed_state(self, document_ids: Sequence[str]) -> dict[str, IndexedState]:
        """Current generation info for each indexed document id (missing ids omitted)."""
        out: dict[str, IndexedState] = {}
        for doc_id in document_ids:
            sources = self.get_by_document(doc_id)
            if sources:
                head = sources[0]
                out[doc_id] = IndexedState(
                    document_id=doc_id,
                    content_hash=head.get("content_hash"),
                    updated_at=head.get("updated_at"),
                    extraction_status=head.get("extraction_status"),
                    extraction_attempts=head.get("extraction_attempts") or 0,
                    extraction_error=head.get("extraction_error"),
                    next_retry_at=head.get("next_retry_at"),
                )
        return out

    def get_document_vectors(self, document_id: str) -> dict[str, list[float]]:
        """``{chunk text: vector}`` of a document's indexed chunks, so re-processing unchanged
        text does not re-embed it. Default: nothing to reuse."""
        return {}

    def documents_due_for_retry(self, now, limit: int = 200) -> list[str] | None:  # noqa: ANN001
        """Ids of documents whose extraction is due for a retry; ``None`` if unsupported."""
        return None

    def delete_documents(self, document_ids: Sequence[str]) -> None:
        for doc_id in document_ids:
            self.delete_by_document(doc_id)

    def list_document_ids(self, filters: dict, limit: int = 10_000) -> set[str] | None:
        """Distinct document ids matching ``filters``; ``None`` if unsupported."""
        return None

    def facet(self, field: str, filters: dict | None = None, size: int = 100) -> (
        list[tuple[str, int]] | None
    ):
        """Distinct values of a keyword ``field`` with document counts; ``None`` if unsupported.
        """
        return None

    def max_value(self, field: str, filters: dict | None = None):  # noqa: ANN201
        """Maximum value of a date/numeric ``field``; ``None`` if unsupported or empty."""
        return None


class Inferencer(ABC):
    """A structured-output LLM client: given prompts, return parsed JSON.

    Kept separate from :class:`Embedder` because semantic indexing uses a generative model (a
    Claude model on the org's own Bedrock) rather than an embedding model. The indexer depends
    only on this interface, so the provider is swappable.
    """

    @abstractmethod
    def infer_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        """Run the model and return the parsed JSON object it produced."""


class SemanticIndexer(ABC):
    """Extracts neutral, objective metadata (entities, relationships, topics) for a document.

    Extraction only — no conclusions or judgments. The result improves retrieval; the agent
    does the reasoning.
    """

    @abstractmethod
    def extract(self, document: Document) -> SemanticMetadata:
        """Analyze a document and return its neutral semantic metadata."""


class Interceptor:
    """The enterprise extension seam.

    Deliberately a concrete base class, not an ABC: every hook has a safe no-op default so an
    interceptor subclasses it and overrides only the points it cares about.

    Core ships no interceptors (pure pass-through). Enterprise features — RBAC, audit logging,
    redaction, SSO-derived principals — register interceptors that the pipeline and MCP tools
    invoke at well-defined points. **Core never imports enterprise code**; interceptors are
    loaded by dotted path from configuration.

    All methods have safe no-op defaults so an interceptor only overrides what it cares about.
    """

    def before_ingest(self, document: Document) -> Document:
        """Inspect/transform a document before it is chunked and embedded."""
        return document

    def after_ingest(self, document: Document) -> None:
        """Observe a document after its chunks have been upserted (e.g. for audit)."""

    def before_query(self, ctx: QueryContext) -> QueryContext:
        """Inspect/transform a query before it executes (e.g. inject tenant filters)."""
        return ctx

    def after_query(
        self, results: list[SearchResult], ctx: QueryContext
    ) -> list[SearchResult]:
        """Inspect/transform results before they are returned (e.g. redact, re-rank)."""
        return results

    def authorize(self, principal: str | None, action: str, resource: str) -> bool:
        """Authorization decision. OSS default allows everything."""
        return True
