"""Sign-in for the UI, with the same access tokens as MCP.

- ``auth_mode=token``: a person signs in with an Aquifer access token. The server checks it with
  the MCP authenticator and sets an HttpOnly, SameSite=Strict session cookie (HMAC-signed with a
  key kept in the data directory). The token's name is the session's principal. Each request
  re-checks that the token still exists, so revoking a token ends its sessions.
- ``auth_mode=none`` (only allowed on loopback): the person at this machine is principal
  ``local`` with admin rights, as for the stdio server.

Reading is open to every valid token (Aquifer's access model: one trust domain). Changing
sources or tokens requires a principal listed in ``web.admin_principals``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from aquifer.core.config import Settings
from aquifer.mcp.auth import authenticate

COOKIE = "aquifer_session"
LOCAL_PRINCIPAL = "local"


@dataclass
class Principal:
    name: str
    admin: bool
    sid: str


def _key(settings: Settings) -> bytes:
    path = Path(settings.node.data_dir) / "web-session.key"
    if path.is_file():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Sessions:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._key: bytes | None = None
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    @property
    def auth_required(self) -> bool:
        return self.settings.mcp.auth_mode == "token"

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = _key(self.settings)
        return self._key

    def is_admin(self, principal: str) -> bool:
        return (not self.auth_required) or principal in self.settings.web.admin_principals

    # --- sign in -------------------------------------------------------------------

    def throttled(self, client: str, limit: int = 10, window_s: int = 60) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._attempts.get(client, []) if now - t < window_s]
            recent.append(now)
            self._attempts[client] = recent
            return len(recent) > limit

    def sign_in(self, token: str) -> tuple[str, str] | None:
        """Return ``(principal, cookie value)`` for a valid token."""
        principal = authenticate(f"Bearer {token}", self.settings.mcp.auth_tokens)
        if principal is None:
            return None
        payload = {"p": principal, "sid": secrets.token_urlsafe(12),
                   "exp": int(time.time() + self.settings.web.session_hours * 3600),
                   # Bind the session to this token value: rotating the token ends it.
                   "tf": self._fingerprint(token)}
        return principal, self._sign(payload)

    def _fingerprint(self, token: str) -> str:
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()[:16]

    def _sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        mac = hmac.new(self.key, body, hashlib.sha256).hexdigest()
        return f"{body.decode()}.{mac}"

    # --- per request ---------------------------------------------------------------

    def principal(self, cookie: str | None) -> Principal | None:
        if not self.auth_required:
            return Principal(LOCAL_PRINCIPAL, True, "local")
        if not cookie or "." not in cookie:
            return None
        body, _, mac = cookie.rpartition(".")
        expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        except ValueError:
            return None
        if payload.get("exp", 0) < time.time():
            return None
        name = payload.get("p")
        token = self.settings.mcp.auth_tokens.get(name)
        if not token or not hmac.compare_digest(self._fingerprint(token), payload.get("tf", "")):
            return None  # the token was revoked or rotated
        return Principal(name, self.is_admin(name), payload.get("sid", ""))
