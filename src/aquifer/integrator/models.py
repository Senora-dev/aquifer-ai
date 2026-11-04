"""What the MCP Integrator stores: sources (connections) and their sync plans.

A **source** is one connection to an external MCP server: where it is, how to authenticate,
and a **sync plan**. A plan is a list of **streams**; each stream says which approved read-only
tool lists items, how it paginates, how item fields map to Aquifer documents, and what the
stream can and cannot detect. The plan is executed deterministically; no model runs during a
sync.

Coverage is computed from the plan rather than claimed by it, so status and previews can say
exactly what synchronization a stream provides.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from aquifer.core.models import DocumentKind

SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

SourceState = Literal["needs_authorization", "ready", "active", "paused", "needs_reauth",
                      "error"]


class Pagination(BaseModel):
    """How a list tool pages through results."""

    type: Literal["none", "cursor", "page", "offset"] = "none"
    # Tool argument that carries the cursor / page number / offset.
    param: str = ""
    # cursor: where the next cursor is in the result (dotted path); empty or missing = done.
    next: str = ""
    # cursor: optional boolean path that says whether more pages exist.
    has_more: str = ""
    # page/offset: first value and page size (page size also sent as `size_param`).
    start: int = 1
    size: int = 50
    size_param: str = ""


class FieldMap(BaseModel):
    """Dotted paths into an item (``a.b``); ``a|b`` takes the first non-empty."""

    id: str
    title: str = ""
    body: str = ""
    url: str = ""
    updated_at: str = ""
    created_at: str = ""
    author: str = ""
    state: str = ""
    labels: str = ""


class DetailSpec(BaseModel):
    """Optional per-item call for the full content (when the list returns summaries)."""

    tool: str
    # Arguments; "{id}" (or "{field.path}") is replaced with the item's value.
    args: dict = Field(default_factory=dict)
    # Where the body is in the detail result; "" = the whole text result.
    body: str = ""
    # The detail tool reporting an error that matches this pattern means "item does not exist".
    # Used as deletion evidence; anything else is treated as an error, never as a deletion.
    not_found: str = r"not found|could not resolve|404|does not exist|no such"


class StreamSpec(BaseModel):
    name: str
    kind: DocumentKind = DocumentKind.RECORD
    tool: str
    args: dict = Field(default_factory=dict)
    # Where the item list is in the result ("" = the result itself is the list).
    items: str = ""
    pagination: Pagination = Field(default_factory=Pagination)
    fields: FieldMap
    detail: DetailSpec | None = None
    # Optional lookup used only to confirm a deletion (when the list already has the content).
    # Defaults to `detail`.
    verify: DetailSpec | None = None
    # complete: the tool lists every item of a container (e.g. all releases of a repository),
    # so an item missing from a finished listing may have been deleted. partial: a search or
    # filtered view; absence proves nothing and deletions are never inferred.
    enumeration: Literal["complete", "partial"] = "partial"
    # Results come newest-updated first, so paging can stop at the previous high-water mark.
    ordered_by_updated_desc: bool = False
    # Hard limits per run (protect the source's rate limits and the index).
    max_items: int = 5000
    max_pages: int = 200

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", v):
            raise ValueError("stream names are lowercase letters, digits, '-' or '_'")
        return v

    @field_validator("tool")
    @classmethod
    def _tool(cls, v: str) -> str:
        if not TOOL_NAME_RE.fullmatch(v):
            raise ValueError(f"invalid tool name {v!r}")
        return v

    def coverage(self) -> dict:
        """What this stream can detect, stated plainly (shown in previews and status)."""
        has_updated = bool(self.fields.updated_at)
        if self.ordered_by_updated_desc and has_updated:
            changes = "new and changed items, incrementally (listing ordered by update time)"
        elif has_updated:
            changes = ("new and changed items; every run lists all items and compares update "
                       "times and content")
        else:
            changes = ("new items and content edits, by re-listing every run and comparing "
                       "content (the source exposes no update time)")
        checker = self.verify or self.detail
        if self.enumeration == "complete":
            deletions = ("items missing from a complete listing are removed"
                         + (f" after {checker.tool} confirms they no longer exist"
                            if checker else ""))
            if self.ordered_by_updated_desc:
                deletions += " (incremental runs stop early; deletions are checked on the " \
                             "periodic full listing)"
        else:
            deletions = "not detected (partial listing; absence is not evidence of deletion)"
        return {
            "stream": self.name,
            "tool": self.tool,
            "enumeration": self.enumeration,
            "changes": changes,
            "deletions": deletions,
            "limits": f"at most {self.max_items} items and {self.max_pages} pages per run",
        }


class SyncPlan(BaseModel):
    streams: list[StreamSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> SyncPlan:
        names = [s.name for s in self.streams]
        if len(names) != len(set(names)):
            raise ValueError("stream names must be unique")
        return self

    def tools(self) -> set[str]:
        out = set()
        for s in self.streams:
            out.add(s.tool)
            for extra in (s.detail, s.verify):
                if extra:
                    out.add(extra.tool)
        return out


class AuthConfig(BaseModel):
    # none: public server. bearer: a static token (PAT / API key) at `secret_ref`.
    # oauth: OAuth 2.1 authorization code + PKCE; tokens stored (and renewed) at `secret_ref`.
    type: Literal["none", "bearer", "oauth"] = "none"
    secret_ref: str = ""
    # OAuth only: requested scopes (empty = what the server advertises).
    scope: str = ""
    # OAuth only, for servers without dynamic client registration (e.g. Slack): a client you
    # registered with the provider. The secret, if any, is at `client_secret_ref`.
    client_id: str = ""
    client_secret_ref: str = ""


class Source(BaseModel):
    name: str
    provider: str = "custom"
    url: str
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Extra request headers (non-secret, e.g. GitHub's X-MCP-Readonly).
    headers: dict[str, str] = Field(default_factory=dict)
    plan: SyncPlan = Field(default_factory=SyncPlan)
    # Tools the operator approved for this source (read-only). Nothing else is ever called.
    approved_tools: list[str] = Field(default_factory=list)
    # Also accept tools without MCP annotations whose names read like reads (get/list/...).
    # Off by default; write-like names are refused regardless.
    allow_unannotated: bool = False
    schedule_hours: int = 24
    enabled: bool = False
    state: SourceState = "needs_authorization"
    created_at: datetime | None = None
    # Who connected it (operator label), shown in status for accountability.
    connected_by: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SOURCE_NAME_RE.fullmatch(v):
            raise ValueError("source names are 2-41 lowercase letters, digits or '-', "
                             "starting with a letter")
        return v

    @property
    def scope(self) -> str:
        """The namespace of this source's documents (the `repo` field of the index)."""
        return f"mcp/{self.name}"
