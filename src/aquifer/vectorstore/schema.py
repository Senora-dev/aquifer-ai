"""The indexed chunk schema, shared by every built-in vector store.

One function turns a :class:`Chunk` into the stored record, one turns a stored record back into
a :class:`SearchResult`, and one evaluates filters in memory. Keeping these in one place means
the OpenSearch adapter and the in-memory store index and filter identically, so offline tests
and the benchmark exercise the same semantics as production.
"""

from __future__ import annotations

from datetime import datetime

from aquifer.core.entities import namespaced_key, normalize_name
from aquifer.core.models import Chunk, SearchResult

# Fields an agent may filter on through the MCP tools.
AGENT_FILTERABLE_FIELDS = frozenset(
    {
        "document_id",
        "source_type",
        "kind",
        "repo",
        "path",
        "author",
        "labels",
        "state",
        "entity_names",
        "entity_types",
        "entity_keys",
        "entity_name_keys",
        "topics",
        "relationship_targets",
        "relationship_types",
        "relationship_target_keys",
        "relationship_source_keys",
        "relationship_origins",
    }
)

# Additional fields used internally by the pipeline (generation cleanup, freshness).
INTERNAL_FILTERABLE_FIELDS = frozenset(
    {"content_hash", "extraction_status", "chunk_id", "generation"}
)

FILTERABLE_FIELDS = AGENT_FILTERABLE_FIELDS | INTERNAL_FILTERABLE_FIELDS

# Keyword fields added after the first release. ``ensure_index`` adds them to an existing
# index's mapping so term filters on them work without re-creating the index.
KEYWORD_FIELDS = (
    "chunk_id",
    "document_id",
    "source_type",
    "kind",
    "repo",
    "url",
    "author",
    "labels",
    "path",
    "revision",
    "state",
    "extraction_status",
    "content_hash",
    "generation",
    "entity_names",
    "entity_types",
    "entity_keys",
    "entity_name_keys",
    "topics",
    "relationship_targets",
    "relationship_types",
    "relationship_target_keys",
    "relationship_source_keys",
    "relationship_origins",
)
DATE_FIELDS = ("created_at", "updated_at", "revision_committed_at", "fetched_at", "indexed_at")
INTEGER_FIELDS = ("chunk_index", "overlap_words", "char_start", "char_end", "line_start",
                  "line_end")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _uniq(values) -> list[str]:  # noqa: ANN001
    return list(dict.fromkeys(v for v in values if v))


def chunk_to_source(chunk: Chunk) -> dict:
    """The stored record for one chunk (vector + text + filterable/provenance metadata)."""
    entities = chunk.entities
    relationships = chunk.relationships
    return {
        "vector": chunk.embedding,
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "document_id": chunk.document_id,
        "chunk_index": chunk.index,
        "overlap_words": chunk.overlap_words,
        "source_type": str(chunk.source_type),
        "kind": str(chunk.kind),
        "repo": chunk.repo,
        "title": chunk.title,
        "url": chunk.url,
        "author": chunk.author,
        "created_at": _iso(chunk.created_at),
        "updated_at": _iso(chunk.updated_at),
        "labels": chunk.labels,
        # Provenance & freshness.
        "path": chunk.path,
        "revision": chunk.revision,
        "revision_committed_at": _iso(chunk.revision_committed_at),
        "fetched_at": _iso(chunk.fetched_at),
        "indexed_at": _iso(chunk.indexed_at),
        "state": chunk.state,
        "extraction_status": chunk.extraction_status,
        "content_hash": chunk.content_hash,
        "generation": chunk.generation,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        # Neutral semantic metadata: full structured objects plus flattened keyword arrays.
        "summary": chunk.summary,
        "entities": [e.model_dump(exclude_none=True) for e in entities],
        "entity_names": [e.name for e in entities],
        "entity_types": [e.type for e in entities],
        "entity_keys": _uniq(namespaced_key(e.type, e.name, e.namespace) for e in entities),
        "entity_name_keys": _uniq(normalize_name(e.name) for e in entities),
        "topics": chunk.topics,
        "relationships": [r.model_dump(exclude_none=True) for r in relationships],
        "relationship_targets": [r.target for r in relationships],
        "relationship_types": [r.type for r in relationships],
        "relationship_target_keys": _uniq(normalize_name(r.target) for r in relationships),
        "relationship_source_keys": _uniq(
            normalize_name(r.source) for r in relationships if r.source
        ),
        "relationship_origins": _uniq(r.origin for r in relationships),
    }


def source_to_result(source: dict, *, score: float, hit_id: str = "") -> SearchResult:
    """Rebuild a :class:`SearchResult` from a stored record."""
    return SearchResult(
        chunk_id=source.get("chunk_id") or hit_id,
        document_id=source.get("document_id", ""),
        score=score or 0.0,
        text=source.get("text", ""),
        title=source.get("title", ""),
        url=source.get("url", ""),
        source_type=source.get("source_type"),
        kind=source.get("kind"),
        repo=source.get("repo"),
        summary=source.get("summary", ""),
        entities=source.get("entities", []),
        topics=source.get("topics", []),
        relationships=source.get("relationships", []),
        chunk_index=source.get("chunk_index"),
        path=source.get("path"),
        revision=source.get("revision"),
        line_start=source.get("line_start"),
        line_end=source.get("line_end"),
        created_at=source.get("created_at"),
        updated_at=source.get("updated_at"),
        revision_committed_at=source.get("revision_committed_at"),
        indexed_at=source.get("indexed_at"),
        state=source.get("state"),
        extraction_status=source.get("extraction_status"),
        content_hash=source.get("content_hash"),
    )


def validate_filters(filters: dict | None, allowed: frozenset[str] = FILTERABLE_FIELDS) -> dict:
    """Reject unknown fields and non-scalar values (no query-DSL passthrough)."""
    if not filters:
        return {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object of field → value(s)")
    clean: dict = {}
    for field, value in filters.items():
        if field not in allowed:
            raise ValueError(f"Unsupported filter field: {field!r}")
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(v, (str, int, float, bool)) for v in values):
            raise ValueError(f"Filter values for {field!r} must be strings, numbers or booleans")
        clean[field] = value
    return clean


def matches_filters(source: dict, filters: dict | None) -> bool:
    """Evaluate term/terms filters against a stored record exactly as OpenSearch would.

    A scalar field matches when equal to the value (or to any value in a list). An array field
    matches when any element matches.
    """
    for field, wanted in (filters or {}).items():
        wanted_values = set(wanted) if isinstance(wanted, list) else {wanted}
        actual = source.get(field)
        actual_values = set(actual) if isinstance(actual, list) else {actual}
        if not (wanted_values & actual_values):
            return False
    return True
