"""The single-server runtime: one process runs the MCP server, a scheduler and workers.

``aquifer serve`` starts :class:`NodeService` (background threads) and then the authenticated
MCP HTTP server in the foreground. Ingestion uses the same discovery, worker and pipeline code as
the AWS Lambdas; only the queue and state store differ (see :mod:`aquifer.node.queue`).

Job kinds:

- ``discover``: one discovery run (GitHub); enqueues ``fetch`` jobs. Added by the scheduler
  every ``node.sync_interval_s`` and by ``aquifer sync``.
- ``fetch``: one page of a source, or a re-processing batch (extraction retries).
- ``retry_failed``: requeue extraction for documents marked failed (operator action).

Additional kinds (MCP source syncs) are registered with :func:`register_handler`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aquifer.core.config import Settings
from aquifer.core.errors import RateLimitedError, log_event
from aquifer.core.models import FetchJob
from aquifer.ingestion.runtime import set_runtime
from aquifer.node.queue import Job, JobQueue, NodeRuntime

logger = logging.getLogger(__name__)

Handler = Callable[["NodeService", Job], None]
_HANDLERS: dict[str, Handler] = {}

def register_handler(kind: str, handler: Handler) -> None:
    _HANDLERS[kind] = handler


def queue_path(settings: Settings) -> Path:
    return Path(settings.node.data_dir) / "queue.db"


def open_queue(settings: Settings) -> JobQueue:
    node = settings.node
    return JobQueue(queue_path(settings), max_attempts=node.job_max_attempts,
                    retry_base_s=node.job_retry_base_s, retry_max_s=node.job_retry_max_s,
                    lease_s=node.job_lease_s)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- job handlers ---------------------------------------------------------------------

def _handle_discover(service: NodeService, job: Job) -> None:
    from aquifer.ingestion import discovery_handler

    started = _now()
    service.queue.set_state("discovery:last_started", started)
    try:
        result = discovery_handler.run_discovery(service.settings,
                                                 full=bool(job.payload.get("full")))
    except Exception as exc:
        service.queue.set_state("discovery:last_error",
                                {"at": _now(), "error": _error_text(exc)})
        raise
    service.queue.set_state("discovery:last_success", {"at": _now(), "started": started,
                                                      **result})
    service.queue.set_state("discovery:last_error", None)


def _handle_fetch(service: NodeService, job: Job) -> None:
    from aquifer.ingestion import worker_handler

    fetch = FetchJob.model_validate(job.payload)
    settings = service.settings
    try:
        successor = worker_handler._run_with_fresh_credentials(fetch, settings)
    except RateLimitedError as exc:
        if fetch.attempt >= settings.ingestion.max_rate_limit_requeues:
            raise
        delay = max(1, int(exc.retry_after))
        service.queue.postpone(job, delay, json_payload(fetch.model_copy(
            update={"attempt": fetch.attempt + 1})))
        log_event(logger, "job_rate_limited", level=logging.WARNING, repo=fetch.repo,
                  kind=str(fetch.kind), delay_s=delay, attempt=fetch.attempt + 1)
        raise _Postponed from None
    if successor is not None:
        service.runtime.enqueue(successor)


def _handle_retry_failed(service: NodeService, job: Job) -> None:
    from aquifer.ingestion import discovery_handler

    discovery_handler.enqueue_failed_retries(service.settings)


def json_payload(model) -> dict:  # noqa: ANN001
    import json

    return json.loads(model.model_dump_json())


register_handler("discover", _handle_discover)
register_handler("fetch", _handle_fetch)
register_handler("retry_failed", _handle_retry_failed)


class _Postponed(Exception):
    """The handler already re-scheduled the job (not a failure)."""


def _error_text(exc: BaseException) -> str:
    # Exception messages in Aquifer never carry credentials (connectors raise sanitized
    # errors); keep only the type and a bounded message.
    text = str(exc).splitlines()[0][:300] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class NodeService:
    def __init__(self, settings: Settings, queue: JobQueue | None = None) -> None:
        self.settings = settings
        self.queue = queue or open_queue(settings)
        self.runtime = NodeRuntime(self.queue)
        set_runtime(self.runtime)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()

    # --- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        recovered = self.queue.recover_interrupted()
        if recovered:
            log_event(logger, "jobs_recovered_after_restart", jobs=recovered)
        self._spawn("aquifer-scheduler", self._schedule_loop)
        for i in range(max(1, self.settings.node.workers)):
            self._spawn(f"aquifer-worker-{i}", self._work_loop)
        log_event(logger, "node_started", workers=self.settings.node.workers,
                  sync_interval_s=self.settings.node.sync_interval_s)

    def stop(self, timeout: float = 30) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # --- scheduler --------------------------------------------------------------------

    def schedule_due(self, now: float | None = None) -> list[str]:
        """Add periodic jobs that are due. Returns the kinds added."""
        now = now or time.time()
        added = []
        interval = self.settings.node.sync_interval_s
        last = self.queue.get_state("scheduler:discover", 0)
        if interval > 0 and now - last >= interval and self.queue.put(
                "discover", {}, dedupe_key="discover"):
            added.append("discover")
        if interval > 0 and now - last >= interval:
            self.queue.set_state("scheduler:discover", now)
        for extra in _SCHEDULERS:
            added.extend(extra(self, now))
        if added:
            self._wake.set()
        return added

    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.schedule_due()
            except Exception:
                logger.exception("scheduler error")
            self._stop.wait(15)

    # --- workers ----------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and run one job. Returns False if nothing was due."""
        job = self.queue.claim()
        if job is None:
            return False
        handler = _HANDLERS.get(job.kind)
        started = time.monotonic()
        try:
            if handler is None:
                raise ValueError(f"no handler for job kind {job.kind!r}")
            handler(self, job)
        except _Postponed:
            return True
        except Exception as exc:
            action = self.queue.fail(job, _error_text(exc))
            log_event(logger, "job_failed", level=logging.ERROR, job_id=job.id, kind=job.kind,
                      attempt=job.attempts, action=action, error=type(exc).__name__)
            return True
        self.queue.complete(job)
        log_event(logger, "job_done", job_id=job.id, kind=job.kind,
                  duration_ms=int((time.monotonic() - started) * 1000))
        return True

    def drain(self, timeout_s: float = 600) -> int:
        """Run due jobs in this thread until none is due (tests, one-shot CLI)."""
        deadline = time.monotonic() + timeout_s
        n = 0
        while time.monotonic() < deadline and self.run_once():
            n += 1
        return n

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("worker error")
                worked = False
            if not worked:
                self._wake.wait(2)
                self._wake.clear()


# Extra periodic schedulers (e.g. MCP source syncs): fn(service, now) -> kinds added.
_SCHEDULERS: list[Callable[[NodeService, float], list[str]]] = []


def register_scheduler(fn: Callable[[NodeService, float], list[str]]) -> None:
    _SCHEDULERS.append(fn)
