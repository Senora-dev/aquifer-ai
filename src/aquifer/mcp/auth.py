"""Bearer-token authentication for the MCP HTTP endpoint.

Network isolation is not authorization. Every HTTP request to the MCP server must carry
``Authorization: Bearer <token>`` matching one of the configured tokens; the token's name becomes
the request's *principal*, which is passed to every interceptor ``authorize`` call and recorded
in audit events.

Tokens are configured as a JSON object ``{"principal-name": "token", ...}`` in
``AQUIFER_MCP__AUTH_TOKENS``. The CDK stack injects it from Secrets Manager (never as plaintext
in the task definition). Comparison is constant-time.

Permission model (deliberately simple and enforced): every valid token can read every
repository in the configured allowlist — one trust domain. Per-principal source permissions can
be added through the interceptor ``authorize``/``before_query`` hooks; this module does not
pretend to provide them.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from contextvars import ContextVar

from aquifer.core.config import McpSettings
from aquifer.core.errors import log_event

audit_logger = logging.getLogger("aquifer.audit")

MIN_TOKEN_LENGTH = 32

_current_principal: ContextVar[str | None] = ContextVar("aquifer_principal", default=None)


def current_principal() -> str | None:
    """The authenticated principal of the request being served (``None`` if unauthenticated)."""
    return _current_principal.get()


class AuthConfigError(ValueError):
    """The server's authentication configuration is unsafe or incomplete."""


def is_loopback(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_auth_config(settings: McpSettings) -> dict[str, str]:
    """Fail fast on unsafe configurations; return the usable ``{principal: token}`` map."""
    if settings.auth_mode == "none":
        if not is_loopback(settings.host):
            raise AuthConfigError(
                "AQUIFER_MCP__AUTH_MODE=none is only allowed when the server binds a loopback "
                f"address (got host={settings.host!r}). Configure AQUIFER_MCP__AUTH_TOKENS."
            )
        return {}
    tokens = dict(settings.auth_tokens or {})
    if not tokens:
        raise AuthConfigError(
            "No MCP auth tokens configured. Set AQUIFER_MCP__AUTH_TOKENS to a JSON object "
            '{"<principal>": "<token>"} (the deployed stack injects it from Secrets Manager).'
        )
    short = [name for name, tok in tokens.items() if len(tok or "") < MIN_TOKEN_LENGTH]
    if short:
        raise AuthConfigError(
            f"MCP auth tokens must be at least {MIN_TOKEN_LENGTH} characters; too short for: "
            f"{sorted(short)}"
        )
    return tokens


def authenticate(header_value: str | None, tokens: dict[str, str]) -> str | None:
    """Return the principal for a valid ``Bearer`` header, else ``None`` (constant-time)."""
    if not header_value:
        return None
    scheme, _, presented = header_value.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return None
    presented_b = presented.strip().encode()
    match = None
    for principal, token in tokens.items():
        # Compare against every token so timing does not reveal which (if any) matched.
        if hmac.compare_digest(presented_b, token.encode()):
            match = principal
    return match


class BearerAuthMiddleware:
    """ASGI middleware: reject unauthenticated HTTP requests before they reach MCP."""

    def __init__(self, app, tokens: dict[str, str]) -> None:  # noqa: ANN001 - ASGI app
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001 - ASGI
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # lifespan etc.
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        principal = authenticate(headers.get("authorization"), self.tokens)
        if principal is None:
            client = (scope.get("client") or ("unknown",))[0]
            log_event(audit_logger, "mcp_auth_failed", level=logging.WARNING,
                      path=scope.get("path"), client=client,
                      reason="missing" if "authorization" not in headers else "invalid")
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="aquifer"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        token = _current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_principal.reset(token)
