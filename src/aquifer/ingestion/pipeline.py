"""The ingestion pipeline — the AWS-free core of ingestion.

Given a single :class:`FetchJob`, it fetches one page, skips documents whose content is already
indexed, runs the interceptor seam, chunks, embeds, and replaces the document's chunks in the
vector store, then returns the successor job (or ``None``) so the caller can re-enqueue.

Reliability properties (each covered by tests):

- **Idempotent.** A document whose content hash matches the indexed generation is skipped
  without any Bedrock call, so redelivered SQS messages and overlapping watermarks are cheap.
- **Never missing.** Replacement writes the new generation before deleting older ones.
- **Failure isolation.** A permanent failure on one document is logged (by id, never content)
  and counted; the rest of the page — and its successor page — still proceed. Transient
  failures propagate so SQS retries the whole page.
- **Deletion propagation.** Connectors may report which documents still exist in a scope; the
  pipeline removes indexed documents in that scope that no longer do.

Keeping this layer free of boto3/SQS/S3 makes the ingest path unit-testable with fakes.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from aquifer.core.chunking import chunk_document
from aquifer.core.errors import is_transient, log_event
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Connector, Embedder, VectorStore
from aquifer.core.models import (
    Document,
    ExtractionStatus,
    FetchJob,
    IndexedState,
    Reconciliation,
)

logger = logging.getLogger(__name__)

# Documents in these states are re-processed even if their content is unchanged, so a
# transient extraction failure (e.g. Bedrock throttling) is eventually repaired.
_RETRY_EXTRACTION = {ExtractionStatus.FAILED.value, ExtractionStatus.PARTIAL.value}


@dataclass
class JobResult:
    successor: FetchJob | None
    fetched: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    failed_ids: list[str] = field(default_factory=list)


class IngestionPipeline:
    def __init__(
        self,
        connector: Connector,
        embedder: Embedder,
        vector_store: VectorStore,
        interceptors: InterceptorPipeline | None = None,
        *,
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        self.connector = connector
        self.embedder = embedder
        self.vector_store = vector_store
        self.interceptors = interceptors or InterceptorPipeline()
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def process_job(self, job: FetchJob) -> FetchJob | None:
        """Process one page; return the successor job to enqueue, or ``None`` if exhausted."""
        return self.run_job(job).successor

    def run_job(self, job: FetchJob) -> JobResult:
        started = time.monotonic()
        documents, successor, reconciliation = self.connector.fetch_with_reconciliation(job)
        result = JobResult(successor=successor, fetched=len(documents))

        states = self.vector_store.get_indexed_state([d.id for d in documents]) if documents else {}
        for document in documents:
            try:
                outcome = self.process_document(document, indexed_state=states.get(document.id),
                                                lookup=False)
            except Exception as exc:
                if is_transient(exc):
                    raise  # retry the page via SQS
                result.failed += 1
                result.failed_ids.append(document.id)
                log_event(
                    logger,
                    "document_failed",
                    level=logging.ERROR,
                    document_id=document.id,
                    repo=job.repo,
                    kind=str(job.kind),
                    error=type(exc).__name__,
                )
                continue
            if outcome == "indexed":
                result.indexed += 1
            else:
                result.skipped += 1

        if reconciliation is not None:
            result.deleted = self.reconcile(reconciliation)

        log_event(
            logger,
            "job_processed",
            repo=job.repo,
            kind=str(job.kind),
            cursor=job.cursor,
            fetched=result.fetched,
            indexed=result.indexed,
            skipped=result.skipped,
            failed=result.failed,
            deleted=result.deleted,
            has_successor=successor is not None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    def process_document(
        self,
        document: Document,
        *,
        indexed_state: IndexedState | None = None,
        lookup: bool = True,
        force: bool = False,
    ) -> str:
        """Run one document through skip-check → seam → chunk → embed → replace.

        Returns ``"indexed"``, ``"unchanged"``, ``"older_than_indexed"`` or ``"empty"``.
        """
        if indexed_state is None and lookup:
            indexed_state = self.vector_store.get_indexed_state([document.id]).get(document.id)
        content_hash = document.content_hash()

        if indexed_state is not None and not force:
            if (
                indexed_state.content_hash == content_hash
                and indexed_state.extraction_status not in _RETRY_EXTRACTION
            ):
                return "unchanged"
            if (
                indexed_state.updated_at is not None
                and document.updated_at is not None
                and document.updated_at < indexed_state.updated_at
            ):
                # A newer version is already indexed (out-of-order delivery).
                return "older_than_indexed"

        document = self.interceptors.before_ingest(document)
        if document.extraction_status is None:
            document.extraction_status = ExtractionStatus.NOT_RUN.value

        chunks = chunk_document(document, self.chunk_size, self.chunk_overlap)
        if not chunks:
            if indexed_state is not None:
                # The upstream item became empty: its old content must not linger.
                self.vector_store.delete_by_document(document.id)
            self.interceptors.after_ingest(document)
            return "empty"

        generation = uuid.uuid4().hex
        vectors = self.embedder.embed_documents([c.text for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
            chunk.content_hash = content_hash
            chunk.generation = generation

        self.vector_store.replace_document(document.id, chunks)
        self.interceptors.after_ingest(document)
        return "indexed"

    def reconcile(self, reconciliation: Reconciliation) -> int:
        """Delete indexed documents in ``scope`` that the source no longer has."""
        indexed = self.vector_store.list_document_ids(reconciliation.scope)
        if indexed is None:
            log_event(
                logger,
                "reconcile_unsupported",
                level=logging.WARNING,
                scope=reconciliation.scope,
            )
            return 0
        stale = sorted(indexed - reconciliation.live_document_ids)
        if stale:
            self.vector_store.delete_documents(stale)
        log_event(
            logger,
            "reconciled",
            scope=reconciliation.scope,
            reason=reconciliation.reason,
            indexed=len(indexed),
            live=len(reconciliation.live_document_ids),
            deleted=len(stale),
        )
        return len(stale)
