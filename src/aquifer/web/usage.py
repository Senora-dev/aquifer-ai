"""Which access tokens have been observed calling Aquifer's MCP tools.

Recorded by the MCP server for every tool call, per principal (the token's name), tool and day.
This is the finest level the server can identify: it knows which *token* made a call, not which
person or which agent product holds the token. Calls made from the UI are not recorded here.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    principal TEXT NOT NULL,
    tool TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY (principal, tool, day)
);
"""


class UsageLog:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                     timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def record(self, principal: str | None, tool: str, now: float | None = None) -> None:
        now = now or time.time()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (principal, tool, day, count, last_at) VALUES (?, ?, ?, 1, ?)"
                " ON CONFLICT(principal, tool, day) DO UPDATE SET count = count + 1, "
                "last_at = MAX(last_at, excluded.last_at)",
                (principal or "anonymous", tool, day, now))

    def summary(self, days: int = 7, now: float | None = None) -> dict[str, dict]:
        now = now or time.time()
        since = datetime.fromtimestamp(now - days * 86400, UTC).date().isoformat()
        out: dict[str, dict] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT principal, tool, SUM(CASE WHEN day >= ? THEN count ELSE 0 END), "
                "MAX(last_at), SUM(count) FROM calls GROUP BY principal, tool", (since,)
            ).fetchall()
        for principal, tool, recent, last_at, total in rows:
            entry = out.setdefault(principal, {"last_call_at": None, "calls_recent": 0,
                                               "calls_total": 0, "tools": {}})
            entry["calls_recent"] += recent
            entry["calls_total"] += total
            entry["tools"][tool] = total
            if entry["last_call_at"] is None or last_at > entry["last_call_at"]:
                entry["last_call_at"] = last_at
        for entry in out.values():
            entry["last_call_at"] = datetime.fromtimestamp(entry["last_call_at"],
                                                           UTC).isoformat()
        return out


@lru_cache(maxsize=4)
def _log(path: str) -> UsageLog:
    return UsageLog(path)


def usage_log(data_dir: str) -> UsageLog:
    return _log(str(Path(data_dir) / "usage.db"))
