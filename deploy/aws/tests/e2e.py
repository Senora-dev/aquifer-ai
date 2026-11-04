#!/usr/bin/env python3
"""End-to-end checks against a deployed Aquifer, through a real MCP client.

Run by ``./aquifer-aws test`` (which opens the SSM port forward and passes the URL, token
and repositories). Prints one line per check and a JSON summary; exits non-zero on failure.

    python e2e.py --url http://127.0.0.1:18080/mcp --token ... \
        --repos owner/aquifer-test-payments owner/aquifer-test-platform \
                owner/aquifer-test-legacy-billing [--expect-llm]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
results: list[dict] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail if not ok else ""})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail and not ok else ""), flush=True)
    return bool(ok)


def raw_status(url: str, token: str | None) -> int:
    import httpx

    headers = {"accept": "application/json, text/event-stream"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    return httpx.post(url, json=body, headers=headers, timeout=20).status_code


def edges(out: dict) -> dict:
    return {(e["direction"], e["source"], e["type"], e["target"]): e for e in out["relationships"]}


async def run(args: argparse.Namespace) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    payments, platform, legacy = args.repos
    owner_platform = platform

    # --- authentication ------------------------------------------------------------------
    check("request without token is rejected (401)", raw_status(args.url, None) == 401)
    check("request with a wrong token is rejected (401)",
          raw_status(args.url, "x" * 48) == 401)

    headers = {"Authorization": f"Bearer {args.token}"}
    async with streamablehttp_client(args.url, headers=headers) as (r, w, _), \
            ClientSession(r, w) as s:
        await s.initialize()

        async def call(tool: str, **kw) -> object:
            started = time.perf_counter()
            res = await s.call_tool(tool, kw)
            elapsed = (time.perf_counter() - started) * 1000
            if res.isError:
                raise RuntimeError(f"{tool} error: {res.content[0].text}")
            payload = json.loads(res.content[0].text) if len(res.content) == 1 else [
                json.loads(c.text) for c in res.content]
            results.append({"latency_ms": round(elapsed, 1), "tool": tool})
            return payload

        tools = {t.name for t in (await s.list_tools()).tools}
        check("five MCP tools are exposed", tools == {"search_context", "get_document",
                                                       "list_sources", "find_related",
                                                       "list_entities"}, sorted(tools))

        # --- ingestion happened ----------------------------------------------------------
        sources = await call("list_sources")
        counts = {x["repo"]: x.get("indexed_documents", 0) for x in sources}
        check("every repository has indexed documents",
              all(counts.get(r, 0) > 0 for r in args.repos), counts)

        # --- explicit relationships with exact evidence ----------------------------------
        out = await call("find_related", entity="billing-api", direction="incoming")
        e = edges(out).get(("incoming", "orders-service", "depends_on", "billing-api"))
        ok = e is not None
        check("orders-service → billing-api found (incoming)", ok, list(edges(out)))
        if ok:
            ev = next((x for x in e["evidence"] if x.get("path") == "docker-compose.yml"), {})
            check("evidence: repo + file + line 6", (ev.get("repository"), ev.get("line_start"))
                  == (owner_platform, 6), ev)
            check("evidence: real commit SHA", bool(SHA_RE.match(ev.get("revision") or "")), ev)
            check("evidence: line-anchored permalink to that commit",
                  (ev.get("url") or "").startswith(
                      f"https://github.com/{owner_platform}/blob/{ev.get('revision')}/"
                      "docker-compose.yml#L6"), ev.get("url"))
            # Both descriptors state it explicitly; an LLM may additionally find it in prose.
            check("explicit origin, stated by both descriptors",
                  "explicit_reference" in e["origins"] and e["support"]["explicit"] == 2,
                  e["support"])

        out = await call("find_related", entity="billing-api", direction="outgoing")
        es = edges(out)
        check("billing-api → payments-db and redis (outgoing)",
              ("outgoing", "billing-api", "depends_on", "payments-db") in es
              and ("outgoing", "billing-api", "depends_on", "redis") in es, list(es))

        # --- LLM extraction (Bedrock) ----------------------------------------------------
        if args.expect_llm:
            out = await call("find_related", entity="billing-api", direction="incoming")
            llm = [x for x in out["relationships"] if x["source"] and "checkout" in x["source"]
                   and "llm_extracted" in x["origins"]]
            check("LLM: checkout-service → billing-api extracted from the issue", bool(llm),
                  [(x["source"], x["type"], x["origins"]) for x in out["relationships"]])
            if llm:
                ev = next(e for e in llm[0]["evidence"] if e["origin"] == "llm_extracted")
                check("LLM edge: claimed (not current), verified quote on line 2",
                      llm[0]["status"] == "claimed" and ev.get("quote_verified") is True
                      and ev.get("line_start") == 2, {k: ev.get(k) for k in (
                          "status", "quote_verified", "line_start", "assertion")})

            # A merged PR that *moved off* memcached must never make memcached current.
            out = await call("find_related", entity="memcached")
            mem = [x for x in out["relationships"] if x["source"] == "billing-api"]
            check("removed dependency (merged PR) is 'removed', never current",
                  bool(mem) and all(x["status"] == "removed" for x in mem),
                  [(x["status"], x["support"]) for x in mem])
            out = await call("find_related", entity="fraud-check")
            prop = [x for x in out["relationships"] if x["source"] == "billing-api"]
            check("open-PR proposal is 'proposed', never current",
                  bool(prop) and all(x["status"] == "proposed" for x in prop),
                  [(x["status"], x["support"]) for x in prop])

        # --- terraform module references ------------------------------------------------
        src = "git::https://github.com/acme/terraform-modules.git//sqs-queue?ref=v1.4.0"
        out = await call("find_related", entity=src, direction="incoming")
        got = {(x["source"], x["evidence"][0]["repository"], x["evidence"][0]["line_start"])
               for x in out["relationships"]}
        check("terraform: both stacks using sqs-queue v1.4.0, with lines",
              got == {("module.billing_queue", payments, 4),
                      ("module.orders_queue", platform, 8)}, got)

        # --- similar names / missing evidence / isolation -------------------------------
        out = await call("find_related", entity="billing-api-v2")
        targets = {x["target"] for x in out["relationships"]}
        check("billing-api-v2 is not confused with billing-api",
              "oracle-db" in targets and "payments-db" not in targets, targets)
        out = await call("find_related", entity="kafka")
        check("unknown entity → evidence_status none", out["evidence_status"] == "none"
              and out["relationships"] == [], out["evidence_status"])
        out = await call("search_context", query="billing-api depends on database",
                         filters={"repo": legacy}, k=10)
        check("repo filter returns only that repository", {x["repo"] for x in out} <= {legacy},
              {x["repo"] for x in out})
        out = await call("search_context", query="billing", filters={"repo": "someone/else"})
        check("out-of-scope repo returns nothing", out == [], len(out))

        # --- semantic search ------------------------------------------------------------
        out = await call("search_context",
                         query="why does ledger-service store balances in PostgreSQL", k=5)
        docs = []
        for x in out:
            if x["document_id"] not in docs:
                docs.append(x["document_id"])
        adr = f"github:{payments}:file:docs/adr/0007-ledger-on-postgres.md"
        if not args.after_mutation:  # the update test deletes this ADR
            check("search: the ADR ranks in the top 3", adr in docs[:3], docs[:3])
        if out:
            check("search results carry indexed_at and evidence",
                  all("indexed_at" in x and x["evidence"].get("repository") for x in out))

        # --- open the evidence ----------------------------------------------------------
        compose = f"github:{payments}:file:docker-compose.yml"
        doc = await call("get_document", document_id=compose, line_start=6, line_end=8)
        expected_line = "- valkey" if args.after_mutation else "- redis"
        check("get_document excerpt shows the exact lines",
              expected_line in ((doc.get("excerpt") or {}).get("text") or ""), doc.get("excerpt"))
        check("get_document has revision + indexed_at",
              bool(SHA_RE.match(doc.get("revision") or "")) and bool(doc.get("indexed_at")))

        # --- real PR/issue state --------------------------------------------------------
        merged = await call("get_document", document_id=f"github:{payments}:pr:2")
        proposal = await call("get_document", document_id=f"github:{payments}:pr:3")
        check("PR state: merged vs open proposal",
              (merged.get("state"), proposal.get("state")) == ("merged", "open"),
              (merged.get("state"), proposal.get("state")))
        issue = await call("get_document", document_id=f"github:{payments}:issue:1")
        check("issue has source updated_at", bool(issue.get("updated_at")), issue.get("found"))

        if args.after_mutation:
            out = await call("find_related", entity="billing-api", direction="outgoing")
            es = edges(out)
            valkey = es.get(("outgoing", "billing-api", "depends_on", "valkey"))
            check("update propagated: valkey edge from docker-compose.yml",
                  valkey is not None and any(ev.get("path") == "docker-compose.yml"
                                             for ev in valkey["evidence"]), list(es))
            # redis may still be stated elsewhere (e.g. a merged PR, via the LLM); what must be
            # gone is the evidence from the edited file.
            redis = es.get(("outgoing", "billing-api", "depends_on", "redis"))
            stale = [ev for ev in (redis or {}).get("evidence", [])
                     if ev.get("path") == "docker-compose.yml"]
            check("update propagated: no redis evidence left from docker-compose.yml",
                  not stale, stale)
            if redis:
                print(f"       note: redis still supported by {redis['support']} "
                      f"({sorted({ev.get('document_id') for ev in redis['evidence']})})")
            gone = await call("get_document", document_id=adr)
            check("deletion propagated: ADR no longer found", gone == {"document_id": adr,
                                                                     "found": False}, gone)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--repos", nargs=3, required=True,
                   help="payments, platform and legacy-billing repositories, in that order")
    p.add_argument("--expect-llm", action="store_true")
    p.add_argument("--after-mutation", action="store_true")
    p.add_argument("--json-out")
    args = p.parse_args()
    asyncio.run(run(args))
    checks = [r for r in results if "check" in r]
    latencies = [r["latency_ms"] for r in results if "latency_ms" in r]
    summary = {
        "passed": sum(c["ok"] for c in checks),
        "failed": sum(not c["ok"] for c in checks),
        "failures": [c for c in checks if not c["ok"]],
        "tool_latency_ms": {"p50": sorted(latencies)[len(latencies) // 2] if latencies else None,
                            "max": max(latencies) if latencies else None,
                            "calls": len(latencies)},
    }
    print(json.dumps(summary, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"summary": summary, "checks": checks}, f, indent=2)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
