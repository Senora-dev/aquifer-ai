"""Discovery Lambda — triggered on a schedule by EventBridge.

Enumerates configured sources, reads each source's incremental watermark from S3, and fans the
resulting fetch jobs out onto SQS for the worker Lambda. It then advances the watermark to the
run's start time so the next run only sees newer items.

Watermark semantics (baseline): the new watermark is the *run start time*, written after the
jobs are enqueued. This is the simple, well-understood "high-water mark" pattern; a job that
fails after enqueue is retried via SQS/DLQ rather than via the watermark. Enterprise deployments
can replace this with per-job completion tracking via the interceptor seam.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aquifer.core.config import get_settings
from aquifer.core.models import Source, SourceType
from aquifer.ingestion import aws
from aquifer.ingestion.factory import build_connector

logger = logging.getLogger(__name__)


def _build_sources() -> list[Source]:
    """Construct the configured sources. Baseline: a single GitHub source."""
    settings = get_settings()
    repos = settings.github.repo_allowlist
    if not repos:
        return []
    return [Source(id="github", type=SourceType.GITHUB, config={"repos": repos})]


def _watermark_key(source_id: str) -> str:
    return f"{get_settings().ingestion.state_prefix}{source_id}.json"


def handler(event=None, context=None) -> dict:  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    ingestion = settings.ingestion
    run_started = datetime.now(UTC)

    enqueued = 0
    for source in _build_sources():
        key = _watermark_key(source.id)
        since = aws.read_watermark(ingestion.state_bucket, key)
        source.config["since"] = since

        connector = build_connector(source.type, settings=settings)
        for job in connector.discover(source):
            aws.enqueue_job(ingestion.queue_url, job)
            enqueued += 1

        # Advance the high-water mark for the next scheduled run.
        aws.write_watermark(ingestion.state_bucket, key, run_started)

    logger.info("discovery enqueued %d job(s)", enqueued)
    return {"enqueued": enqueued}
