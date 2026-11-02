"""Minimal generic MCP client for Aquifer's streamable-HTTP endpoint (uses the `mcp` SDK).

    AQUIFER_MCP_URL=http://127.0.0.1:8080/mcp python examples/mcp/python_client.py billing-api
    AQUIFER_MCP_URL=http://<internal-nlb>/mcp AQUIFER_MCP_TOKEN=... python ... billing-api
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(entity: str) -> None:
    url = os.environ.get("AQUIFER_MCP_URL", "http://127.0.0.1:8080/mcp")
    token = os.environ.get("AQUIFER_MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("find_related", {"entity": entity})
        payload = json.loads(result.content[0].text)
        for edge in payload["relationships"]:
            where = ", ".join(
                f"{ev.get('repository')}:{ev.get('path') or ev.get('document_id')}"
                f"{'#L' + str(ev['line_start']) if ev.get('line_start') else ''}"
                for ev in edge["evidence"]
            )
            print(f"{edge['source']} --{edge['type']}--> {edge['target']}  "
                  f"[{', '.join(edge['origins'])}]  {where}")
        print("evidence_status:", payload["evidence_status"])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "billing-api"))
