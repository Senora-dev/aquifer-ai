"""An in-process vector store for tests, the retrieval benchmark, and the no-AWS local mode.

It stores exactly the records the OpenSearch adapter stores (via the shared schema module) and
evaluates filters with the same term semantics, so behavior verified here carries over. It is
**not** intended for production: search is a linear cosine scan, and persistence (optional) is a
single JSON file rewritten on every change.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path

from aquifer.core.interfaces import VectorStore
from aquifer.core.models import IndexedState, SearchResult
from aquifer.vectorstore.schema import (
    DATE_FIELDS,
    FILTERABLE_FIELDS,
    chunk_rank,
    chunk_to_source,
    generation_rank,
    matches_filters,
    retry_due_record,
    source_to_result,
    validate_filters,
)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryVectorStore(VectorStore):
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._records: dict[str, dict] = {}  # system id -> stored record
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._records = json.loads(self._path.read_text())

    # --- persistence -----------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._records))
        tmp.replace(self._path)

    def __len__(self) -> int:
        return len(self._records)

    # --- writes ----------------------------------------------------------

    def ensure_index(self) -> None:
        return None

    def upsert(self, chunks: Sequence) -> None:
        with self._lock:
            for chunk in chunks:
                # System-generated ids, exactly like an AOSS Classic vector collection.
                self._records[uuid.uuid4().hex] = chunk_to_source(chunk)
            self._save()

    def replace_document(self, document_id: str, chunks: Sequence) -> None:
        if not chunks:
            self.delete_by_document(document_id)
            return
        self.upsert(chunks)
        ours = chunk_rank(chunks[0])
        with self._lock:
            for rid, rec in list(self._records.items()):
                if (rec.get("document_id") == document_id
                        and rec.get("generation") != chunks[0].generation
                        and generation_rank(rec) < ours):
                    del self._records[rid]
            self._save()

    def delete_by_filter(self, filters: dict) -> int:
        filters = validate_filters(filters)
        if not filters:
            raise ValueError("delete_by_filter requires at least one filter")
        with self._lock:
            doomed = [rid for rid, r in self._records.items() if matches_filters(r, filters)]
            for rid in doomed:
                del self._records[rid]
            self._save()
        return len(doomed)

    def delete_by_document(self, document_id: str) -> None:
        self.delete_by_filter({"document_id": document_id})

    def delete_documents(self, document_ids: Sequence[str]) -> None:
        if document_ids:
            self.delete_by_filter({"document_id": list(document_ids)})

    # --- reads -----------------------------------------------------------

    def _matching(self, filters: dict | None) -> list[tuple[str, dict]]:
        filters = validate_filters(filters)
        return [(rid, r) for rid, r in self._records.items() if matches_filters(r, filters)]

    @staticmethod
    def _public(record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "vector"}

    def query(
        self, vector: Sequence[float], k: int, filters: dict | None = None
    ) -> list[SearchResult]:
        scored = [
            (_cosine(vector, r.get("vector") or []), rid, r) for rid, r in self._matching(filters)
        ]
        # Ties broken by id so results are deterministic.
        scored.sort(key=lambda t: (-t[0], t[2].get("chunk_id") or t[1]))
        return [
            source_to_result(self._public(r), score=round(s, 6), hit_id=rid)
            for s, rid, r in scored[:k]
        ]

    def search_by_metadata(self, filters: dict, k: int) -> list[SearchResult]:
        rows = self._matching(filters)
        # Most recently updated first (unknown last), then document/chunk order — matching the
        # OpenSearch adapter's sort. Two stable sorts keep this readable.
        rows.sort(key=lambda t: (t[1].get("document_id") or "", t[1].get("chunk_index") or 0))
        rows.sort(key=lambda t: t[1].get("updated_at") or "", reverse=True)
        return [source_to_result(self._public(r), score=1.0, hit_id=rid) for rid, r in rows[:k]]

    def get_by_document(self, document_id: str) -> list[dict]:
        rows = [self._public(r) for _, r in self._matching({"document_id": document_id})]
        rows.sort(key=lambda r: r.get("chunk_index") or 0)
        return rows

    def get_indexed_state(self, document_ids: Sequence[str]) -> dict[str, IndexedState]:
        out: dict[str, IndexedState] = {}
        newest: dict[str, dict] = {}
        for _, r in self._matching({"document_id": list(document_ids)} if document_ids else None):
            if not document_ids:
                break
            cur = newest.get(r["document_id"])
            if cur is None or (r.get("indexed_at") or "") > (cur.get("indexed_at") or ""):
                newest[r["document_id"]] = r
        for doc_id, r in newest.items():
            out[doc_id] = IndexedState(
                document_id=doc_id,
                content_hash=r.get("content_hash"),
                updated_at=r.get("updated_at"),
                extraction_status=r.get("extraction_status"),
                extraction_attempts=r.get("extraction_attempts") or 0,
                extraction_error=r.get("extraction_error"),
                next_retry_at=r.get("next_retry_at"),
            )
        return out

    def get_document_vectors(self, document_id: str) -> dict[str, list[float]]:
        return {r["text"]: r["vector"] for _, r in self._matching({"document_id": document_id})
                if r.get("vector")}

    def documents_due_for_retry(self, now, limit: int = 200) -> list[str] | None:  # noqa: ANN001
        now_iso = now.isoformat()
        ids = sorted({r["document_id"] for r in self._records.values()
                      if r.get("document_id") and retry_due_record(r, now_iso)})
        return ids[:limit]

    def list_document_ids(self, filters: dict, limit: int = 10_000) -> set[str] | None:
        ids = sorted({r.get("document_id") for _, r in self._matching(filters)} - {None})
        return set(ids[:limit])

    def facet(
        self, field: str, filters: dict | None = None, size: int = 100
    ) -> list[tuple[str, int]] | None:
        if field not in FILTERABLE_FIELDS:
            raise ValueError(f"Unsupported facet field: {field!r}")
        docs: dict[str, set[str]] = {}
        for _, r in self._matching(filters):
            value = r.get(field)
            for v in value if isinstance(value, list) else [value]:
                if v is not None:
                    docs.setdefault(v, set()).add(r.get("document_id"))
        ranked = sorted(docs.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
        return [(k, len(v)) for k, v in ranked[:size]]

    def max_value(self, field: str, filters: dict | None = None):  # noqa: ANN201
        if field not in DATE_FIELDS:
            raise ValueError(f"Unsupported max field: {field!r}")
        values = [r.get(field) for _, r in self._matching(filters) if r.get(field)]
        return max(values) if values else None
