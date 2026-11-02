"""The semantic-indexing interceptor — how indexing plugs into the pipeline.

Registered in ``settings.interceptors``, its ``before_ingest`` attaches neutral metadata to the
document before it is chunked, embedded, and indexed:

1. **Deterministic extraction** (Terraform, docker-compose, Backstage descriptors) — free,
   local, ``origin = explicit_reference`` with parser line numbers.
2. **LLM extraction** for everything else (and for structured files only when
   ``llm_for_structured_files`` is set) — ``origin = llm_extracted``, then grounded against the
   text.

The two are merged; explicit references win on overlap. The outcome is recorded in
``document.extraction_status`` (``ok`` / ``partial`` / ``failed`` / ``disabled``). Transient
LLM errors (throttling, timeouts) propagate so SQS retries the page; permanent ones (e.g.
unparseable output) are recorded as ``failed`` and re-attempted whenever the document is next
fetched, instead of being silently left without metadata.

The LLM indexer is built lazily so loading the interceptor never touches AWS.
"""

from __future__ import annotations

import logging

from aquifer.core.config import get_settings
from aquifer.core.errors import is_transient, log_event
from aquifer.core.interfaces import Interceptor, SemanticIndexer
from aquifer.core.models import Document, ExtractionStatus
from aquifer.semantic.structured import extract_structured, ground_llm_metadata, merge_metadata

logger = logging.getLogger(__name__)


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
        llm_failed = False
        if run_llm:
            try:
                llm = ground_llm_metadata(self.indexer.extract(document), document)
            except Exception as exc:  # noqa: BLE001 - best effort, but recorded
                if is_transient(exc):
                    # Throttling/timeouts: fail the page so SQS retries it. Recording "failed"
                    # instead would leave issues/PRs without metadata until they next change.
                    raise
                llm_failed = True
                log_event(logger, "semantic_extraction_failed", level=logging.ERROR,
                          document_id=document.id, error=type(exc).__name__)

        merged = merge_metadata(structured, llm)
        if merged is not None:
            document.semantic = merged
        if llm_failed:
            status = ExtractionStatus.PARTIAL if structured is not None else ExtractionStatus.FAILED
        elif structured is not None or llm is not None:
            status = ExtractionStatus.OK
        elif not settings.enabled:
            status = ExtractionStatus.DISABLED
        else:
            status = ExtractionStatus.NOT_RUN
        document.extraction_status = status.value
        return document
