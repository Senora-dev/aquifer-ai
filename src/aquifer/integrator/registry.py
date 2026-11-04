"""Persistent state of the MCP Integrator (SQLite in the server's data directory).

- ``sources``: each connection's configuration (no secrets; credentials are references).
- ``runs``: one row per sync run, with progress counters and the outcome.
- ``streams``: per-stream checkpoints (high-water mark, last complete listing).
- ``seen``: ids listed during a run, for deletion reconciliation of complete listings.
- ``oauth``: pending authorizations, shared between the CLI and the server process.

Configuration changes take effect on the next run; nothing needs redeploying.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aquifer.integrator.models import Source

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    config TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,            -- running | succeeded | partial | failed
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS runs_source ON runs(source, started_at);
CREATE TABLE IF NOT EXISTS streams (
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, stream)
);
CREATE TABLE IF NOT EXISTS seen (
    run_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY (run_id, stream, document_id)
);
CREATE TABLE IF NOT EXISTS oauth (
    state TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    created_at REAL NOT NULL,
    auth_url TEXT,
    status TEXT NOT NULL,            -- starting | waiting | code_received | succeeded | failed
    oauth_state TEXT,
    code TEXT,
    error TEXT
);
"""


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


class Registry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                     timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- sources ----------------------------------------------------------------------

    def save(self, source: Source) -> None:
        if source.created_at is None:
            source.created_at = datetime.now(UTC)
        self._q("INSERT INTO sources (name, config, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET config = excluded.config, "
                "updated_at = excluded.updated_at",
                (source.name, source.model_dump_json(), time.time()))

    def get(self, name: str) -> Source | None:
        rows = self._q("SELECT config FROM sources WHERE name = ?", (name,))
        return Source.model_validate_json(rows[0][0]) if rows else None

    def require(self, name: str) -> Source:
        source = self.get(name)
        if source is None:
            raise KeyError(f"no source named {name!r} (see: aquifer sources list)")
        return source

    def all(self) -> list[Source]:
        return [Source.model_validate_json(r[0])
                for r in self._q("SELECT config FROM sources ORDER BY name")]

    def delete(self, name: str) -> None:
        with self._lock:
            for table in ("sources", "streams"):
                col = "name" if table == "sources" else "source"
                self._conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (name,))
            self._conn.execute("DELETE FROM seen WHERE run_id IN "
                               "(SELECT run_id FROM runs WHERE source = ?)", (name,))
            self._conn.execute("DELETE FROM runs WHERE source = ?", (name,))
            self._conn.execute("DELETE FROM oauth WHERE source = ?", (name,))

    def active_scopes(self) -> list[str]:
        """Namespaces readable through Aquifer's MCP tools (enabled sources)."""
        return [s.scope for s in self.all() if s.enabled]

    # --- runs -------------------------------------------------------------------------

    def start_run(self, source: str) -> str:
        run_id = uuid.uuid4().hex[:16]
        self._q("INSERT INTO runs (run_id, source, started_at, status) VALUES (?, ?, ?, ?)",
                (run_id, source, time.time(), "running"))
        return run_id

    def add_stats(self, run_id: str, **counts: int) -> None:
        with self._lock:
            row = self._conn.execute("SELECT stats FROM runs WHERE run_id = ?",
                                     (run_id,)).fetchone()
            if row is None:
                return
            stats = json.loads(row[0])
            for k, v in counts.items():
                stats[k] = stats.get(k, 0) + v
            self._conn.execute("UPDATE runs SET stats = ? WHERE run_id = ?",
                               (json.dumps(stats), run_id))

    def note_stream(self, run_id: str, stream: str, status: str, error: str | None = None
                    ) -> None:
        """Record a stream's outcome within a run (done / failed / skipped)."""
        with self._lock:
            row = self._conn.execute("SELECT stats FROM runs WHERE run_id = ?",
                                     (run_id,)).fetchone()
            if row is None:
                return
            stats = json.loads(row[0])
            stats.setdefault("streams", {})[stream] = {"status": status,
                                                       **({"error": error} if error else {})}
            self._conn.execute("UPDATE runs SET stats = ? WHERE run_id = ?",
                               (json.dumps(stats), run_id))

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self._q("UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE run_id = ?",
                (time.time(), status, error, run_id))
        self._q("DELETE FROM seen WHERE run_id = ?", (run_id,))

    def run(self, run_id: str) -> dict | None:
        rows = self._q("SELECT run_id, source, started_at, finished_at, status, stats, error "
                       "FROM runs WHERE run_id = ?", (run_id,))
        return self._run_row(rows[0]) if rows else None

    def runs(self, source: str, limit: int = 5) -> list[dict]:
        rows = self._q("SELECT run_id, source, started_at, finished_at, status, stats, error "
                       "FROM runs WHERE source = ? ORDER BY started_at DESC LIMIT ?",
                       (source, limit))
        return [self._run_row(r) for r in rows]

    @staticmethod
    def _run_row(r: tuple) -> dict:
        return {"run_id": r[0], "source": r[1], "started_at": _iso(r[2]),
                "finished_at": _iso(r[3]), "status": r[4], "stats": json.loads(r[5]),
                "error": r[6]}

    def last_success(self, source: str) -> str | None:
        rows = self._q("SELECT MAX(finished_at) FROM runs WHERE source = ? AND status = "
                       "'succeeded'", (source,))
        return _iso(rows[0][0]) if rows and rows[0][0] else None

    # --- stream checkpoints and seen ids ------------------------------------------------

    def stream_state(self, source: str, stream: str) -> dict:
        rows = self._q("SELECT state FROM streams WHERE source = ? AND stream = ?",
                       (source, stream))
        return json.loads(rows[0][0]) if rows else {}

    def set_stream_state(self, source: str, stream: str, state: dict) -> None:
        self._q("INSERT INTO streams (source, stream, state) VALUES (?, ?, ?) ON CONFLICT"
                "(source, stream) DO UPDATE SET state = excluded.state",
                (source, stream, json.dumps(state, default=str)))

    def mark_seen(self, run_id: str, stream: str, document_ids: list[str]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen (run_id, stream, document_id) VALUES (?, ?, ?)",
                [(run_id, stream, d) for d in document_ids])

    def seen(self, run_id: str, stream: str) -> set[str]:
        return {r[0] for r in self._q("SELECT document_id FROM seen WHERE run_id = ? AND "
                                       "stream = ?", (run_id, stream))}

    # --- OAuth handoff ------------------------------------------------------------------

    def oauth_begin(self, source: str) -> str:
        """A pending authorization requested by the CLI; the server fills in the URL."""
        request_id = uuid.uuid4().hex
        self._q("DELETE FROM oauth WHERE source = ? OR created_at < ?",
                (source, time.time() - 3600))
        self._q("INSERT INTO oauth (state, source, created_at, status) VALUES (?, ?, ?, ?)",
                (request_id, source, time.time(), "starting"))
        return request_id

    def oauth_update(self, request_id: str, **fields: str | None) -> None:
        allowed = {"auth_url", "status", "code", "error", "oauth_state"}
        sets = [f"{k} = ?" for k in fields if k in allowed]
        if sets:
            self._q(f"UPDATE oauth SET {', '.join(sets)} WHERE state = ?",
                    (*[fields[k] for k in fields if k in allowed], request_id))

    def oauth_get(self, request_id: str) -> dict | None:
        rows = self._q("SELECT state, source, created_at, auth_url, status, code, error, "
                       "oauth_state FROM oauth WHERE state = ?", (request_id,))
        if not rows:
            return None
        r = rows[0]
        return {"request_id": r[0], "source": r[1], "created_at": r[2], "auth_url": r[3],
                "status": r[4], "code": r[5], "error": r[6], "oauth_state": r[7]}

    def oauth_deliver(self, oauth_state: str, *, code: str | None, error: str | None) -> bool:
        """Called by the HTTP callback. Only a waiting request with this exact state matches
        (states are single-use: the row leaves 'waiting')."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM oauth WHERE oauth_state = ? AND status = 'waiting' AND "
                "created_at > ?", (oauth_state, time.time() - 900)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE oauth SET status = ?, code = ?, error = ? WHERE state = ?",
                ("code_received" if code else "failed", code, error, row[0]))
            return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
