"""Discovery Lambda — triggered on a schedule by EventBridge.

For each configured source it:

1. Validates the source credentials once (a bad or unpopulated GitHub token fails the run with
   a clear message instead of failing every job later).
2. Reads the incremental watermark from S3 and rewinds it by ``watermark_overlap_s`` so items
   updated around a run boundary are not missed (re-reads are skipped by content hash).
3. Fans fetch jobs out onto SQS for the worker Lambda.
4. Advances the watermark to the run's start time — only after every job was enqueued.

It also removes indexed documents of repositories that are no longer in the allowlist, so a
repository taken out of scope stops being retrievable.

Watermark semantics: a job that fails after enqueue is retried via SQS and lands in the DLQ
after ``maxReceiveCount`` (alarmed in the stack). The watermark does not wait for job completion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aquifer.core.config import Settings, get_settings
from aquifer.core.errors import log_event
from aquifer.core.models import Source, SourceType
from aquifer.ingestion import aws
from aquifer.ingestion.factory import build_connector, build_vector_store

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


def _github_token(settings: Settings) -> str | None:
    if not settings.github.token_secret:
        return None
    return aws.get_secret(settings.github.token_secret)


def purge_unlisted_repos(settings: Settings, vector_store=None) -> int:  # noqa: ANN001
    """Delete indexed documents whose repository is no longer in the allowlist."""
    allowed = set(settings.github.repo_allowlist)
    if not allowed:
        # An empty allowlist is far more likely a misconfiguration than an intent to wipe.
        log_event(logger, "purge_skipped_empty_allowlist", level=logging.WARNING)
        return 0
    store = vector_store or build_vector_store(settings)
    facets = store.facet("repo", None, size=10_000)
    if facets is None:
        return 0
    removed = 0
    for repo, _count in facets:
        if repo not in allowed:
            store.delete_by_filter({"repo": repo})
            removed += 1
            log_event(logger, "repo_purged", repo=repo)
    return removed


def handler(event=None, context=None) -> dict:  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    ingestion = settings.ingestion
    run_started = datetime.now(UTC)

    enqueued = 0
    for source in _build_sources():
        key = _watermark_key(source.id)
        since = aws.read_watermark(ingestion.state_bucket, key)
        if since is not None:
            since = since - timedelta(seconds=ingestion.watermark_overlap_s)
        source.config["since"] = since

        token = _github_token(settings) if source.type == SourceType.GITHUB else None
        connector = build_connector(source.type, token=token, settings=settings)
        if hasattr(connector, "validate_access"):
            connector.validate_access()  # raises SourceAuthError with a clear message

        for job in connector.discover(source):
            aws.enqueue_job(ingestion.queue_url, job)
            enqueued += 1

        # Advance the high-water mark for the next scheduled run.
        aws.write_watermark(ingestion.state_bucket, key, run_started)
        log_event(logger, "source_discovered", source_id=source.id,
                  since=since.isoformat() if since else None)

    purged = 0
    if ingestion.purge_unlisted_repos and settings.vector_store.endpoint:
        purged = purge_unlisted_repos(settings)

    log_event(logger, "discovery_complete", enqueued=enqueued, repos_purged=purged)
    return {"enqueued": enqueued, "repos_purged": purged}
