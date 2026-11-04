#!/usr/bin/env python3
"""End-to-end check of the MCP Integrator against GitHub's real MCP server.

Creates a uniquely marked discussion and release in a test repository (with `gh`), then
verifies, through Aquifer's own MCP endpoint:
1. both are imported, with provenance
2. an edit propagates
3. deleting both at the source removes them from Aquifer (each confirmed by the source)

Everything it creates is deleted at the end, even on failure.

    python integrator_e2e.py --url http://127.0.0.1:18080/mcp --token … \\
        --repo owner/aquifer-test-payments --source eng-github \\
        --aquifer "deploy/aws/aquifer-aws run"        # or ".venv/bin/aquifer"

The source must already exist with the discussions and releases templates for --repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
import time
import uuid
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)
results: list[dict] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if not ok and detail else ""),
          flush=True)
    return bool(ok)


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def gh_graphql(query: str, **variables: str) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-f", f"{k}={v}"]
    return json.loads(sh(*args))["data"]


def aquifer(cli: str, *args: str) -> str:
    return sh(*shlex.split(cli), *args)


def sync_and_wait(cli: str, source: str, *, full: bool = False, timeout: int = 300) -> dict:
    """Trigger a run and wait until a run that started after the trigger has finished."""
    before = time.time()
    aquifer(cli, "sources", "sync", source, *(["--full"] if full else []))
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = json.loads(aquifer(cli, "sources", "status", source, "--json"))[0]
        run = status.get("last_run") or {}
        started = run.get("started_at")
        if started and run.get("status") != "running":
            if datetime.fromisoformat(started).timestamp() >= before - 5:
                return run
        time.sleep(3)
    raise TimeoutError("the sync did not finish in time")


async def mcp(url: str, token: str, tool: str, **kw) -> object:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (
            r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool(tool, kw)
        if res.isError:
            raise RuntimeError(res.content[0].text)
        items = [json.loads(c.text) for c in res.content]
        return items[0] if len(items) == 1 and tool != "search_context" else items


def call(args, tool: str, **kw):  # noqa: ANN001
    return asyncio.run(mcp(args.url, args.token, tool, **kw))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--aquifer", required=True, help="command prefix that runs the aquifer CLI")
    args = p.parse_args()
    owner, name = args.repo.split("/")
    marker = uuid.uuid4().hex[:10]
    tag = f"e2e-{marker}"
    created: dict = {}
    try:
        repo = gh_graphql("query($o:String!,$n:String!){repository(owner:$o,name:$n){id "
                          "discussionCategories(first:10){nodes{id name}}}}", o=owner, n=name)
        repo = repo["repository"]
        category = next(c["id"] for c in repo["discussionCategories"]["nodes"]
                        if c["name"] == "General")
        d = gh_graphql("mutation($r:ID!,$c:ID!,$t:String!,$b:String!){createDiscussion(input:"
                       "{repositoryId:$r,categoryId:$c,title:$t,body:$b}){discussion{id number}}}",
                       r=repo["id"], c=category, t=f"E2E runbook {marker}",
                       b=f"zebra-{marker}: reporting-api depends on warehouse-db.")
        created["discussion"] = d["createDiscussion"]["discussion"]
        sh("gh", "release", "create", tag, "--repo", args.repo, "--title", f"E2E {marker}",
           "--notes", f"okapi-{marker}: billing-api now uses redis for rate limits.")
        created["release"] = tag

        run = sync_and_wait(args.aquifer, args.source)
        check("sync run succeeded", run["status"] == "succeeded", run)
        hits = call(args, "search_context", query=f"zebra-{marker} warehouse-db", k=5)
        disc = next((h for h in hits if f"zebra-{marker}" in h["text"]), None)
        check("discussion imported and searchable through Aquifer MCP", disc is not None,
              [h["title"] for h in hits])
        hits = call(args, "search_context", query=f"okapi-{marker} release", k=5)
        rel = next((h for h in hits if f"okapi-{marker}" in h["text"]), None)
        check("release notes imported and searchable", rel is not None,
              [h["title"] for h in hits])
        if disc:
            doc = call(args, "get_document", document_id=disc["document_id"])
            via = doc.get("imported_via", {})
            check("provenance: MCP server, tool and source recorded",
                  via.get("tool") == "list_discussions" and via.get("source") == args.source
                  and "githubcopilot" in via.get("mcp_server", ""), via)
            check("provenance: link to the original discussion",
                  doc["url"].endswith(f"/discussions/{created['discussion']['number']}"),
                  doc["url"])
            check("namespace is the source's (mcp/<source>)", doc["repo"] ==
                  f"mcp/{args.source}", doc["repo"])
        sources = call(args, "list_sources")
        sources = sources if isinstance(sources, list) else [sources]
        mine = [s for s in sources if s.get("name") == args.source]
        check("list_sources shows the source with coverage and last sync",
              bool(mine) and mine[0].get("last_successful_sync") and mine[0].get(
                  "sync_coverage"), mine)

        gh_graphql("mutation($id:ID!,$b:String!){updateDiscussion(input:{discussionId:$id,"
                   "body:$b}){discussion{number}}}", id=created["discussion"]["id"],
                   b=f"zebra-{marker}: reporting-api moved to lakehouse-db (edited).")
        time.sleep(2)
        run = sync_and_wait(args.aquifer, args.source)
        if disc:
            doc = call(args, "get_document", document_id=disc["document_id"])
            check("edit propagated (incremental run)", "lakehouse-db" in doc.get("text", "")
                  and "warehouse-db" not in doc.get("text", ""), doc.get("text"))

        gh_graphql("mutation($id:ID!){deleteDiscussion(input:{id:$id}){discussion{number}}}",
                   id=created.pop("discussion")["id"])
        sh("gh", "release", "delete", tag, "--repo", args.repo, "--yes", "--cleanup-tag")
        created.pop("release")
        run = sync_and_wait(args.aquifer, args.source, full=True)
        check("full run after deletions succeeded", run["status"] == "succeeded", run)
        for label, hit in (("discussion", disc), ("release", rel)):
            if hit:
                doc = call(args, "get_document", document_id=hit["document_id"])
                check(f"deletion propagated: {label} gone", doc.get("found") is False, doc)
        check("deletions were confirmed by the source", run["stats"].get("deleted", 0) >= 2,
              run["stats"])
    finally:
        if "discussion" in created:
            gh_graphql("mutation($id:ID!){deleteDiscussion(input:{id:$id}){discussion{number}}}",
                       id=created["discussion"]["id"])
        if "release" in created:
            subprocess.run(["gh", "release", "delete", created["release"], "--repo", args.repo,
                            "--yes", "--cleanup-tag"], capture_output=True)
    failed = [r for r in results if not r["ok"]]
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed)}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
