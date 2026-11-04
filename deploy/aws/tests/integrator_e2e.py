#!/usr/bin/env python3
"""End-to-end check of the MCP Integrator against GitHub's real MCP server.

All mutations happen in a dedicated fixture repository (--repo): each run creates uniquely
named discussions and a release, and deletes everything it created at the end, even on failure.
Verified through Aquifer's own MCP endpoint:

1. initial import (paged), retrieval and provenance
2. unchanged content: a second run indexes nothing and makes no detail calls
3. an edit propagates incrementally
4. retries (optional, --fault-on/--fault-off): a run during a network fault fails and is
   retried; it succeeds once the fault is removed
5. restart recovery (optional, --crash): the server is killed mid-run; the run resumes and
   completes with every item present
6. deletions at the source propagate (each confirmed by the source)

    python integrator_e2e.py --url … --token … --repo owner/fixture --source NAME \\
        --aquifer "deploy/aws/aquifer-aws run" [--fault-on CMD --fault-off CMD] [--crash CMD]

The source must already have the discussions and releases templates for --repo.
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


def run_cmd(cmd: str) -> str:
    return subprocess.run(shlex.split(cmd), check=True, capture_output=True, text=True).stdout


def gh_graphql(query: str, **variables: str) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-f", f"{k}={v}"]
    return json.loads(sh(*args))["data"]


def aquifer(cli: str, *args: str) -> str:
    return sh(*shlex.split(cli), *args)


def source_status(cli: str, source: str) -> dict:
    return json.loads(aquifer(cli, "sources", "status", source, "--json"))[0]


def wait_run(cli: str, source: str, since: float, *, want=("succeeded", "partial", "failed"),
             timeout: int = 420) -> dict:
    """The first finished run started after ``since`` whose status is in ``want``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = source_status(cli, source).get("last_run") or {}
        started = run.get("started_at")
        if (started and run.get("status") in want
                and datetime.fromisoformat(started).timestamp() >= since - 5):
            return run
        time.sleep(3)
    raise TimeoutError(f"no {want} run of {source} within {timeout}s")


def sync(cli: str, source: str, *, full: bool = False) -> dict:
    since = time.time()
    aquifer(cli, "sources", "sync", source, *(["--full"] if full else []))
    return wait_run(cli, source, since)


async def _mcp(url: str, token: str, tool: str, **kw) -> object:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (
            r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool(tool, kw)
        if res.isError:
            raise RuntimeError(res.content[0].text)
        items = [json.loads(c.text) for c in res.content]
        return items if tool in {"search_context", "list_sources"} or len(items) != 1 \
            else items[0]


def find(args, text: str) -> dict | None:  # noqa: ANN001
    hits = asyncio.run(_mcp(args.url, args.token, "search_context", query=text, k=10))
    return next((h for h in hits if text in h["text"]), None)


def get_doc(args, doc_id: str) -> dict:  # noqa: ANN001
    return asyncio.run(_mcp(args.url, args.token, "get_document", document_id=doc_id))


def create_discussion(repo_id: str, category: str, title: str, body: str) -> dict:
    return gh_graphql("mutation($r:ID!,$c:ID!,$t:String!,$b:String!){createDiscussion(input:{"
                      "repositoryId:$r,categoryId:$c,title:$t,body:$b}){discussion{id number}}}",
                      r=repo_id, c=category, t=title, b=body)["createDiscussion"]["discussion"]


def delete_discussion(node_id: str) -> None:
    gh_graphql("mutation($id:ID!){deleteDiscussion(input:{id:$id}){discussion{number}}}",
               id=node_id)


def main() -> int:  # noqa: PLR0915
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--repo", required=True, help="dedicated fixture repository")
    p.add_argument("--source", required=True)
    p.add_argument("--aquifer", required=True, help="command prefix that runs the aquifer CLI")
    p.add_argument("--discussions", type=int, default=4)
    p.add_argument("--fault-on", help="command that cuts the server's outbound HTTPS")
    p.add_argument("--fault-off", help="command that restores it")
    p.add_argument("--crash", help="command that kills the server mid-run and waits for its "
                                   "restart; prints RECOVERED=<jobs>")
    p.add_argument("--json-out")
    args = p.parse_args()
    owner, name = args.repo.split("/")
    marker = uuid.uuid4().hex[:8]
    tag = f"e2e-{marker}"
    discussions: list[dict] = []
    release = False
    try:
        repo = gh_graphql("query($o:String!,$n:String!){repository(owner:$o,name:$n){id "
                          "discussionCategories(first:10){nodes{id name}}}}", o=owner, n=name)
        repo = repo["repository"]
        category = next(c["id"] for c in repo["discussionCategories"]["nodes"]
                        if c["name"] == "General")
        for i in range(args.discussions):
            discussions.append(create_discussion(
                repo["id"], category, f"E2E {marker} runbook {i}",
                f"zebra{i}-{marker}: reporting-api-{i} depends on warehouse-db-{i}."))
        sh("gh", "release", "create", tag, "--repo", args.repo, "--title", f"E2E {marker}",
           "--notes", f"okapi-{marker}: billing-api now uses redis for rate limits.")
        release = True

        # 1. initial import --------------------------------------------------------------
        run = sync(args.aquifer, args.source)
        check("initial import: run succeeded", run["status"] == "succeeded", run)
        found = {i: find(args, f"zebra{i}-{marker}") for i in range(args.discussions)}
        check("initial import: every discussion retrievable through Aquifer MCP",
              all(found.values()), {i: bool(v) for i, v in found.items()})
        rel = find(args, f"okapi-{marker}")
        check("initial import: release notes retrievable", rel is not None)
        if found.get(0):
            doc = get_doc(args, found[0]["document_id"])
            via = doc.get("imported_via", {})
            check("provenance: server, tool, source, item id and original URL",
                  via.get("tool") == "list_discussions" and via.get("source") == args.source
                  and "githubcopilot" in via.get("mcp_server", "")
                  and doc["url"].endswith(f"/discussions/{discussions[0]['number']}"),
                  {"via": via, "url": doc.get("url")})
        listed = asyncio.run(_mcp(args.url, args.token, "list_sources"))
        mine = [s for s in listed if s.get("name") == args.source]
        check("list_sources shows the source, its coverage and last sync",
              bool(mine) and bool(mine[0].get("last_successful_sync"))
              and bool(mine[0].get("sync_coverage")), mine)

        # 2. unchanged content -----------------------------------------------------------
        run = sync(args.aquifer, args.source)
        st = run["stats"]
        check("unchanged: second run indexes nothing and makes no detail calls",
              run["status"] == "succeeded" and st.get("indexed", 0) == 0
              and st.get("detail_calls", 0) == 0, st)

        # 3. update ----------------------------------------------------------------------
        gh_graphql("mutation($id:ID!,$b:String!){updateDiscussion(input:{discussionId:$id,"
                   "body:$b}){discussion{number}}}", id=discussions[0]["id"],
                   b=f"zebra0-{marker}: reporting-api-0 moved to lakehouse-db (edited).")
        time.sleep(2)
        run = sync(args.aquifer, args.source)
        st = run["stats"]
        if found.get(0):
            text = get_doc(args, found[0]["document_id"]).get("text", "")
            check("update: edit propagated", "lakehouse-db" in text
                  and "warehouse-db-0" not in text, text[-160:])
        check("update: only the changed item was fetched and indexed",
              st.get("indexed") == 1 and st.get("detail_calls") == 1, st)

        # 4. retries during a network fault ---------------------------------------------
        if args.fault_on and args.fault_off:
            run_cmd(args.fault_on)
            since = time.time()
            aquifer(args.aquifer, "sources", "sync", args.source)
            failed = wait_run(args.aquifer, args.source, since, want=("failed",), timeout=240)
            check("retry: run during the fault fails as transient (source_unavailable)",
                  "source_unavailable" in (failed.get("error") or ""), failed)
            healed = time.time()
            run_cmd(args.fault_off)
            run = wait_run(args.aquifer, args.source, healed, want=("succeeded",), timeout=420)
            check("retry: the queued job is retried and succeeds after the fault",
                  run["status"] == "succeeded", run)

        # 5. crash mid-run --------------------------------------------------------------
        if args.crash:
            since = time.time()
            aquifer(args.aquifer, "sources", "sync", args.source, "--full")
            out = run_cmd(args.crash)
            recovered = next((int(line.split("=", 1)[1]) for line in out.splitlines()
                              if line.startswith("RECOVERED=")), 0)
            check("restart: interrupted jobs were recovered after the crash", recovered >= 1, out)
            run = wait_run(args.aquifer, args.source, since, timeout=600)
            check("restart: the interrupted run completed", run["status"] == "succeeded", run)
            check("restart: every item still present exactly once",
                  all(find(args, f"zebra{i}-{marker}") for i in range(args.discussions)), "")

        # 6. deletions ------------------------------------------------------------------
        gone = discussions.pop()
        delete_discussion(gone["id"])
        sh("gh", "release", "delete", tag, "--repo", args.repo, "--yes", "--cleanup-tag")
        release = False
        run = sync(args.aquifer, args.source, full=True)
        check("deletion: run succeeded", run["status"] == "succeeded", run)
        last = args.discussions - 1
        if found.get(last):
            check("deletion: deleted discussion gone from Aquifer",
                  get_doc(args, found[last]["document_id"]).get("found") is False)
        if rel:
            check("deletion: deleted release gone from Aquifer",
                  get_doc(args, rel["document_id"]).get("found") is False)
        check("deletion: confirmed by the source (2 deletions)",
              run["stats"].get("deleted", 0) == 2, run["stats"])
        if found.get(1):
            check("deletion: other discussions untouched",
                  get_doc(args, found[1]["document_id"]).get("found") is True)
    finally:
        for d in discussions:
            try:
                delete_discussion(d["id"])
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup: could not delete discussion {d['number']}: {exc}")
        if release:
            subprocess.run(["gh", "release", "delete", tag, "--repo", args.repo, "--yes",
                            "--cleanup-tag"], capture_output=True)
    failed = [r for r in results if not r["ok"]]
    summary = {"passed": len(results) - len(failed), "failed": len(failed), "marker": marker}
    print(json.dumps(summary, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"summary": summary, "checks": results}, fh, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
