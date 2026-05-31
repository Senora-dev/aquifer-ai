"""Semantic indexing: neutral metadata extraction at ingestion time.

Plugs into the ``before_ingest`` hook via :class:`SemanticIndexInterceptor`. The extracted
entities, relationships, and topics are denormalized onto chunks and indexed in AOSS as
queryable fields — objective context to improve retrieval, never conclusions or verdicts.
"""
