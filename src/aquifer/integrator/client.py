"""Connecting to an external MCP server safely.

- **Network guard.** Every HTTP request (MCP calls, and OAuth discovery, registration and token
  calls) must go to a public address over HTTPS. Private, loopback, link-local (including the
  instance metadata service) and reserved addresses are refused unless the operator allowed
  private networks for that deployment. This stops a source URL, or metadata returned by a
  remote server, from reaching internal services.
- **Approved tools only.** :meth:`McpConnection.call` refuses any tool that is not on the
  source's approved list, whatever the plan or the server says.
- **Bounded results.** Tool results above ``max_result_bytes`` are rejected.
- **Classified failures.** Callers get :class:`NeedsAuthorization`, :class:`SourceRateLimited`,
  :class:`SourceUnavailable` (transient: retried) or :class:`ToolCallError` (the tool reported
  an error) instead of transport internals.

Remote tool names, descriptions and results are untrusted data. They are never executed or
interpreted as instructions; descriptions are only shown to the operator, sanitized.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from aquifer.core.secrets import resolve_secret
from aquifer.integrator.credentials import (
    NeedsAuthorization,
    SecretTokenStorage,
    build_oauth,
    force_refresh,
)
from aquifer.integrator.models import Source

MAX_RESULT_BYTES = 5_000_000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class IntegratorError(Exception):
    """Base class; ``code`` is a stable identifier shown in status and logs."""

    code = "integrator_error"


class UnsafeDestination(IntegratorError):
    code = "unsafe_destination"


class SourceRateLimited(IntegratorError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailable(IntegratorError):
    code = "source_unavailable"


class ToolCallError(IntegratorError):
    code = "tool_error"


class ToolNotApproved(IntegratorError):
    code = "tool_not_approved"


class ResultTooLarge(IntegratorError):
    code = "result_too_large"


def sanitize(text: str | None, limit: int = 300) -> str:
    """Make untrusted remote text safe to display: no control characters, bounded length."""
    text = _CONTROL.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- network guard ------------------------------------------------------------------------

def check_destination(url: str, *, allow_private: bool = False) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise UnsafeDestination(f"not a URL: {sanitize(url, 120)}")
    if allow_private:
        if parsed.scheme not in {"https", "http"}:
            raise UnsafeDestination("only http(s) URLs are allowed")
        return
    if parsed.scheme != "https":
        raise UnsafeDestination(f"{host}: MCP sources must use HTTPS")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SourceUnavailable(f"{host}: DNS lookup failed") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeDestination(
                f"{host} resolves to a non-public address ({ip}); refused. Set "
                "AQUIFER_INTEGRATOR__ALLOW_PRIVATE_NETWORKS=true only for trusted internal "
                "MCP servers.")


# --- results ------------------------------------------------------------------------------

@dataclass
class ToolResult:
    data: object  # parsed JSON (dict/list) or raw text
    text: str
    size: int


def _parse_result(result) -> ToolResult:  # noqa: ANN001
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    text = "\n".join(texts)
    size = len(text.encode())
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        size = max(size, len(json.dumps(structured)))
    if size > MAX_RESULT_BYTES:
        raise ResultTooLarge(f"tool result of {size} bytes exceeds {MAX_RESULT_BYTES}")
    data = structured if structured is not None else text
    # Servers wrap non-object results as {"result": ...} (e.g. a JSON document returned as a
    # string); unwrap it, and parse JSON text.
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, str):
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(data)
    return ToolResult(data=data, text=text, size=size)


_RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b|throttl", re.I)


def _http_status(exc: BaseException) -> int | None:
    """Find an HTTP status inside (possibly grouped) transport errors."""
    stack = [exc]
    while stack:
        e = stack.pop()
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code
        stack.extend(getattr(e, "exceptions", []) or [])
        if e.__cause__:
            stack.append(e.__cause__)
        if e.__context__:
            stack.append(e.__context__)
    return None


def _find(exc: BaseException, kind: type) -> BaseException | None:
    stack = [exc]
    while stack:
        e = stack.pop()
        if isinstance(e, kind):
            return e
        stack.extend(getattr(e, "exceptions", []) or [])
        if e.__cause__:
            stack.append(e.__cause__)
        if e.__context__:
            stack.append(e.__context__)
    return None


def classify_failure(exc: BaseException) -> IntegratorError:
    """Map any connection failure to one of the integrator's error classes."""
    for kind in (NeedsAuthorization, UnsafeDestination, ToolNotApproved, ResultTooLarge,
                 SourceRateLimited, ToolCallError):
        found = _find(exc, kind)
        if found is not None:
            if isinstance(found, NeedsAuthorization):
                err = IntegratorError(str(found))
                err.code = "needs_authorization"
                return err
            return found  # type: ignore[return-value]
    status = _http_status(exc)
    if status in {401, 403}:
        err = IntegratorError(f"the server rejected the credentials (HTTP {status})")
        err.code = "needs_authorization"
        return err
    if status == 429:
        return SourceRateLimited("the server is rate limiting requests (HTTP 429)")
    if status is not None and status >= 500:
        return SourceUnavailable(f"the server returned HTTP {status}")
    if status is not None:
        return ToolCallError(f"the server returned HTTP {status}")
    if _find(exc, (httpx.TransportError, TimeoutError, OSError)) is not None:  # type: ignore[arg-type]
        return SourceUnavailable(f"could not reach the server ({type(exc).__name__})")
    return SourceUnavailable(f"connection failed ({type(exc).__name__})")


# --- the connection -----------------------------------------------------------------------

class McpConnection:
    def __init__(self, session, source: Source) -> None:  # noqa: ANN001
        self.session = session
        self.source = source
        self.server_info: dict = {}

    async def list_tools(self) -> list:
        tools = []
        cursor = None
        for _ in range(50):
            page = await self.session.list_tools(cursor) if cursor else \
                await self.session.list_tools()
            tools.extend(page.tools)
            cursor = getattr(page, "nextCursor", None)
            if not cursor:
                break
        return tools

    async def call(self, tool: str, args: dict, *, timeout: float = 120) -> ToolResult:
        if tool not in self.source.approved_tools:
            raise ToolNotApproved(f"tool {sanitize(tool, 80)!r} is not approved for "
                                  f"source {self.source.name!r}")
        try:
            result = await asyncio.wait_for(self.session.call_tool(tool, args), timeout)
        except TimeoutError as exc:
            raise SourceUnavailable(f"{tool} timed out after {timeout:.0f}s") from exc
        except IntegratorError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport / protocol errors
            raise classify_failure(exc) from None
        parsed = _parse_result(result)
        if result.isError:
            message = sanitize(parsed.text, 400)
            if _RATE_RE.search(message):
                raise SourceRateLimited(f"{tool}: {message}")
            raise ToolCallError(f"{tool}: {message}")
        return parsed


def _headers(source: Source) -> dict[str, str]:
    headers = {k: v for k, v in source.headers.items() if k.lower() != "authorization"}
    if source.auth.type == "bearer":
        headers["Authorization"] = f"Bearer {resolve_secret(source.auth.secret_ref)}"
    return headers


@asynccontextmanager
async def connect(source: Source, *, redirect_uri: str = "", allow_private: bool = False,
                  redirect_handler=None, callback_handler=None):  # noqa: ANN001
    """Open an MCP session to ``source`` (yields :class:`McpConnection`).

    Unattended (no handlers): an OAuth source whose access token was rejected gets one forced
    refresh; if that fails too, :class:`NeedsAuthorization` is raised.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    check_destination(source.url, allow_private=allow_private)

    async def guard(request: httpx.Request) -> None:
        check_destination(str(request.url), allow_private=allow_private)

    def factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:  # noqa: ANN001
        return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30, read=120),
                                 auth=auth, follow_redirects=False,
                                 event_hooks={"request": [guard]})

    attempts = 2 if source.auth.type == "oauth" and redirect_handler is None else 1
    for attempt in range(attempts):
        entered = False
        auth = None
        storage = None
        if source.auth.type == "oauth":
            storage = SecretTokenStorage(source.auth.secret_ref)
            auth = build_oauth(source.url, storage, redirect_uri=redirect_uri,
                               scope=source.auth.scope, redirect_handler=redirect_handler,
                               callback_handler=callback_handler)
        try:
            async with streamablehttp_client(source.url, headers=_headers(source), timeout=30,
                                             sse_read_timeout=120, auth=auth,
                                             httpx_client_factory=factory) as (r, w, _), \
                    ClientSession(r, w) as session:
                init = await session.initialize()
                conn = McpConnection(session, source)
                conn.server_info = {"name": sanitize(init.serverInfo.name, 80),
                                    "version": sanitize(init.serverInfo.version, 80),
                                    "protocol": init.protocolVersion}
                entered = True
                yield conn
                return
        except BaseException as exc:  # noqa: BLE001 - classified below
            if entered:
                # A failure of the caller's own work: surface it (unwrapped from the
                # transport's exception group) without retrying the connection.
                ours = _find(exc, IntegratorError)
                if ours is not None and ours is not exc:
                    raise ours from None
                raise
            err = classify_failure(exc)
            retry = (err.code == "needs_authorization" and attempt + 1 < attempts
                     and storage is not None and force_refresh(storage))
            if retry:
                continue
            if err is exc:
                raise
            raise err from None
