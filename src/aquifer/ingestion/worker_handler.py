"""Worker Lambda — triggered by SQS.

Each SQS record is one :class:`FetchJob`. The worker processes a single page through the
:class:`IngestionPipeline` and, if the connector reports more pages, enqueues the successor job
back onto SQS. One bounded page per invocation keeps every run well under the Lambda timeout, so
a full backfill is just many small messages.

Failure handling:

- Rate limited by the source → the same job is re-enqueued with an SQS delay (up to 15 min) and
  the message is acknowledged, so a long GitHub reset does not exhaust SQS receives. After
  ``max_rate_limit_requeues`` the error propagates and the job ends in the DLQ.
- Any other exception propagates: SQS redelivers after the visibility timeout and moves the job
  to the DLQ after ``maxReceiveCount``. Nothing is silently dropped.
- Permanent per-document failures are isolated inside the pipeline (logged and counted).

Clients, the GitHub token (with a TTL so rotation is picked up), and per-source pipelines are
cached at module scope so warm invocations skip re-initialization.
"""

from __future__ import annotations

import logging
import time

from aquifer.core.config import Settings, get_settings
from aquifer.core.errors import (
    RateLimitedError,
    SourceAuthError,
    configure_lambda_logging,
    log_event,
)
from aquifer.core.hooks import get_pipeline
from aquifer.core.models import FetchJob, SourceType
from aquifer.ingestion import aws
from aquifer.ingestion.factory import build_connector, build_embedder, build_vector_store
from aquifer.ingestion.pipeline import IngestionPipeline

logger = logging.getLogger(__name__)

_TOKEN_TTL_S = 900

# Warm-invocation caches (per Lambda container).
_token_cache: dict[str, tuple[float, str]] = {}
_pipeline_cache: dict[str, tuple[float, IngestionPipeline]] = {}


def _github_token(settings: Settings) -> str | None:
    secret = settings.github.token_secret
    if not secret:
        return None
    cached = _token_cache.get(secret)
    if cached is None or time.monotonic() - cached[0] > _TOKEN_TTL_S:
        _token_cache[secret] = (time.monotonic(), aws.get_secret(secret))
    return _token_cache[secret][1]


def _pipeline_for(source_type: SourceType, settings: Settings) -> IngestionPipeline:
    key = source_type.value
    cached = _pipeline_cache.get(key)
    # Rebuild periodically so a rotated source token is used.
    if cached is None or time.monotonic() - cached[0] > _TOKEN_TTL_S:
        token = _github_token(settings) if source_type == SourceType.GITHUB else None
        vector_store = build_vector_store(settings)
        vector_store.ensure_index()  # idempotent; also adds newly introduced fields
        connector = build_connector(
            source_type, token=token, settings=settings,
            index_lookup=vector_store.get_indexed_state,
        )
        pipeline = IngestionPipeline(
            connector=connector,
            embedder=build_embedder(settings),
            vector_store=vector_store,
            interceptors=get_pipeline(),
            chunk_size=settings.ingestion.chunk_size,
            chunk_overlap=settings.ingestion.chunk_overlap,
        )
        _pipeline_cache[key] = (time.monotonic(), pipeline)
    return _pipeline_cache[key][1]


def _invalidate_credentials() -> None:
    """Forget cached tokens and pipelines so the next use re-reads the secret."""
    _token_cache.clear()
    _pipeline_cache.clear()


def _run(job: FetchJob, settings: Settings) -> FetchJob | None:
    pipeline = _pipeline_for(job.source_type, settings)
    if job.job_type in {"reprocess", "reprocess_failed"}:
        pipeline.reprocess(job.document_ids, include_failed=job.job_type == "reprocess_failed")
        return None
    return pipeline.process_job(job)


def _run_with_fresh_credentials(job: FetchJob, settings: Settings) -> FetchJob | None:
    try:
        return _run(job, settings)
    except SourceAuthError:
        # The token may have been rotated since this warm container cached it: re-read the
        # secret once before failing the job.
        log_event(logger, "source_auth_retry", level=logging.WARNING, repo=job.repo)
        _invalidate_credentials()
        return _run(job, settings)


def handler(event, context=None) -> dict:  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    configure_lambda_logging(settings.log_level)
    processed = 0
    requeued = 0
    for record in event.get("Records", []):
        job = FetchJob.model_validate_json(record["body"])
        try:
            successor = _run_with_fresh_credentials(job, settings)
        except RateLimitedError as exc:
            if job.attempt >= settings.ingestion.max_rate_limit_requeues:
                log_event(logger, "rate_limit_requeues_exhausted", level=logging.ERROR,
                          repo=job.repo, kind=str(job.kind), attempt=job.attempt)
                raise
            delay = int(min(900, max(1, exc.retry_after)))
            aws.enqueue_job(
                settings.ingestion.queue_url,
                job.model_copy(update={"attempt": job.attempt + 1}),
                delay_seconds=delay,
            )
            requeued += 1
            log_event(logger, "job_rate_limited", level=logging.WARNING, repo=job.repo,
                      kind=str(job.kind), delay_s=delay, attempt=job.attempt + 1)
            continue
        if successor is not None:
            aws.enqueue_job(settings.ingestion.queue_url, successor)
        processed += 1
    logger.info("worker processed %d job(s)", processed)
    return {"processed": processed, "requeued": requeued}
