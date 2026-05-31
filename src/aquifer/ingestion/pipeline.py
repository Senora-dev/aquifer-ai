"""The ingestion pipeline — the AWS-free core of ingestion.

Given a single :class:`FetchJob`, it fetches one page, normalizes each document, runs the
interceptor seam, chunks, embeds, and upserts into the vector store, then returns the successor
job (or ``None``) so the caller can re-enqueue. Keeping this layer free of boto3/SQS/S3 makes
the whole ingest path unit-testable with fakes; the Lambda handlers supply the AWS wiring.
"""

from __future__ import annotations

import logging

from aquifer.core.chunking import chunk_document
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Connector, Embedder, VectorStore
from aquifer.core.models import Document, FetchJob

logger = logging.getLogger(__name__)


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
        documents, successor = self.connector.fetch(job)
        logger.info(
            "fetched %d documents (repo=%s kind=%s cursor=%s)",
            len(documents), job.repo, job.kind, job.cursor,
        )
        for document in documents:
            self.process_document(document)
        return successor

    def process_document(self, document: Document) -> None:
        """Run one document through the seam → chunk → embed → upsert."""
        document = self.interceptors.before_ingest(document)

        chunks = chunk_document(document, self.chunk_size, self.chunk_overlap)
        if not chunks:
            self.interceptors.after_ingest(document)
            return

        vectors = self.embedder.embed_documents([c.text for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector

        # Delete-then-insert so an edited upstream item never leaves stale chunks behind.
        self.vector_store.delete_by_document(document.id)
        self.vector_store.upsert(chunks)

        self.interceptors.after_ingest(document)
