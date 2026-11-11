"""Agent credentials: access tokens issued for MCP clients from the Agents page.

The tokens themselves live where every Aquifer token lives (``mcp.auth_tokens_ref``: SSM on
AWS, a 0600 file locally). This module keeps only non-secret facts about the ones issued for
agents, in ``<node.data_dir>/agent-tokens.json``: who created them and when, and when they were
revoked. An agent credential:

- calls the MCP tools, which are read-only (search and read indexed context);
- cannot sign in to the UI, so it cannot reach the admin actions there (sources, tokens);
- never takes the name of an admin principal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aquifer.core.config import Settings

# The MCP clients whose setup Aquifer documents, and how each was verified. Bearer tokens in a
# header are what Aquifer accepts; clients that only do OAuth for remote servers cannot use it.
CLIENTS = [
    {"id": "claude-code", "name": "Claude Code (CLI)", "transport": "Streamable HTTP",
     "auth": "Authorization header from an environment variable", "tested": True},
    {"id": "python-sdk", "name": "MCP Python SDK", "transport": "Streamable HTTP",
     "auth": "Authorization header", "tested": True},
    {"id": "json", "name": "Other clients with HTTP + custom headers", "transport":
     "Streamable HTTP", "auth": "Authorization header", "tested": False},
]
OAUTH_ONLY_NOTE = ("Clients that connect to remote MCP servers only through OAuth (for example "
                   "custom connectors added in a chat app's settings) cannot send Aquifer's "
                   "bearer token. Aquifer's MCP endpoint does not offer OAuth.")


def _path(settings: Settings) -> Path:
    return Path(settings.node.data_dir) / "agent-tokens.json"


def load(settings: Settings) -> dict[str, dict]:
    path = _path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(settings: Settings, data: dict) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_issued(settings: Settings, name: str, by: str) -> dict:
    data = load(settings)
    data[name] = {"kind": "agent", "created_at": _now(), "created_by": by, "revoked_at": None}
    _save(settings, data)
    return data[name]


def record_revoked(settings: Settings, name: str, by: str) -> None:
    data = load(settings)
    entry = data.setdefault(name, {"kind": "token", "created_at": None, "created_by": None})
    entry.update(revoked_at=_now(), revoked_by=by)
    _save(settings, data)


def is_agent(settings: Settings, name: str) -> bool:
    entry = load(settings).get(name)
    return bool(entry) and entry.get("kind") == "agent"


def setup(url: str, name: str) -> dict:
    """Copyable setup for the documented clients. The token is never inlined: each snippet
    reads it from the AQUIFER_TOKEN environment variable."""
    keychain = f"aquifer-{name}"
    return {
        "env": {
            "macos": (f'security add-generic-password -U -a "$USER" -s {keychain} -w\n'
                      "# paste the token at the prompt (it is not echoed), then in your shell "
                      "profile:\n"
                      f'export AQUIFER_TOKEN="$(security find-generic-password -a "$USER" -s '
                      f'{keychain} -w)"'),
            "other": ("# Load it from your password manager or secret store; avoid typing it on "
                      "a command line\nexport AQUIFER_TOKEN=\"$(<command that prints the "
                      "token>)\""),
        },
        "claude-code": (f"claude mcp add --transport http aquifer {url} "
                        "--header 'Authorization: Bearer ${AQUIFER_TOKEN}'"),
        "json": json.dumps({"mcpServers": {"aquifer": {
            "type": "http", "url": url,
            "headers": {"Authorization": "Bearer ${AQUIFER_TOKEN}"}}}}, indent=2),
        "python-sdk": (
            "import asyncio, os\n"
            "import httpx\n"
            "from mcp import ClientSession\n"
            "from mcp.client.streamable_http import streamable_http_client\n\n"
            "async def main():\n"
            '    auth = {"Authorization": "Bearer " + os.environ["AQUIFER_TOKEN"]}\n'
            "    async with httpx.AsyncClient(headers=auth, timeout=60) as http:\n"
            f'        async with streamable_http_client("{url}", http_client=http) as (r, w, _):\n'
            "            async with ClientSession(r, w) as s:\n"
            "                await s.initialize()\n"
            "                print([t.name for t in (await s.list_tools()).tools])\n"
            '                print(await s.call_tool("list_sources", {}))\n\n'
            "asyncio.run(main())"),
        "verify": (f"curl -sS {url} -H \"Authorization: Bearer $AQUIFER_TOKEN\" "
                   "-H 'Content-Type: application/json' "
                   "-H 'Accept: application/json, text/event-stream' "
                   "-d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\","
                   "\"params\":{\"name\":\"list_sources\",\"arguments\":{}}}'"),
    }
