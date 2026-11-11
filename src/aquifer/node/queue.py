"""A durable job queue and state store in SQLite, for the single-server profile.

It provides what SQS + S3 provide in the scale profile, on the server's own disk:

- **At-least-once delivery.** A claimed job holds a lease. If the process dies mid-job, the lease
  expires and the job is claimed again. Jobs are idempotent (the pipeline skips unchanged
  content), so a repeat is cheap.
- **Bounded retries.** A failed job is retried with exponential backoff and moved to the
  dead-letter list after ``max_attempts``. Dead letters stay visible (``aquifer status``) and
  can be requeued. Nothing is dropped silently.
- **Delays.** Rate-limited jobs are re-scheduled for when the source allows them.
- **Deduplication.** Periodic jobs (discovery, MCP source syncs) carry a key; while one is
  pending or running, another with the same key is not added.
- **State.** Watermarks, sync checkpoints and run history, in the same transactional file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aquifer.core.models import INDEX_FORMAT_VERSION, FetchJob
from aquifer.ingestion.runtime import Runtime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | running | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    lease_until REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    dedupe_key: str | None = None


class JobQueue:
    def __init__(self, path: str | Path, *, max_attempts: int = 5, retry_base_s: float = 30,
                 retry_max_s: float = 3600, lease_s: float = 1800) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.lease_s = lease_s
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                     timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    # --- jobs -------------------------------------------------------------------------

    def put(self, kind: str, payload: dict, *, delay_s: float = 0,
            dedupe_key: str | None = None) -> bool:
        """Add a job. Returns False if a job with the same ``dedupe_key`` is already queued."""
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO jobs (kind, payload, dedupe_key, available_at, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, json.dumps(payload), dedupe_key, now + max(0, delay_s), now, now))
            except sqlite3.IntegrityError:
                return False
        return True

    def claim(self) -> Job | None:
        """Take the next due job (or one whose lease expired) and lease it."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_until = ?, "
                "updated_at = ? WHERE id = (SELECT id FROM jobs WHERE "
                "(status = 'pending' AND available_at <= ?) OR "
                "(status = 'running' AND lease_until < ?) ORDER BY available_at, id LIMIT 1) "
                "RETURNING id, kind, payload, attempts, dedupe_key",
                (now + self.lease_s, now, now, now)).fetchone()
        if row is None:
            return None
        return Job(id=row[0], kind=row[1], payload=json.loads(row[2]), attempts=row[3],
                   dedupe_key=row[4])

    def complete(self, job: Job) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))

    def retry_delay(self, attempts: int) -> float:
        return min(self.retry_max_s, self.retry_base_s * 2 ** max(0, attempts - 1))

    def fail(self, job: Job, error: str) -> str:
        """Record a failure: schedule a retry with backoff, or dead-letter the job."""
        now = time.time()
        with self._lock:
            if job.attempts >= self.max_attempts:
                self._conn.execute(
                    "UPDATE jobs SET status = 'dead', lease_until = NULL, last_error = ?, "
                    "updated_at = ? WHERE id = ?", (error[:500], now, job.id))
                return "dead_letter"
            self._conn.execute(
                "UPDATE jobs SET status = 'pending', lease_until = NULL, last_error = ?, "
                "available_at = ?, updated_at = ? WHERE id = ?",
                (error[:500], now + self.retry_delay(job.attempts), now, job.id))
            return "retry_scheduled"

    def postpone(self, job: Job, delay_s: float, payload: dict | None = None) -> None:
        """Re-schedule without counting a failure (e.g. the source asked us to wait)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'pending', lease_until = NULL, attempts = attempts - 1,"
                " available_at = ?, payload = COALESCE(?, payload), updated_at = ? WHERE id = ?",
                (now + max(0, delay_s), json.dumps(payload) if payload is not None else None,
                 now, job.id))

    def requeue_dead(self, job_id: int | None = None) -> int:
        """Give dead-lettered jobs (one, or all) a fresh retry budget."""
        now = time.time()
        with self._lock:
            sql = ("UPDATE jobs SET status = 'pending', attempts = 0, available_at = ?, "
                   "updated_at = ? WHERE status = 'dead'")
            args: tuple = (now, now)
            if job_id is not None:
                sql += " AND id = ?"
                args += (job_id,)
            try:
                return self._conn.execute(sql, args).rowcount
            except sqlite3.IntegrityError:
                return 0  # an identical periodic job is already queued

    def recover_interrupted(self) -> int:
        """At startup: jobs left 'running' by a previous process are made claimable now."""
        with self._lock:
            return self._conn.execute(
                "UPDATE jobs SET status = 'pending', lease_until = NULL, available_at = ? "
                "WHERE status = 'running'", (time.time(),)).rowcount

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status").fetchall()
        out: dict = {}
        for kind, status, n in rows:
            out.setdefault(status, {})[kind] = n
        return out

    def active(self, limit: int = 5000) -> list[dict]:
        """Pending and running jobs (kind, payload, status, attempts, last error)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, payload, status, attempts, last_error FROM jobs "
                "WHERE status IN ('pending', 'running') ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [{"kind": r[0], "payload": json.loads(r[1]), "status": r[2], "attempts": r[3],
                 "last_error": r[4]} for r in rows]

    def dead_letters(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, payload, attempts, last_error, updated_at FROM jobs "
                "WHERE status = 'dead' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "kind": r[1], "summary": _summary(r[1], json.loads(r[2])),
                 "repo": json.loads(r[2]).get("repo"),
                 "attempts": r[3], "error": r[4],
                 "failed_at": datetime.fromtimestamp(r[5]).astimezone().isoformat()}
                for r in rows]

    # --- state ------------------------------------------------------------------------

    def get_state(self, key: str, default=None):  # noqa: ANN001, ANN201
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:  # noqa: ANN001
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO "
                "UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, default=str), time.time()))

    def delete_state(self, prefix: str) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM kv WHERE key LIKE ?",
                                      (prefix.replace("%", "") + "%",)).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _summary(kind: str, payload: dict) -> str:
    if kind == "fetch":
        return f"{payload.get('job_type', 'fetch')} {payload.get('repo')} {payload.get('kind')}"
    return kind + (f" {payload.get('source')}" if payload.get("source") else "")


class NodeRuntime(Runtime):
    """The single-server runtime: jobs and watermarks in the local SQLite queue."""

    def __init__(self, queue: JobQueue) -> None:
        self.queue = queue

    def enqueue(self, job: FetchJob, *, delay_seconds: int = 0) -> None:
        self.queue.put("fetch", json.loads(job.model_dump_json()), delay_s=delay_seconds)

    def read_watermark(self, key: str) -> datetime | None:
        value = self.queue.get_state("watermark:" + key)
        # A watermark written under another index format would skip unchanged issues/PRs that
        # must be re-extracted: treat it as "never ran" (same rule as the S3 watermark).
        if not value or value.get("index_format") != INDEX_FORMAT_VERSION:
            return None
        return datetime.fromisoformat(value["since"])

    def write_watermark(self, key: str, since: datetime) -> None:
        self.queue.set_state("watermark:" + key, {"since": since.isoformat(),
                                                  "index_format": INDEX_FORMAT_VERSION})
