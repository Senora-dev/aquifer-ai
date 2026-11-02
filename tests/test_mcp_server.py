"""MCP server: authentication config, bearer auth, and an end-to-end call over real HTTP."""

import asyncio
import socket
import threading
import time

import pytest

from aquifer.core.config import GitHubSettings, McpSettings, Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Interceptor
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.mcp.auth import AuthConfigError, authenticate, validate_auth_config
from aquifer.mcp.tools import ContextTools
from aquifer.vectorstore.memory import InMemoryVectorStore

TOKEN_A = "a" * 40
TOKEN_B = "b" * 40


def test_no_auth_is_refused_on_non_loopback_hosts():
    with pytest.raises(AuthConfigError, match="loopback"):
        validate_auth_config(McpSettings(auth_mode="none", host="0.0.0.0"))
    assert validate_auth_config(McpSettings(auth_mode="none", host="127.0.0.1")) == {}


def test_token_mode_requires_strong_tokens():
    with pytest.raises(AuthConfigError, match="No MCP auth tokens"):
        validate_auth_config(McpSettings())
    with pytest.raises(AuthConfigError, match="at least"):
        validate_auth_config(McpSettings(auth_tokens={"x": "short"}))


def test_authenticate_maps_token_to_principal():
    tokens = {"ci-agent": TOKEN_A, "dev": TOKEN_B}
    assert authenticate(f"Bearer {TOKEN_B}", tokens) == "dev"
    assert authenticate(f"bearer {TOKEN_A}", tokens) == "ci-agent"
    assert authenticate(f"Bearer {TOKEN_A}x", tokens) is None
    assert authenticate(f"Basic {TOKEN_A}", tokens) is None
    assert authenticate(None, tokens) is None


def test_auth_tokens_are_not_in_settings_repr():
    assert TOKEN_A not in repr(McpSettings(auth_tokens={"x": TOKEN_A}))


class RecordingInterceptor(Interceptor):
    seen: list = []

    def authorize(self, principal, action, resource):
        RecordingInterceptor.seen.append((principal, action))
        return principal != "blocked"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    import uvicorn

    from aquifer.mcp.server import create_http_app

    port = _free_port()
    settings = Settings(
        github=GitHubSettings(repo_allowlist=["o/r"]),
        mcp=McpSettings(host="127.0.0.1", port=port,
                        auth_tokens={"agent-a": TOKEN_A, "blocked": TOKEN_B}),
    )
    tools = ContextTools(embedder=HashingEmbedder(32), vector_store=InMemoryVectorStore(),
                         interceptors=InterceptorPipeline([RecordingInterceptor()]),
                         settings=settings)
    app = create_http_app(settings, tools=tools)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def test_unauthenticated_requests_are_rejected(server_url):
    import httpx

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"accept": "application/json, text/event-stream"}
    r = httpx.post(server_url, json=body, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = httpx.post(server_url, json=body,
                   headers={**headers, "authorization": "Bearer " + "c" * 40})
    assert r.status_code == 401


async def _call(url, token, tool, args):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (
        read, write, _
    ), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool(tool, args)
        return [t.name for t in listed.tools], result


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_authenticated_client_lists_tools_and_principal_reaches_interceptors(server_url):
    RecordingInterceptor.seen.clear()
    names, result = asyncio.run(_call(server_url, TOKEN_A, "find_related",
                                      {"entity": "billing-api"}))
    assert set(names) == {"search_context", "get_document", "list_sources", "find_related",
                          "list_entities"}
    assert not result.isError
    assert ("agent-a", "find_related") in RecordingInterceptor.seen


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_interceptor_denial_is_returned_as_tool_error(server_url):
    _, result = asyncio.run(_call(server_url, TOKEN_B, "search_context", {"query": "x"}))
    assert result.isError
