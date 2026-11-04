"""Credential storage for MCP sources, and OAuth that keeps working unattended.

Credentials live behind a secret reference (``ssm:`` on AWS, ``file:`` elsewhere), never in the
source configuration or in logs.

OAuth uses the MCP SDK's client (discovery, dynamic client registration, PKCE, RFC 8707
resource indicators, issuer checks), with two additions that a scheduled, unattended sync needs:

- The access token's absolute expiry and the discovered authorization-server metadata are
  stored with the tokens. After a restart the client therefore *refreshes* an expired token at
  the right token endpoint, instead of starting an interactive login.
- In unattended runs any step that would need a browser raises :class:`NeedsAuthorization`,
  which marks the source ``needs_reauth`` with an actionable message instead of hanging.
"""

from __future__ import annotations

import json
import time
from typing import Any

from aquifer.core.secrets import SecretNotFoundError, resolve_secret, store_secret


class NeedsAuthorization(Exception):
    """The source needs an operator to (re-)authorize it: `aquifer sources authorize NAME`."""


class SecretTokenStorage:
    """The SDK's TokenStorage protocol, persisted as one JSON secret."""

    def __init__(self, ref: str) -> None:
        self.ref = ref

    # --- raw document -----------------------------------------------------------------

    def load(self) -> dict:
        try:
            return json.loads(resolve_secret(self.ref))
        except SecretNotFoundError:
            return {}

    def save(self, doc: dict) -> None:
        store_secret(self.ref, json.dumps(doc))

    def update(self, **fields: Any) -> None:
        doc = self.load()
        doc.update(fields)
        self.save(doc)

    # --- SDK protocol -----------------------------------------------------------------

    async def get_tokens(self):  # noqa: ANN201
        from mcp.shared.auth import OAuthToken

        doc = self.load()
        return OAuthToken.model_validate(doc["tokens"]) if doc.get("tokens") else None

    async def set_tokens(self, tokens) -> None:  # noqa: ANN001
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        doc = self.load()
        previous = doc.get("tokens") or {}
        data = tokens.model_dump(mode="json", exclude_none=True)
        # Some servers omit the refresh token on refresh: keep the one we have.
        if not data.get("refresh_token") and previous.get("refresh_token"):
            data["refresh_token"] = previous["refresh_token"]
        doc.update(tokens=data, expires_at=expires_at, authorized=True)
        self.save(doc)

    async def get_client_info(self):  # noqa: ANN201
        from mcp.shared.auth import OAuthClientInformationFull

        doc = self.load()
        return (OAuthClientInformationFull.model_validate(doc["client_info"])
                if doc.get("client_info") else None)

    async def set_client_info(self, client_info) -> None:  # noqa: ANN001
        self.update(client_info=client_info.model_dump(mode="json", exclude_none=True))


def build_oauth(server_url: str, storage: SecretTokenStorage, *, redirect_uri: str,
                scope: str = "", redirect_handler=None, callback_handler=None):  # noqa: ANN001, ANN201
    """An OAuth provider for httpx that survives restarts (see module docstring)."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import (
        OAuthClientMetadata,
        OAuthMetadata,
        OAuthToken,
        ProtectedResourceMetadata,
    )

    class AquiferOAuth(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            doc = storage.load()
            # The SDK only knows a token's lifetime when it obtained the token itself; restore
            # it so an expired token is refreshed rather than sent (and answered with 401).
            if doc.get("tokens"):
                self.context.current_tokens = OAuthToken.model_validate(doc["tokens"])
                self.context.token_expiry_time = doc.get("expires_at")
            if doc.get("oauth_metadata"):
                self.context.oauth_metadata = OAuthMetadata.model_validate(doc["oauth_metadata"])
            if doc.get("protected_resource_metadata"):
                self.context.protected_resource_metadata = (
                    ProtectedResourceMetadata.model_validate(doc["protected_resource_metadata"]))
            if doc.get("auth_server_url"):
                self.context.auth_server_url = doc["auth_server_url"]

        async def _handle_token_response(self, response) -> None:  # noqa: ANN001
            await super()._handle_token_response(response)
            self._remember_metadata()

        def _remember_metadata(self) -> None:
            ctx = self.context
            storage.update(
                oauth_metadata=(ctx.oauth_metadata.model_dump(mode="json", exclude_none=True)
                                if ctx.oauth_metadata else None),
                protected_resource_metadata=(
                    ctx.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
                    if ctx.protected_resource_metadata else None),
                auth_server_url=ctx.auth_server_url,
            )

    async def unattended(*_args) -> Any:  # noqa: ANN002
        raise NeedsAuthorization("authorization required")

    metadata = OAuthClientMetadata(
        client_name="Aquifer (self-hosted)",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=scope or None,
    )
    return AquiferOAuth(server_url=server_url, client_metadata=metadata, storage=storage,
                        redirect_handler=redirect_handler or unattended,
                        callback_handler=callback_handler or unattended, timeout=600)


def force_refresh(storage: SecretTokenStorage) -> bool:
    """Mark the stored access token expired so the next request refreshes it.

    Used once when a server rejects a token the client still considered valid (revoked or
    rotated early). Returns False when there is no refresh token to try.
    """
    doc = storage.load()
    if not (doc.get("tokens") or {}).get("refresh_token"):
        return False
    doc["expires_at"] = 1  # in the past (0 would mean "no expiry" to the SDK)
    storage.save(doc)
    return True
