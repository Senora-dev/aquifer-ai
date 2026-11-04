"""A persistent single-server index: SQLite on disk, vectors searched in memory.

This is the default store of the small deployment profile (one server, no OpenSearch). It
stores exactly the records the OpenSearch adapter stores (via the shared schema module) and
inherits filter, facet and retry semantics from :class:`InMemoryVectorStore`, so the retrieval
tools, the benchmark and the tests behave identically on all three stores.

- **Durable.** Every write is one SQLite transaction (WAL journal), so a crash or power loss
  leaves either the old or the new generation of a document, never a partial one.
- **Fast enough for a small deployment.** Normalized vectors are held in one float32 matrix;
  a query is one matrix-vector product plus a top-k selection (measured on a t4g.small at
  baseline CPU: 4 ms at 5,000 chunks, 22 ms at 25,000). Records are kept in memory without
  their vectors.
- **Thread-safe.** The MCP server and ingestion workers share one instance in one process.
- **Multi-process aware.** If another process (e.g. ``aquifer`` CLI maintenance) writes to the
  same file, the next read notices (``PRAGMA data_version``) and reloads.

Sizing (measured, 1024 dimensions, ~3 KB of text per chunk): ~480 MiB peak for 25,000
chunks, ~900 MiB for 50,000. Use the OpenSearch scale profile well beyond that.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from aquifer.core.models import SearchResult
from aquifer.vectorstore.memory import InMemoryVectorStore
from aquifer.vectorstore.schema import (
    chunk_to_source,
    generation_rank,
    matches_filters,
    retry_due_record,
    source_to_result,
    validate_filters,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    rid TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    record TEXT NOT NULL,
    vector BLOB
);
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
"""


class SqliteVectorStore(InMemoryVectorStore):
    def __init__(self, path: str | os.PathLike) -> None:
        super().__init__(None)
        self._lock = threading.RLock()
        self._db_path = Path(path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False,
                                     isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._data_version: int | None = None
        self._load()

    # --- in-memory vector matrix --------------------------------------------------

    def _reset_vectors(self) -> None:
        self._dim: int | None = None
        self._mat = np.zeros((0, 0), dtype=np.float32)
        self._alive = np.zeros(0, dtype=bool)
        self._row: dict[str, int] = {}
        self._rids: list[str | None] = []  # row -> record id
        self._free: list[int] = []
        self._used = 0

    def _put_vector(self, rid: str, vector: np.ndarray | None) -> None:
        if vector is None or vector.size == 0:
            return
        if self._dim is None:
            self._dim = int(vector.size)
            self._mat = np.zeros((1024, self._dim), dtype=np.float32)
            self._alive = np.zeros(1024, dtype=bool)
        if vector.size != self._dim:
            raise ValueError(f"vector has {vector.size} dimensions; the index has {self._dim}")
        norm = float(np.linalg.norm(vector))
        unit = vector / norm if norm else vector
        if self._free:
            row = self._free.pop()
        else:
            if self._used == self._mat.shape[0]:
                grow = max(1024, self._mat.shape[0])
                self._mat = np.vstack([self._mat, np.zeros((grow, self._dim), np.float32)])
                self._alive = np.concatenate([self._alive, np.zeros(grow, bool)])
            row = self._used
            self._rids.append(None)
            self._used += 1
        self._mat[row] = unit
        self._alive[row] = True
        self._row[rid] = row
        self._rids[row] = rid

    def _drop_vector(self, rid: str) -> None:
        row = self._row.pop(rid, None)
        if row is not None:
            self._alive[row] = False
            self._rids[row] = None
            self._mat[row] = 0.0
            self._free.append(row)

    # --- persistence --------------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            self._records = {}
            self._reset_vectors()
            for rid, record, blob in self._conn.execute(
                "SELECT rid, record, vector FROM chunks"
            ):
                self._records[rid] = json.loads(record)
                if blob:
                    self._put_vector(rid, np.frombuffer(blob, dtype=np.float32))
            self._data_version = self._version()

    def _version(self) -> int:
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _refresh(self) -> None:
        """Reload if another connection (another process) committed since the last read."""
        with self._lock:
            if self._version() != self._data_version:
                self._load()

    def _save(self) -> None:  # the base class's JSON persistence is not used
        return None

    def _insert(self, chunks: Sequence) -> list[tuple[str, dict, np.ndarray | None]]:
        rows = []
        for chunk in chunks:
            record = chunk_to_source(chunk)
            raw = record.pop("vector", None)
            vector = np.asarray(raw, dtype=np.float32) if raw else None
            rows.append((uuid.uuid4().hex, record, vector))
        self._conn.executemany(
            "INSERT INTO chunks (rid, document_id, record, vector) VALUES (?, ?, ?, ?)",
            [(rid, rec.get("document_id") or "", json.dumps(rec),
              vec.tobytes() if vec is not None else None) for rid, rec, vec in rows],
        )
        return rows

    def _apply_inserts(self, rows) -> None:  # noqa: ANN001
        for rid, record, vector in rows:
            self._records[rid] = record
            self._put_vector(rid, vector)

    def _delete_rids(self, rids: list[str]) -> None:
        for i in range(0, len(rids), 500):
            batch = rids[i : i + 500]
            self._conn.execute(
                f"DELETE FROM chunks WHERE rid IN ({','.join('?' * len(batch))})", batch)

    def _apply_deletes(self, rids: list[str]) -> None:
        for rid in rids:
            self._records.pop(rid, None)
            self._drop_vector(rid)

    def _transaction(self, fn):  # noqa: ANN001, ANN202
        with self._lock:
            self._refresh()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn()
            except BaseException:
                self._conn.execute("ROLLBACK")
                self._load()  # memory may hold part of the failed change
                raise
            self._conn.execute("COMMIT")
            self._data_version = self._version()
            return result

    # --- writes -------------------------------------------------------------------

    def upsert(self, chunks: Sequence) -> None:
        if not chunks:
            return

        def work():  # noqa: ANN202
            self._apply_inserts(self._insert(chunks))

        self._transaction(work)

    def replace_document(self, document_id: str, chunks: Sequence) -> None:
        if not chunks:
            self.delete_by_document(document_id)
            return
        def work():  # noqa: ANN202
            # One transaction: the new generation and the removal of older ones commit together.
            # Keep only the highest-ranked generation (possibly not ours: a newer write may have
            # committed first), so concurrent writers converge in any order.
            self._apply_inserts(self._insert(chunks))
            mine = [(rid, rec) for rid, rec in self._records.items()
                    if rec.get("document_id") == document_id]
            best = max(generation_rank(rec) for _, rec in mine)
            stale = [rid for rid, rec in mine if generation_rank(rec) < best]
            self._delete_rids(stale)
            self._apply_deletes(stale)

        self._transaction(work)

    def delete_by_filter(self, filters: dict) -> int:
        filters = validate_filters(filters)
        if not filters:
            raise ValueError("delete_by_filter requires at least one filter")

        def work() -> int:
            doomed = [rid for rid, r in self._records.items() if matches_filters(r, filters)]
            self._delete_rids(doomed)
            self._apply_deletes(doomed)
            return len(doomed)

        return self._transaction(work)

    # --- reads --------------------------------------------------------------------

    def _matching(self, filters: dict | None) -> list[tuple[str, dict]]:
        filters = validate_filters(filters)
        with self._lock:
            self._refresh()
            return [(rid, r) for rid, r in self._records.items() if matches_filters(r, filters)]

    def query(
        self, vector: Sequence[float], k: int, filters: dict | None = None
    ) -> list[SearchResult]:
        if k <= 0:
            return []
        q = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        usable = bool(norm) and self._dim == q.size
        with self._lock:
            if filters:
                matching = self._matching(filters)
                rids = [rid for rid, _ in matching]
                rows = np.array([self._row.get(rid, -1) for rid in rids], dtype=np.int64)
                scores = np.zeros(len(rids), dtype=np.float32)
                has = rows >= 0
                if usable and has.any():
                    scores[has] = self._mat[rows[has]] @ (q / norm)
            else:
                self._refresh()
                live = np.flatnonzero(self._alive[: self._used])
                scores = (self._mat[live] @ (q / norm) if usable
                          else np.zeros(len(live), np.float32))
                vectorless = ([rid for rid in self._records if rid not in self._row]
                              if len(self._records) > len(self._row) else [])
            if not filters:
                # Keep only candidates that can reach the top k (ties included), then rank them
                # exactly like the reference store: score (6 decimals), then chunk id.
                if len(scores) > k:
                    kth = np.partition(scores, len(scores) - k)[len(scores) - k]
                    keep = np.flatnonzero(scores >= kth - 2e-6)
                else:
                    keep = np.arange(len(scores))
                cand = [(float(scores[j]), self._rids[live[j]]) for j in keep]
                cand += [(0.0, rid) for rid in vectorless]
            else:
                cand = list(zip(scores.tolist(), rids, strict=True))
            ranked = sorted(
                cand, key=lambda t: (-round(t[0], 6),
                                     self._records[t[1]].get("chunk_id") or t[1]))[:k]
            return [source_to_result(self._public(self._records[rid]), score=round(s, 6),
                                     hit_id=rid) for s, rid in ranked]

    def get_document_vectors(self, document_id: str) -> dict[str, list[float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record, vector FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()
        out: dict[str, list[float]] = {}
        for record, blob in rows:
            if blob:
                out[json.loads(record)["text"]] = np.frombuffer(blob, np.float32).tolist()
        return out

    def documents_due_for_retry(self, now, limit: int = 200) -> list[str] | None:  # noqa: ANN001
        now_iso = now.isoformat()
        with self._lock:
            self._refresh()
            ids = sorted({r["document_id"] for r in self._records.values()
                          if r.get("document_id") and retry_due_record(r, now_iso)})
        return ids[:limit]

    def stats(self) -> dict:
        with self._lock:
            self._refresh()
            documents = {r.get("document_id") for r in self._records.values()}
            files = [self._db_path, self._db_path.with_name(self._db_path.name + "-wal")]
            return {"chunks": len(self._records), "documents": len(documents - {None}),
                    "dimensions": self._dim, "path": str(self._db_path),
                    "bytes_on_disk": sum(f.stat().st_size for f in files if f.exists())}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
