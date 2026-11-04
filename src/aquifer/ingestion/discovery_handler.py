"""Discovery — on a schedule (EventBridge → Lambda, or the single-server scheduler).

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
from aquifer.core.errors import configure_lambda_logging, log_event
from aquifer.core.models import DocumentKind, FetchJob, Source, SourceType
from aquifer.ingestion.factory import build_connector, build_vector_store
from aquifer.ingestion.runtime import get_runtime

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
    return get_runtime().get_secret(settings.github.token_secret)


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


def _repo_and_kind(document_id: str) -> tuple[str, str]:
    """``github:owner/repo:kind:external`` → (owner/repo, kind)."""
    parts = document_id.split(":")
    if len(parts) >= 4:
        return parts[1], parts[2]
    return "*", DocumentKind.FILE.value


def enqueue_failed_retries(settings: Settings, vector_store=None) -> int:  # noqa: ANN001
    """Manual recovery: re-run extraction for every document marked ``failed``."""
    store = vector_store or build_vector_store(settings)
    ids = sorted(store.list_document_ids({"extraction_status": "failed"}) or [])
    size = max(1, settings.ingestion.retry_job_size)
    for i in range(0, len(ids), size):
        repo, kind = _repo_and_kind(ids[i])
        try:
            kind_value = DocumentKind(kind)
        except ValueError:
            kind_value = DocumentKind.FILE
        get_runtime().enqueue(FetchJob(
            source_id="retry", source_type=SourceType.GITHUB, repo=repo, kind=kind_value,
            job_type="reprocess_failed", document_ids=ids[i : i + size]))
    log_event(logger, "failed_extraction_requeued", documents=len(ids))
    return len(ids)


def enqueue_due_retries(settings: Settings, vector_store=None, now=None) -> dict:  # noqa: ANN001
    """Queue re-processing for documents whose extraction is due for a retry.

    This is the primary recovery path for failed extraction: it needs no change in the source
    and no re-fetch (documents are rebuilt from the index). Also reports the backlog by status.
    """
    store = vector_store or build_vector_store(settings)
    now = now or datetime.now(UTC)
    due = store.documents_due_for_retry(now, limit=settings.ingestion.retry_sweep_limit) or []
    groups: dict[tuple[str, str], list[str]] = {}
    for doc_id in due:
        groups.setdefault(_repo_and_kind(doc_id), []).append(doc_id)
    jobs = 0
    size = max(1, settings.ingestion.retry_job_size)
    for (repo, kind), ids in groups.items():
        for i in range(0, len(ids), size):
            try:
                kind_value = DocumentKind(kind)
            except ValueError:
                kind_value = DocumentKind.FILE
            get_runtime().enqueue(FetchJob(
                source_id="retry", source_type=SourceType.GITHUB, repo=repo, kind=kind_value,
                job_type="reprocess", document_ids=ids[i : i + size]))
            jobs += 1
    backlog = dict(store.facet("extraction_status", None, size=20) or [])
    errors = dict(store.facet("extraction_error", None, size=20) or [])
    log_event(logger, "extraction_backlog", due_for_retry=len(due), retry_jobs=jobs,
              by_status=backlog, by_error=errors)
    return {"due_for_retry": len(due), "retry_jobs": jobs, "by_status": backlog,
            "by_error": errors}


def _has_index(settings: Settings) -> bool:
    return settings.vector_store.backend != "opensearch" or bool(settings.vector_store.endpoint)


def handler(event=None, context=None) -> dict:  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    configure_lambda_logging(settings.log_level)
    if isinstance(event, dict) and event.get("retry_failed"):
        # Operator action after fixing a cause (e.g. a model or prompt): fresh retry budget.
        return {"requeued_failed": enqueue_failed_retries(settings)}
    return run_discovery(settings, full=isinstance(event, dict) and bool(event.get("full")))


def run_discovery(settings: Settings, *, full: bool = False) -> dict:
    """One discovery run. ``full`` ignores the watermark (re-lists everything; unchanged
    documents are still skipped by content hash)."""
    ingestion = settings.ingestion
    runtime = get_runtime()
    run_started = datetime.now(UTC)

    enqueued = 0
    for source in _build_sources():
        key = _watermark_key(source.id)
        since = None if full else runtime.read_watermark(key)
        if since is not None:
            since = since - timedelta(seconds=ingestion.watermark_overlap_s)
        source.config["since"] = since

        token = _github_token(settings) if source.type == SourceType.GITHUB else None
        connector = build_connector(source.type, token=token, settings=settings)
        if hasattr(connector, "validate_access"):
            connector.validate_access()  # raises SourceAuthError with a clear message

        for job in connector.discover(source):
            runtime.enqueue(job)
            enqueued += 1

        # Advance the high-water mark for the next scheduled run.
        runtime.write_watermark(key, run_started)
        log_event(logger, "source_discovered", source_id=source.id,
                  since=since.isoformat() if since else None)

    purged = 0
    retries: dict = {}
    if _has_index(settings):
        if ingestion.purge_unlisted_repos:
            purged = purge_unlisted_repos(settings)
        retries = enqueue_due_retries(settings)

    log_event(logger, "discovery_complete", enqueued=enqueued, repos_purged=purged,
              retry_jobs=retries.get("retry_jobs"))
    return {"enqueued": enqueued, "repos_purged": purged, "extraction": retries}
