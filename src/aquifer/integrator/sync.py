"""Deterministic execution of a source's sync plan, one page at a time.

Each page is its own queue job (``mcp_page``), so a sync inherits the job queue's guarantees:
an interrupted run resumes from its last page (the cursor is in the job), rate-limited pages are
postponed, transient failures are retried with backoff, and nothing is lost silently.

Per page:

1. Call the stream's list tool with the page cursor (approved tools only).
2. Map items to documents (source content plus provenance: server, tool, stream, item id,
   fetch time). Items whose update time equals the indexed one are skipped without further
   calls. Otherwise the detail tool fetches the full content when the plan has one.
3. Run new or changed documents through the shared ingestion pipeline. Unchanged content is
   skipped by content hash, so duplicate deliveries and re-runs are cheap and idempotent.
4. Remember the ids seen (for complete listings) and enqueue the next page, or finish the
   stream.

Deletions are inferred only from a **complete** listing that reached its end in this run.
- When the plan has a detail tool, each missing item is confirmed with it; only an explicit
  "not found" deletes.
- A listing that came back empty while items are indexed, or that would remove most of a
  stream, is not trusted: it more likely reflects a permission or API change. Nothing is
  deleted and the run reports why.
- Partial listings (search, filtered views) never delete anything.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aquifer.core.errors import log_event
from aquifer.core.models import Document, SourceType
from aquifer.integrator.client import McpConnection, ToolCallError, sanitize
from aquifer.integrator.models import Source, StreamSpec
from aquifer.integrator.plan import find_items, get_path

logger = logging.getLogger(__name__)

MAX_TITLE = 500
MAX_BODY = 200_000
MAX_VERIFY_PER_RUN = 100


@dataclass
class PageResult:
    next_cursor: object | None = None
    done: bool = False
    fetched: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    detail_calls: int = 0
    max_updated: str | None = None
    stopped_at_high_water: bool = False
    truncated: bool = False
    seen: list[str] = field(default_factory=list)


# --- mapping ------------------------------------------------------------------------------

def _text(value) -> str:  # noqa: ANN001
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _time(value) -> datetime | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value  # epoch ms or s
            return datetime.fromtimestamp(ts, UTC)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _labels(value) -> list[str]:  # noqa: ANN001
    if not isinstance(value, list):
        return []
    out = []
    for v in value[:50]:
        name = v.get("name") if isinstance(v, dict) else v
        if isinstance(name, str) and name.strip():
            out.append(sanitize(name, 80))
    return out


def _url(value) -> str:  # noqa: ANN001
    value = _text(value)
    return value if re.match(r"^https?://", value) else ""


def item_id(stream: StreamSpec, item: dict) -> str:
    value = get_path(item, stream.fields.id)
    if value in (None, ""):
        raise ValueError(f"item without an id (fields.id = {stream.fields.id!r})")
    return re.sub(r"\s+", "_", _text(value))[:200]


def document_id(source: Source, stream: StreamSpec, external_id: str) -> str:
    return Document.make_id(SourceType.MCP, source.scope, stream.kind,
                            f"{stream.name}/{external_id}")


def stream_prefix(source: Source, stream: StreamSpec) -> str:
    return document_id(source, stream, "")


def to_document(source: Source, stream: StreamSpec, item: dict, *, body: str | None = None,
                fetched_at: datetime | None = None) -> Document:
    f = stream.fields
    ext = item_id(stream, item)
    content = body if body is not None else _text(get_path(item, f.body) if f.body else "")
    state = _text(get_path(item, f.state)) if f.state else ""
    return Document(
        id=document_id(source, stream, ext),
        source_type=SourceType.MCP,
        external_id=f"{stream.name}/{ext}",
        kind=stream.kind,
        repo=source.scope,
        title=sanitize(_text(get_path(item, f.title)) if f.title else "", MAX_TITLE),
        body=content[:MAX_BODY],
        url=_url(get_path(item, f.url)) if f.url else "",
        author=sanitize(_text(get_path(item, f.author)), 120) or None if f.author else None,
        created_at=_time(get_path(item, f.created_at)) if f.created_at else None,
        updated_at=_time(get_path(item, f.updated_at)) if f.updated_at else None,
        labels=_labels(get_path(item, f.labels)) if f.labels else [],
        state=sanitize(state, 40).lower() or None,
        fetched_at=fetched_at or datetime.now(UTC),
        # Flat scalars: they are stored with every chunk (provenance survives re-processing).
        metadata={"mcp_source": source.name, "mcp_server": source.url,
                  "mcp_tool": stream.tool, "mcp_stream": stream.name, "mcp_item_id": ext,
                  "mcp_item_id_is_int": isinstance(get_path(item, f.id), int),
                  **({"mcp_detail_tool": stream.detail.tool} if stream.detail else {})},
    )


def _detail_args(stream: StreamSpec, item: dict, ext: str) -> dict:
    return _spec_args(stream.detail, stream, item, ext)  # type: ignore[arg-type]


def _spec_args(spec, stream: StreamSpec, item: dict, ext: str) -> dict:  # noqa: ANN001
    def fill(v):  # noqa: ANN001, ANN202
        if isinstance(v, str):
            if v == "{id}":
                raw = get_path(item, stream.fields.id)
                return raw if raw is not None else ext
            m = re.fullmatch(r"\{([A-Za-z0-9_.|]+)\}", v)
            if m:
                return get_path(item, m.group(1))
        return v
    return {k: fill(v) for k, v in spec.args.items()}


# --- one page -----------------------------------------------------------------------------

def page_args(stream: StreamSpec, cursor) -> dict:  # noqa: ANN001
    args = dict(stream.args)
    p = stream.pagination
    if p.size_param:
        args[p.size_param] = p.size
    if p.type == "cursor" and cursor:
        args[p.param] = cursor
    elif p.type in {"page", "offset"}:
        args[p.param] = cursor if cursor is not None else p.start
    return args


def _next_cursor(stream: StreamSpec, data, cursor, n_items: int):  # noqa: ANN001, ANN202
    p = stream.pagination
    if p.type == "none" or n_items == 0:
        return None
    if p.type == "cursor":
        if p.has_more and get_path(data, p.has_more) in (False, None, "false"):
            return None
        nxt = get_path(data, p.next)
        return nxt if nxt not in (None, "", cursor) else None
    current = cursor if cursor is not None else p.start
    if p.size_param and n_items < p.size:
        return None
    return current + 1 if p.type == "page" else current + n_items


async def run_page(conn: McpConnection, source: Source, stream: StreamSpec, cursor,  # noqa: ANN001
                   *, high_water: datetime | None, overlap_s: int, pipeline,  # noqa: ANN001
                   remaining_items: int) -> PageResult:
    result = await conn.call(stream.tool, page_args(stream, cursor))
    items = find_items(result.data, stream.items)
    out = PageResult(fetched=len(items))
    if len(items) > remaining_items:
        items = items[:remaining_items]
        out.truncated = True
    now = datetime.now(UTC)
    docs: list[tuple[Document, dict]] = []
    for item in items:
        try:
            doc = to_document(source, stream, item, fetched_at=now)
        except ValueError as exc:
            out.failed += 1
            log_event(logger, "mcp_item_unmappable", level=logging.WARNING, source=source.name,
                      stream=stream.name, error=sanitize(str(exc), 200))
            continue
        if (stream.ordered_by_updated_desc and high_water and doc.updated_at
                and doc.updated_at < high_water - timedelta(seconds=overlap_s)):
            out.stopped_at_high_water = True
            break
        docs.append((doc, item))
        out.seen.append(doc.id)
        if doc.updated_at and (out.max_updated is None
                               or doc.updated_at.isoformat() > out.max_updated):
            out.max_updated = doc.updated_at.isoformat()

    states = pipeline.vector_store.get_indexed_state([d.id for d, _ in docs]) if docs else {}
    for doc, item in docs:
        state = states.get(doc.id)
        # Same source version already indexed: no detail call, no processing.
        if (state is not None and doc.updated_at and state.updated_at
                and state.updated_at == doc.updated_at and stream.detail is not None):
            out.unchanged += 1
            continue
        if stream.detail is not None:
            ext = doc.metadata["mcp_item_id"]
            try:
                detail = await conn.call(stream.detail.tool, _detail_args(stream, item, ext))
            except ToolCallError as exc:
                if re.search(stream.detail.not_found, str(exc), re.I):
                    out.unchanged += 1  # listed but already gone; reconciliation handles it
                    continue
                raise
            out.detail_calls += 1
            body = detail.text if not stream.detail.body else _text(
                get_path(detail.data, stream.detail.body))
            doc = doc.model_copy(update={"body": body[:MAX_BODY]})
        outcome = pipeline.process_document(doc, indexed_state=state, lookup=False)
        if outcome == "indexed":
            out.indexed += 1
        else:
            out.unchanged += 1

    out.next_cursor = (None if out.stopped_at_high_water or out.truncated
                       else _next_cursor(stream, result.data, cursor, len(items)))
    out.done = out.next_cursor is None
    return out


# --- deletions ----------------------------------------------------------------------------

async def reconcile_stream(conn: McpConnection, source: Source, stream: StreamSpec,
                           seen: set[str], store) -> dict:  # noqa: ANN001
    """Remove documents a complete listing no longer contains, with evidence (see module)."""
    prefix = stream_prefix(source, stream)
    indexed = {d for d in (store.list_document_ids({"repo": source.scope,
                                                   "kind": stream.kind.value}) or set())
               if d.startswith(prefix)}
    missing = sorted(indexed - seen)
    report = {"indexed": len(indexed), "listed": len(seen), "missing": len(missing),
              "deleted": 0, "kept": 0}
    if not missing:
        return report
    checker = stream.verify or stream.detail
    # Without a per-item check, a listing that suddenly lost everything (or most items) more
    # likely reflects a permission or API change than mass deletion: delete nothing.
    if checker is None and not seen:
        report["suspended"] = "the listing returned no items while items are indexed"
        return report
    if checker is None and len(missing) > max(10, len(indexed) // 2):
        report["suspended"] = (f"{len(missing)} of {len(indexed)} items would be removed; "
                               "that looks like a permission or API change")
        return report
    doomed = []
    if checker is None:
        doomed = missing
    else:
        for doc_id in missing[:MAX_VERIFY_PER_RUN]:
            ext = doc_id[len(prefix):]
            raw = _raw_id(store, doc_id) or ext
            try:
                await conn.call(checker.tool,
                                _spec_args(checker, stream, {_id_key(stream): raw}, ext))
                report["kept"] += 1  # still exists; it was only missing from the listing
            except ToolCallError as exc:
                if re.search(checker.not_found, str(exc), re.I):
                    doomed.append(doc_id)
                else:
                    report["kept"] += 1
        if len(missing) > MAX_VERIFY_PER_RUN:
            report["deferred"] = len(missing) - MAX_VERIFY_PER_RUN
    if doomed:
        store.delete_documents(doomed)
    report["deleted"] = len(doomed)
    log_event(logger, "mcp_reconciled", source=source.name, stream=stream.name, **{
        k: v for k, v in report.items() if isinstance(v, int)})
    return report


def _id_key(stream: StreamSpec) -> str:
    return stream.fields.id.split("|")[0]


def _raw_id(store, document_id_: str):  # noqa: ANN001, ANN202
    """The item id as the source spells it (numbers stay numbers for detail calls)."""
    rows = store.get_by_document(document_id_)
    if not rows:
        return None
    meta = rows[0].get("source_metadata") or {}
    value = meta.get("mcp_item_id")
    if value is not None and meta.get("mcp_item_id_is_int"):
        try:
            return int(value)
        except ValueError:
            return value
    return value
