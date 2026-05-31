"""Worker Lambda — triggered by SQS.

Each SQS record is one :class:`FetchJob`. The worker processes a single page through the
:class:`IngestionPipeline` and, if the connector reports more pages, enqueues the successor job
back onto SQS. One bounded page per invocation keeps every run well under the Lambda timeout, so
a full backfill is just many small messages.

Clients, the GitHub token, and per-source pipelines are cached at module scope so warm
invocations skip re-initialization.
"""

from __future__ import annotations

import logging

from aquifer.core.config import Settings, get_settings
from aquifer.core.hooks import get_pipeline
from aquifer.core.models import FetchJob, SourceType
from aquifer.ingestion import aws
from aquifer.ingestion.factory import build_connector, build_embedder, build_vector_store
from aquifer.ingestion.pipeline import IngestionPipeline

logger = logging.getLogger(__name__)

# Warm-invocation caches (per Lambda container).
_token_cache: dict[str, str] = {}
_pipeline_cache: dict[str, IngestionPipeline] = {}


def _github_token(settings: Settings) -> str | None:
    secret = settings.github.token_secret
    if not secret:
        return None
    if secret not in _token_cache:
        _token_cache[secret] = aws.get_secret(secret)
    return _token_cache[secret]


def _pipeline_for(source_type: SourceType, settings: Settings) -> IngestionPipeline:
    key = source_type.value
    if key not in _pipeline_cache:
        token = _github_token(settings) if source_type == SourceType.GITHUB else None
        connector = build_connector(source_type, token=token, settings=settings)
        vector_store = build_vector_store(settings)
        vector_store.ensure_index()  # idempotent; runs once per cold start
        _pipeline_cache[key] = IngestionPipeline(
            connector=connector,
            embedder=build_embedder(settings),
            vector_store=vector_store,
            interceptors=get_pipeline(),
            chunk_size=settings.ingestion.chunk_size,
            chunk_overlap=settings.ingestion.chunk_overlap,
        )
    return _pipeline_cache[key]


def handler(event, context=None) -> dict:  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    processed = 0
    for record in event.get("Records", []):
        job = FetchJob.model_validate_json(record["body"])
        pipeline = _pipeline_for(job.source_type, settings)
        successor = pipeline.process_job(job)
        if successor is not None:
            aws.enqueue_job(settings.ingestion.queue_url, successor)
        processed += 1
    logger.info("worker processed %d job(s)", processed)
    return {"processed": processed}
