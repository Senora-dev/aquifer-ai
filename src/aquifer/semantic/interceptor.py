"""The semantic-indexing interceptor — how indexing plugs into the pipeline.

Registered in ``settings.interceptors``, its ``before_ingest`` attaches neutral metadata to the
document before it is chunked, embedded, and indexed:

1. **Deterministic extraction** (Terraform, docker-compose, Backstage descriptors) — free,
   local, ``origin = explicit_reference``, status ``current``, parser line numbers.
2. **LLM extraction** for everything else (and for structured files only when
   ``llm_for_structured_files`` is set) — ``origin = llm_extracted``; each relationship carries
   the source's ``assertion`` and a derived ``status`` (claimed / removed / proposed / ...),
   and is checked against the text (``quote_verified``, ``mention_found``).

The two are merged; explicit references win on overlap.

Extraction failures never fail the document's ingestion: it is indexed (search works) and
marked ``retry_pending`` with an exponential-backoff ``next_retry_at``; discovery re-queues due
documents and the worker re-runs extraction from the content already in the index — no change
in the source is needed. After the retry budget the document is marked ``failed`` with a stable
``extraction_error`` code and is not retried until its content changes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aquifer.core.config import SemanticIndexSettings, get_settings
from aquifer.core.errors import log_event
from aquifer.core.interfaces import Interceptor, SemanticIndexer
from aquifer.core.models import Document, ExtractionStatus
from aquifer.semantic.failures import classify_extraction_error
from aquifer.semantic.structured import extract_structured, ground_llm_metadata, merge_metadata

logger = logging.getLogger(__name__)


def next_retry_delay(attempts: int, settings: SemanticIndexSettings) -> timedelta:
    """Exponential backoff: base, 2x base, 4x base ... capped."""
    seconds = settings.retry_base_s * (2 ** max(0, attempts - 1))
    return timedelta(seconds=min(settings.retry_max_s, seconds))


class SemanticIndexInterceptor(Interceptor):
    def __init__(self, indexer: SemanticIndexer | None = None) -> None:
        self._indexer = indexer

    @property
    def indexer(self) -> SemanticIndexer:
        if self._indexer is None:
            from aquifer.ingestion.factory import build_semantic_indexer

            self._indexer = build_semantic_indexer()
        return self._indexer

    def before_ingest(self, document: Document) -> Document:
        settings = get_settings().semantic_index

        structured = None
        if settings.structured_extraction_enabled:
            try:
                structured = extract_structured(document)
            except Exception as exc:  # noqa: BLE001 - a parser bug must not fail ingestion
                log_event(logger, "structured_extraction_failed", level=logging.ERROR,
                          document_id=document.id, error=type(exc).__name__)

        run_llm = settings.enabled and (structured is None or settings.llm_for_structured_files)
        llm = None
        failure = None
        if run_llm:
            try:
                llm = ground_llm_metadata(self.indexer.extract(document), document)
            except Exception as exc:  # noqa: BLE001 - recorded and retried, never swallowed
                failure = classify_extraction_error(exc)

        merged = merge_metadata(structured, llm)
        if merged is not None:
            document.semantic = merged

        if failure is not None:
            attempts = document.extraction_attempts + 1
            budget = (settings.max_content_attempts if failure.kind == "content"
                      else settings.max_attempts)
            document.extraction_attempts = attempts
            document.extraction_error = failure.code
            if attempts >= budget:
                document.extraction_status = ExtractionStatus.FAILED.value
                document.next_retry_at = None
            else:
                document.extraction_status = ExtractionStatus.RETRY_PENDING.value
                document.next_retry_at = datetime.now(UTC) + next_retry_delay(attempts, settings)
            log_event(
                logger,
                "semantic_extraction_failed",
                level=logging.ERROR,
                document_id=document.id,
                error=failure.code,
                kind=failure.kind,
                attempt=attempts,
                status=document.extraction_status,
                next_retry_at=document.next_retry_at,
                action=failure.message,
            )
            return document

        if structured is not None or llm is not None:
            status = ExtractionStatus.OK
        elif not settings.enabled:
            status = ExtractionStatus.DISABLED
        else:
            status = ExtractionStatus.NOT_RUN
        document.extraction_status = status.value
        document.extraction_attempts = 0
        document.extraction_error = None
        document.next_retry_at = None
        return document
