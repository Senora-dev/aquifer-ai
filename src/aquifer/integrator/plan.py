"""Which tools may be used, and turning a sample listing into a deterministic stream.

Read-only gate (applied to the tool list the server reports):

- ``read_only``: annotated ``readOnlyHint: true`` and not ``destructiveHint``. Approvable.
- ``unannotated_read``: no annotations, and the name reads like a read (get/list/search/...).
  Approvable only when the operator explicitly allows unannotated tools for that source.
- ``write``: annotated destructive or not read-only, or named like a write (create, update,
  delete, send, ...). Never approvable, whatever the operator asks.
- ``unknown``: anything else. Not approvable.

Annotations are hints from the remote server, so the name check applies even to tools marked
read-only: a tool called ``delete_page`` is never called.

Stream proposal is deterministic: one sample call of a list tool, then common field names are
recognized (``id``/``number``/``key``, ``title``/``name``, ``body``/``description``/``content``,
``updated_at``/``updatedAt``/``last_edited_time``, …). The operator previews the result before
anything is ingested, and can edit the plan.
"""

from __future__ import annotations

import re

from aquifer.core.models import DocumentKind
from aquifer.integrator.client import sanitize
from aquifer.integrator.models import FieldMap, Pagination, Source, StreamSpec

_WRITE = re.compile(
    r"(^|[_\-.])(create|update|delete|remove|write|send|post|put|patch|add|set|edit|modify|"
    r"merge|close|archive|move|upload|execute|run|invoke|assign|transition|comment|reply|"
    r"publish|approve|dismiss|lock|unlock|fork|push|enable|disable|cancel|trigger|rerun|"
    r"submit|insert|upsert|rename|restore|revert|star|follow|mark|resolve)(?=$|[_\-.])")
_READ = re.compile(r"^(get|list|search|fetch|read|query|find|retrieve|describe|show|lookup|"
                   r"view)([_\-.]|$|[A-Z])", re.I)


def classify_tool(tool) -> str:  # noqa: ANN001 - mcp.types.Tool
    name = tool.name
    ann = tool.annotations
    if _WRITE.search(_snake(name)):
        return "write"
    if ann is not None and (ann.destructiveHint or ann.readOnlyHint is False):
        return "write"
    if ann is not None and ann.readOnlyHint:
        return "read_only"
    if _READ.search(name):
        return "unannotated_read"
    return "unknown"


def _snake(name: str) -> str:
    """``createIssue`` → ``create_issue`` (the write check matches whole words)."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def describe_tools(tools: list) -> list[dict]:
    """Tool list for the operator: name, safety class, parameters, sanitized description."""
    out = []
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        out.append({
            "name": sanitize(t.name, 128),
            "class": classify_tool(t),
            "params": sorted(props)[:20],
            "required": sorted((t.inputSchema or {}).get("required", []))[:20],
            "description": sanitize(t.description, 200),
        })
    return sorted(out, key=lambda d: (d["class"] != "read_only", d["name"]))


def approvable(tool, *, allow_unannotated: bool) -> bool:  # noqa: ANN001
    cls = classify_tool(tool)
    return cls == "read_only" or (cls == "unannotated_read" and allow_unannotated)


# --- paths --------------------------------------------------------------------------------

def get_path(data, path: str):  # noqa: ANN001, ANN201
    """Resolve ``a.b`` (``a|b`` = first non-empty alternative) in parsed JSON."""
    if not path:
        return data
    for alt in path.split("|"):
        cur = data
        for part in alt.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                cur = None
                break
        if cur not in (None, "", []):
            return cur
    return None


def find_items(data, path: str = "") -> list[dict]:  # noqa: ANN001
    """The list of items at ``path`` (a null list counts as empty)."""
    value = get_path(data, path) if path else data
    if value is None:
        return []
    if isinstance(value, dict) and not path:
        value = _first_list(value)
    if not isinstance(value, list):
        raise ValueError(f"no list at {path or 'the result root'!r}")
    return [v for v in value if isinstance(v, dict)]


def _first_list(obj: dict):  # noqa: ANN202
    for key, value in obj.items():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return value
        if value is None and key in {"items", "results", "data", "nodes", "issues",
                                     "discussions", "pages", "records"}:
            return []
    return None


# --- inference ----------------------------------------------------------------------------

_CANDIDATES = {
    "id": ["id", "number", "key", "identifier", "uuid", "tag_name", "slug"],
    "title": ["title", "name", "summary", "subject", "tag_name", "identifier"],
    "body": ["body", "description", "content", "text", "markdown", "summary_text"],
    "url": ["html_url", "url", "web_url", "webUrl", "permalink", "link", "self"],
    "updated_at": ["updated_at", "updatedAt", "last_edited_time", "modified_at", "modified",
                   "updated", "lastModified"],
    "created_at": ["created_at", "createdAt", "created_time", "published_at", "created"],
    "author": ["author.login", "user.login", "author.name", "creator.name", "created_by.name",
               "author", "creator"],
    "state": ["state", "status", "status.name"],
    "labels": ["labels"],
}
_CURSOR_PARAMS = ["after", "cursor", "start_cursor", "next_cursor", "pageToken", "page_token",
                  "nextPageToken"]
_NEXT_PATHS = ["pageInfo.endCursor", "page_info.end_cursor", "next_cursor", "nextCursor",
               "next_page_token", "nextPageToken", "cursor", "pagination.next_cursor"]
_HAS_MORE = ["pageInfo.hasNextPage", "page_info.has_next_page", "has_more", "hasMore",
             "pagination.has_more"]
_SIZE_PARAMS = ["perPage", "per_page", "page_size", "pageSize", "limit", "first", "maxResults"]


def _items_path(sample) -> str:  # noqa: ANN001
    if isinstance(sample, list):
        return ""
    if isinstance(sample, dict):
        for key, value in sample.items():
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return key
            if isinstance(value, dict):
                for k2, v2 in value.items():
                    if isinstance(v2, list) and v2 and isinstance(v2[0], dict):
                        return f"{key}.{k2}"
    raise ValueError("the sample result contains no list of items")


def _pick(items: list[dict], names: list[str]) -> str:
    for name in names:
        if any(get_path(item, name) not in (None, "") for item in items[:5]):
            return name
    return ""


def infer_stream(name: str, tool, args: dict, sample, *, kind: DocumentKind) -> StreamSpec:  # noqa: ANN001
    """A stream spec from one sample result of ``tool`` called with ``args``."""
    props = (tool.inputSchema or {}).get("properties", {})
    items_path = _items_path(sample)
    items = find_items(sample, items_path)
    if not items:
        raise ValueError(f"{tool.name} returned no items to learn fields from; pick arguments "
                         "that return at least one item")
    fields = {k: _pick(items, v) for k, v in _CANDIDATES.items()}
    if not fields["id"]:
        raise ValueError("could not recognize an id field; edit the plan to set fields.id")
    pagination = Pagination()
    cursor_param = next((p for p in _CURSOR_PARAMS if p in props), "")
    size_param = next((p for p in _SIZE_PARAMS if p in props), "")
    if cursor_param:
        pagination = Pagination(type="cursor", param=cursor_param,
                                next=next((p for p in _NEXT_PATHS if get_path(sample, p)
                                           is not None or _has_key(sample, p)), ""),
                                has_more=next((p for p in _HAS_MORE if _has_key(sample, p)), ""),
                                size_param=size_param)
    elif "page" in props:
        pagination = Pagination(type="page", param="page", start=1, size_param=size_param)
    elif "offset" in props:
        pagination = Pagination(type="offset", param="offset", start=0, size_param=size_param)
    if pagination.size_param:
        pagination.size = 50
    ordered = _ordered_by_update(props, args)
    return StreamSpec(
        name=name, kind=kind, tool=tool.name, args=args, items=items_path,
        pagination=pagination, fields=FieldMap(**{k: v for k, v in fields.items() if v}),
        ordered_by_updated_desc=ordered,
        # A listing is only "complete" if the operator says so (see `aquifer sources plan`).
        enumeration="partial",
    )


def _has_key(data, path: str) -> bool:  # noqa: ANN001
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _ordered_by_update(props: dict, args: dict) -> bool:
    order = " ".join(str(v) for k, v in args.items() if k.lower() in {
        "orderby", "order_by", "sort", "sort_by", "order"}).lower()
    direction = " ".join(str(v) for k, v in args.items() if k.lower() in {
        "direction", "order", "sort_direction"}).lower()
    return "updat" in order and ("desc" in direction or "desc" in order)


# --- validation ---------------------------------------------------------------------------

def validate_plan(source: Source, tools: list, *, allow_unannotated: bool = False) -> list[str]:
    """Problems that make the plan unsafe or unrunnable against the server's current tools."""
    by_name = {t.name: t for t in tools}
    problems = []
    for tool_name in sorted(source.plan.tools()):
        tool = by_name.get(tool_name)
        if tool is None:
            problems.append(f"tool {tool_name!r} is not offered by the server")
        elif not approvable(tool, allow_unannotated=allow_unannotated):
            problems.append(f"tool {tool_name!r} is classified {classify_tool(tool)!r}; only "
                            "read-only tools can be used")
    for stream in source.plan.streams:
        if stream.pagination.type == "cursor" and not stream.pagination.next:
            problems.append(f"stream {stream.name!r}: cursor pagination needs `next`")
    return problems
