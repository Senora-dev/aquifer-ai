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
from datetime import UTC, datetime

from aquifer.core.chunking import chunk_document, newest_generation, reassemble
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

# Extraction states that are retried while the content is unchanged (when due).
_RETRYABLE = {ExtractionStatus.RETRY_PENDING.value, ExtractionStatus.PARTIAL.value}


def retry_due(state: IndexedState | None, now: datetime | None = None) -> bool:
    """Whether an unchanged document should be re-processed to retry its extraction."""
    if state is None:
        return False
    status = state.extraction_status
    if status == ExtractionStatus.FAILED.value:
        # Terminal unless it predates retry tracking (no error code): give those one retry.
        return state.extraction_error is None
    if status not in _RETRYABLE:
        return False
    return state.next_retry_at is None or state.next_retry_at <= (now or datetime.now(UTC))


def document_from_index(sources: list[dict]) -> Document:
    """Rebuild a document from its indexed chunks (exact text and source metadata)."""
    head = sources[0]
    title = head.get("title") or ""
    text = reassemble(sources)
    body = text[len(title) + 2:] if title and text.startswith(f"{title}\n\n") else text
    meta = dict(head.get("source_metadata") or {})
    body = meta.pop("_body_leading_ws", "") + body
    external_id = meta.pop("_external_id", None) or head.get("document_id", "")
    return Document(
        id=head["document_id"],
        source_type=head.get("source_type"),
        external_id=external_id,
        kind=head.get("kind"),
        repo=head.get("repo"),
        title=title,
        body=body,
        url=head.get("url") or "",
        author=head.get("author"),
        created_at=head.get("created_at"),
        updated_at=head.get("updated_at"),
        labels=head.get("labels") or [],
        metadata=meta,
        path=head.get("path"),
        revision=head.get("revision"),
        revision_committed_at=head.get("revision_committed_at"),
        fetched_at=head.get("fetched_at"),
        state=head.get("state"),
        extraction_status=head.get("extraction_status"),
        extraction_attempts=head.get("extraction_attempts") or 0,
        extraction_error=head.get("extraction_error"),
    )


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
        content_hash: str | None = None,
        reuse_vectors: dict[str, list[float]] | None = None,
    ) -> str:
        """Run one document through skip-check → seam → chunk → embed → replace.

        Returns ``"indexed"``, ``"unchanged"``, ``"retry_scheduled"``, ``"retry_exhausted"``,
        ``"older_than_indexed"`` or ``"empty"``.
        """
        if indexed_state is None and lookup:
            indexed_state = self.vector_store.get_indexed_state([document.id]).get(document.id)
        content_hash = content_hash or document.content_hash()
        unchanged = indexed_state is not None and indexed_state.content_hash == content_hash

        if indexed_state is not None and not force:
            if unchanged:
                if retry_due(indexed_state):
                    pass  # same content, but extraction is due for a retry
                elif indexed_state.extraction_status in _RETRYABLE:
                    return "retry_scheduled"
                elif indexed_state.extraction_status == ExtractionStatus.FAILED.value:
                    return "retry_exhausted"
                else:
                    return "unchanged"
            elif (
                indexed_state.updated_at is not None
                and document.updated_at is not None
                and document.updated_at < indexed_state.updated_at
            ):
                # A newer version is already indexed (out-of-order delivery).
                return "older_than_indexed"

        # The retry budget belongs to the content version: it carries over while the content is
        # unchanged and resets when it changes.
        if unchanged and indexed_state is not None:
            document.extraction_attempts = indexed_state.extraction_attempts
        elif not force:
            document.extraction_attempts = 0

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
        reuse_vectors = reuse_vectors or {}
        missing = [c.text for c in chunks if c.text not in reuse_vectors]
        fresh = dict(zip(missing, self.embedder.embed_documents(missing), strict=True)) \
            if missing else {}
        for chunk in chunks:
            chunk.embedding = reuse_vectors.get(chunk.text) or fresh[chunk.text]
            chunk.content_hash = content_hash
            chunk.generation = generation

        self.vector_store.replace_document(document.id, chunks)
        self.interceptors.after_ingest(document)
        return "indexed"

    def reprocess(self, document_ids: list[str], *, include_failed: bool = False) -> JobResult:
        """Retry extraction for documents from the content already in the index.

        No source access: the document is rebuilt from its indexed chunks, and existing
        embeddings are reused when the chunk text is unchanged. Documents whose content changed
        meanwhile, or that are no longer due, are skipped.
        """
        started = time.monotonic()
        result = JobResult(successor=None)
        for doc_id in document_ids:
            sources = newest_generation(self.vector_store.get_by_document(doc_id))
            if not sources:
                result.skipped += 1
                continue
            head = sources[0]
            state = IndexedState(
                document_id=doc_id,
                content_hash=head.get("content_hash"),
                extraction_status=head.get("extraction_status"),
                extraction_attempts=head.get("extraction_attempts") or 0,
                extraction_error=head.get("extraction_error"),
                next_retry_at=head.get("next_retry_at"),
            )
            manual = include_failed and state.extraction_status == ExtractionStatus.FAILED.value
            if not (retry_due(state) or manual):
                result.skipped += 1
                continue
            if manual:
                state.extraction_attempts = 0  # an operator asked for a fresh retry budget
            try:
                document = document_from_index(sources)
                document.extraction_attempts = state.extraction_attempts
                vectors = self.vector_store.get_document_vectors(doc_id)
                # Re-check right before writing: a concurrent fetch may have stored new content.
                latest = self.vector_store.get_indexed_state([doc_id]).get(doc_id)
                if latest is None or latest.content_hash != state.content_hash:
                    result.skipped += 1
                    continue
                outcome = self.process_document(
                    document, indexed_state=state, lookup=False, force=True,
                    content_hash=state.content_hash, reuse_vectors=vectors)
            except Exception as exc:
                if is_transient(exc):
                    raise
                result.failed += 1
                result.failed_ids.append(doc_id)
                log_event(logger, "reprocess_failed", level=logging.ERROR, document_id=doc_id,
                          error=type(exc).__name__)
                continue
            result.indexed += outcome == "indexed"
        log_event(logger, "reprocess_processed", documents=len(document_ids),
                  reprocessed=result.indexed, skipped=result.skipped, failed=result.failed,
                  duration_ms=int((time.monotonic() - started) * 1000))
        return result

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
