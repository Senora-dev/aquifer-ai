"""The semantic-indexing interceptor — how indexing plugs into the pipeline.

Registered in ``settings.interceptors``, its ``before_ingest`` runs the semantic indexer and
attaches the neutral metadata to the document before it is chunked, embedded, and indexed. The
indexer is built lazily so merely loading the interceptor (which the loader does with no
arguments) never touches AWS — the model client is constructed on first ingest.

Indexing is best-effort: if extraction fails, the document is ingested without semantic metadata
rather than failing the whole job. Toggle with ``AQUIFER_SEMANTIC_INDEX__ENABLED``.
"""

from __future__ import annotations

import logging

from aquifer.core.config import get_settings
from aquifer.core.interfaces import Interceptor, SemanticIndexer
from aquifer.core.models import Document

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
        if not get_settings().semantic_index.enabled:
            return document
        try:
            document.semantic = self.indexer.extract(document)
        except Exception:
            # Never fail ingestion because indexing failed; the chunk is still useful.
            logger.exception("semantic indexing failed for %s; ingesting without it", document.id)
        return document
