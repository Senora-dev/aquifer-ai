"""`aquifer sources …` — connect external MCP servers and import their content.

Onboarding, in order:

    aquifer sources providers                          # 1. known tools (or any URL)
    aquifer sources add eng-github --provider github --token-stdin < token
    aquifer sources authorize eng-github               # 2. OAuth in the browser / token check
    aquifer sources template eng-github --stream discussions --target acme/payments
    aquifer sources propose NAME --stream tickets --tool list_issues --args '{...}'   # any server
    aquifer sources preview eng-github                 # 3-4. sample documents + coverage
    aquifer sources enable eng-github                  # 5. first import now, then daily
    aquifer sources status                             # 6. health, progress, errors

Credentials are never accepted as command-line arguments (they would land in shell history and
process lists): use --token-stdin, --token-file, --token-env or an existing --token-ref.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from aquifer.core.models import DocumentKind


def _settings():  # noqa: ANN202
    from aquifer.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


def _print(obj) -> None:  # noqa: ANN001
    print(json.dumps(obj, indent=2, default=str))


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _connect(source, settings, **kw):  # noqa: ANN001, ANN003, ANN202
    from aquifer.integrator.client import connect
    from aquifer.integrator.jobs import redirect_uri

    return connect(source, redirect_uri=redirect_uri(settings),
                   allow_private=settings.integrator.allow_private_networks, **kw)


# --- providers / add ----------------------------------------------------------------------

def cmd_providers(args) -> int:  # noqa: ANN001
    from aquifer.integrator.providers import PROVIDERS

    for p in PROVIDERS.values():
        print(f"{p.id:10} {p.name}\n           url: {p.url}  auth: {p.auth}"
              + (f"  templates: {', '.join(p.templates)} (per {p.template_target})"
                 if p.templates else "")
              + f"\n           verified: {p.verified}"
              + (f"\n           note: {p.notes}" if p.notes else "") + "\n")
    print("Any other MCP server: aquifer sources add NAME --url https://… --auth none|bearer|oauth")
    return 0


def _read_token(args) -> str | None:  # noqa: ANN001
    if args.token_stdin:
        return sys.stdin.read().strip()
    if args.token_file:
        with open(args.token_file) as fh:
            return fh.read().strip()
    if args.token_env:
        value = os.environ.get(args.token_env)
        if not value:
            raise ValueError(f"environment variable {args.token_env} is empty")
        return value.strip()
    return None


def cmd_add(args) -> int:  # noqa: ANN001
    from aquifer.core.secrets import store_secret
    from aquifer.integrator.client import check_destination
    from aquifer.integrator.jobs import credentials_ref, registry_for
    from aquifer.integrator.models import AuthConfig, Source
    from aquifer.integrator.providers import get_provider

    settings = _settings()
    reg = registry_for(settings)
    if reg.get(args.name) and not args.replace:
        return _err(f"source {args.name!r} exists (use --replace, or `aquifer sources remove`)")
    provider = get_provider(args.provider) if args.provider else None
    url = args.url or (provider.url if provider else "")
    if not url:
        return _err("pass --provider or --url")
    auth_type = args.auth or (provider.auth if provider else "none")
    headers = dict(provider.headers) if provider else {}
    for h in args.header or []:
        k, _, v = h.partition("=")
        if k.lower() == "authorization":
            return _err("credentials are not accepted as headers; use --token-*")
        headers[k.strip()] = v.strip()
    try:
        check_destination(url, allow_private=settings.integrator.allow_private_networks)
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    auth = AuthConfig(type=auth_type, scope=args.scope or (provider.scope if provider else ""))
    if auth_type == "bearer":
        token = _read_token(args)
        if args.token_ref:
            auth.secret_ref = args.token_ref
        elif token:
            auth.secret_ref = credentials_ref(settings, args.name, "token")
            store_secret(auth.secret_ref, token)
        else:
            return _err("bearer auth needs --token-stdin, --token-file, --token-env or "
                        "--token-ref")
    elif auth_type == "oauth":
        auth.secret_ref = credentials_ref(settings, args.name, "oauth")
        if args.client_id:
            # Pre-registered client (providers without dynamic client registration).
            from aquifer.integrator.credentials import SecretTokenStorage
            from aquifer.integrator.jobs import redirect_uri

            info = {"client_id": args.client_id, "redirect_uris": [redirect_uri(settings)],
                    "token_endpoint_auth_method": "none"}
            secret = _read_token(args)
            if secret:
                info.update(client_secret=secret,
                            token_endpoint_auth_method="client_secret_post")
            SecretTokenStorage(auth.secret_ref).save({"client_info": info})
        elif provider and provider.requires_client_registration:
            return _err(f"{provider.name} does not support dynamic client registration: pass "
                        "--client-id (and the client secret with --token-stdin)")
    source = Source(name=args.name, provider=provider.id if provider else "custom", url=url,
                    auth=auth, headers=headers,
                    schedule_hours=settings.integrator.default_schedule_hours,
                    allow_unannotated=args.allow_unannotated,
                    connected_by=args.connected_by or os.environ.get("USER", ""),
                    state="needs_authorization" if auth_type == "oauth" else "ready")
    reg.save(source)
    print(f"added {source.name} ({source.url}, auth {auth_type})", file=sys.stderr)
    nxt = ("aquifer sources authorize " + source.name) if auth_type == "oauth" else \
        (f"aquifer sources tools {source.name}")
    print(f"next: {nxt}", file=sys.stderr)
    return 0


# --- authorize / tools --------------------------------------------------------------------

def cmd_authorize(args) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for
    from aquifer.node.service import open_queue

    settings = _settings()
    reg = registry_for(settings)
    source = reg.require(args.name)
    if source.auth.type != "oauth":
        # Nothing to authorize interactively: verify the credentials work.
        return cmd_tools(args, quiet=True)
    request_id = reg.oauth_begin(source.name)
    open_queue(settings).put("mcp_authorize", {"source": source.name,
                                               "request_id": request_id})
    if args.no_wait:
        # For remote operation (aquifer-aws): poll with `aquifer sources oauth-status ID`.
        print(f"REQUEST_ID={request_id}")
        return 0
    print("waiting for the Aquifer server to start the authorization…", file=sys.stderr)
    shown = False
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        row = reg.oauth_get(request_id) or {}
        if row.get("auth_url") and not shown:
            print("\nOpen this URL in a browser on the machine that reaches "
                  f"{settings.integrator.public_url} (e.g. through `aquifer-aws connect`):\n\n"
                  f"  {row['auth_url']}\n", file=sys.stderr)
            print(f"AUTH_URL={row['auth_url']}")
            shown = True
        if row.get("status") == "succeeded":
            print(f"authorized {source.name}; next: aquifer sources tools {source.name}",
                  file=sys.stderr)
            return 0
        if row.get("status") == "failed":
            return _err(f"authorization failed: {row.get('error')}")
        time.sleep(1)
    if not shown:
        return _err("the server did not pick up the request; is `aquifer serve` running?")
    return _err("timed out waiting for the browser authorization")


def cmd_oauth_status(args) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for

    row = registry_for(_settings()).oauth_get(args.request_id)
    if row is None:
        return _err("unknown request id")
    _print({k: row[k] for k in ("status", "auth_url", "error", "source")})
    return 0


def cmd_tools(args, quiet: bool = False) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for
    from aquifer.integrator.plan import describe_tools

    settings = _settings()
    reg = registry_for(settings)
    source = reg.require(args.name)

    async def run():  # noqa: ANN202
        async with _connect(source, settings) as conn:
            return conn.server_info, await conn.list_tools()

    try:
        info, tools = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        from aquifer.integrator.client import classify_failure

        err = classify_failure(exc)
        return _err(f"{err.code}: {err}")
    if source.state in {"needs_authorization", "needs_reauth"}:
        source.state = "active" if source.enabled else "ready"
        reg.save(source)
    described = describe_tools(tools)
    if getattr(args, "json", False):
        _print({"server": info, "tools": described})
        return 0
    print(f"connected: {info.get('name')} {info.get('version')} (protocol "
          f"{info.get('protocol')}), {len(tools)} tools", file=sys.stderr)
    if quiet:
        return 0
    for t in described:
        print(f"  [{t['class']:16}] {t['name']}({', '.join(t['params'])})\n"
              f"                     {t['description']}")
    print("\nOnly read_only tools (and unannotated_read with --allow-unannotated) can be used. "
          "Descriptions come from the remote server and are shown as data.", file=sys.stderr)
    return 0


# --- plan ---------------------------------------------------------------------------------

def _approve(source, stream) -> None:  # noqa: ANN001
    from aquifer.integrator.actions import approve

    approve(source, stream)


def _add_stream(source, stream, tools) -> list[str]:  # noqa: ANN001
    from aquifer.integrator.actions import add_stream

    return add_stream(source, stream, tools)


def cmd_template(args) -> int:  # noqa: ANN001
    from aquifer.integrator.actions import add_template_streams
    from aquifer.integrator.jobs import registry_for

    settings = _settings()
    source = registry_for(settings).require(args.name)
    try:
        added = add_template_streams(settings, source, args.stream, args.target,
                                     page_size=args.page_size)
    except ValueError as exc:
        return _err(str(exc))
    for stream in added:
        print(f"added stream {stream.name}: {stream.coverage()['changes']}", file=sys.stderr)
    print(f"next: aquifer sources preview {source.name}", file=sys.stderr)
    return 0


def cmd_propose(args) -> int:  # noqa: ANN001
    """Sample one call of a list tool and derive a stream (deterministic heuristics)."""
    from aquifer.integrator.jobs import registry_for
    from aquifer.integrator.plan import approvable, classify_tool, infer_stream

    settings = _settings()
    reg = registry_for(settings)
    source = reg.require(args.name)
    call_args = json.loads(args.args or "{}")

    async def sample():  # noqa: ANN202
        async with _connect(source, settings) as conn:
            tools = await conn.list_tools()
            tool = next((t for t in tools if t.name == args.tool), None)
            if tool is None:
                raise ValueError(f"the server has no tool {args.tool!r}")
            if not approvable(tool, allow_unannotated=source.allow_unannotated):
                raise ValueError(f"{args.tool} is classified {classify_tool(tool)!r}; only "
                                 "read-only tools can be used")
            source.approved_tools = sorted(set(source.approved_tools) | {tool.name})
            conn.source = source
            result = await conn.call(tool.name, call_args)
            return tools, tool, result.data

    try:
        tools, tool, data = asyncio.run(sample())
        stream = infer_stream(args.stream, tool, call_args, data, kind=DocumentKind(args.kind))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    stream.enumeration = "complete" if args.complete else "partial"
    if args.detail_tool:
        from aquifer.integrator.models import DetailSpec

        stream.detail = DetailSpec(tool=args.detail_tool, args=json.loads(args.detail_args
                                                                          or "{}"),
                                   body=args.detail_body or "")
    problems = _add_stream(source, stream, tools)
    if problems:
        return _err("; ".join(problems))
    reg.save(source)
    _print(stream.model_dump(mode="json"))
    print(f"\nadded stream {stream.name}. Check the field mapping above; edit with "
          f"`aquifer sources plan {source.name} --export plan.json` / `--import plan.json`. "
          f"next: aquifer sources preview {source.name}", file=sys.stderr)
    return 0


def cmd_plan(args) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for
    from aquifer.integrator.models import SyncPlan
    from aquifer.integrator.plan import validate_plan

    settings = _settings()
    reg = registry_for(settings)
    source = reg.require(args.name)
    if args.import_file:
        with open(args.import_file) as fh:
            plan = SyncPlan.model_validate_json(fh.read())

        async def tools():  # noqa: ANN202
            async with _connect(source, settings) as conn:
                return await conn.list_tools()

        source.plan = plan
        source.approved_tools = []
        for stream in plan.streams:
            _approve(source, stream)
        problems = validate_plan(source, asyncio.run(tools()),
                                 allow_unannotated=source.allow_unannotated)
        if problems:
            return _err("; ".join(problems))
        reg.save(source)
        print(f"plan updated ({len(plan.streams)} streams); takes effect on the next run",
              file=sys.stderr)
        return 0
    data = source.plan.model_dump(mode="json")
    if args.export_file:
        with open(args.export_file, "w") as fh:
            json.dump(data, fh, indent=2)
        print(f"wrote {args.export_file}", file=sys.stderr)
        return 0
    _print({"approved_tools": source.approved_tools, **data,
            "coverage": [s.coverage() for s in source.plan.streams]})
    return 0


# --- preview / enable / run ---------------------------------------------------------------

def cmd_preview(args) -> int:  # noqa: ANN001
    from aquifer.integrator.actions import preview
    from aquifer.integrator.jobs import registry_for

    settings = _settings()
    source = registry_for(settings).require(args.name)
    try:
        result = preview(settings, source, limit=args.limit)
    except ValueError as exc:
        return _err(str(exc))
    _print(result)
    if any("error" in s for s in result["streams"]):
        return 1
    print(f"\nnext: aquifer sources enable {source.name}", file=sys.stderr)
    return 0


def cmd_enable(args) -> int:  # noqa: ANN001
    from aquifer.integrator.actions import enable
    from aquifer.integrator.jobs import registry_for

    settings = _settings()
    source = registry_for(settings).require(args.name)
    try:
        enable(settings, source, every_hours=args.every_hours)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        from aquifer.integrator.client import classify_failure

        err = classify_failure(exc)
        return _err(f"{err.code}: {err}")
    print(f"enabled {source.name}: first import queued, then every {source.schedule_hours} h. "
          f"Follow it with: aquifer sources status {source.name}", file=sys.stderr)
    return 0


def cmd_sync(args) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for
    from aquifer.node.service import open_queue

    settings = _settings()
    source = registry_for(settings).require(args.name)
    if not source.enabled:
        return _err(f"{source.name} is not enabled: aquifer sources enable {source.name}")
    added = open_queue(settings).put("mcp_sync", {"source": source.name, "full": args.full},
                                     dedupe_key=f"mcp_sync:{source.name}")
    print("sync queued" if added else "a sync of this source is already queued",
          file=sys.stderr)
    print(f"SYNC_QUEUED={int(added)}")
    return 0


def cmd_pause(args) -> int:  # noqa: ANN001
    from aquifer.integrator.jobs import registry_for

    reg = registry_for(_settings())
    source = reg.require(args.name)
    source.enabled = False
    source.state = "paused"
    reg.save(source)
    print(f"paused {source.name}: no further syncs; its documents are no longer readable "
          "through MCP (they stay indexed until `aquifer sources remove`)", file=sys.stderr)
    return 0


def cmd_remove(args) -> int:  # noqa: ANN001
    from aquifer.core.secrets import delete_secret
    from aquifer.ingestion.factory import build_vector_store
    from aquifer.integrator.jobs import registry_for

    settings = _settings()
    reg = registry_for(settings)
    source = reg.require(args.name)
    removed = 0
    if not args.keep_data:
        store = build_vector_store(settings)
        if hasattr(store, "delete_by_filter"):
            removed = store.delete_by_filter({"repo": source.scope})
    for ref in (source.auth.secret_ref, source.auth.client_secret_ref):
        if ref and not ref.startswith("env:"):
            delete_secret(ref)
    reg.delete(source.name)
    print(f"removed {source.name}: credentials deleted, {removed} indexed chunks deleted"
          + (" (data kept)" if args.keep_data else ""), file=sys.stderr)
    return 0


def cmd_status(args) -> int:  # noqa: ANN001
    from aquifer.integrator.status import sources_status
    from aquifer.node.service import open_queue

    settings = _settings()
    status = sources_status(settings, open_queue(settings))
    if args.name:
        status = [s for s in status if s["name"] == args.name]
    if args.json or args.name:
        _print(status)
        return 0
    if not status:
        print("no MCP sources. Start with: aquifer sources providers")
    for s in status:
        last = s.get("last_run") or {}
        print(f"{s['name']:20} {s['state']:20} last success {s.get('last_success') or 'never'}"
              f"; last run {last.get('status', '-')}"
              + (f" ({last['stats'].get('indexed', 0)} indexed, "
                 f"{last['stats'].get('unchanged', 0)} unchanged, "
                 f"{last['stats'].get('deleted', 0)} deleted)" if last else ""))
        if s.get("action"):
            print(f"{'':20} → {s['action']}")
    return 0


def add_commands(sub) -> None:  # noqa: ANN001
    p = sub.add_parser("sources", help="connect external MCP servers (MCP Integrator)")
    ss = p.add_subparsers(dest="sources_command", required=True)
    ss.add_parser("providers", help="known providers").set_defaults(fn=cmd_providers)

    a = ss.add_parser("add", help="add a source")
    a.add_argument("name")
    a.add_argument("--provider")
    a.add_argument("--url")
    a.add_argument("--auth", choices=["none", "bearer", "oauth"])
    a.add_argument("--token-stdin", action="store_true")
    a.add_argument("--token-file")
    a.add_argument("--token-env")
    a.add_argument("--token-ref", help="an existing secret reference (ssm:/…, file:/…)")
    a.add_argument("--header", action="append", help="extra non-secret header K=V")
    a.add_argument("--scope", help="OAuth scopes to request")
    a.add_argument("--client-id", help="pre-registered OAuth client (no dynamic registration)")
    a.add_argument("--allow-unannotated", action="store_true",
                   help="also accept read-named tools without MCP annotations")
    a.add_argument("--connected-by")
    a.add_argument("--replace", action="store_true")
    a.set_defaults(fn=cmd_add)

    au = ss.add_parser("authorize", help="OAuth in the browser, or check a token")
    au.add_argument("name")
    au.add_argument("--timeout", type=int, default=960)
    au.add_argument("--no-wait", action="store_true",
                    help="queue the authorization and print its id (remote operation)")
    au.set_defaults(fn=cmd_authorize)

    os_ = ss.add_parser("oauth-status", help="state of a pending authorization")
    os_.add_argument("request_id")
    os_.set_defaults(fn=cmd_oauth_status)

    t = ss.add_parser("tools", help="list the server's tools and their safety class")
    t.add_argument("name")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_tools)

    tp = ss.add_parser("template", help="add a provider's preconfigured stream")
    tp.add_argument("name")
    tp.add_argument("--stream", required=True)
    tp.add_argument("--target", action="append", required=True,
                    help="what to import, e.g. owner/repo (repeatable)")
    tp.add_argument("--page-size", type=int, help="items per page (default from the template)")
    tp.set_defaults(fn=cmd_template)

    pr = ss.add_parser("propose", help="derive a stream from one sample call of a list tool")
    pr.add_argument("name")
    pr.add_argument("--stream", required=True)
    pr.add_argument("--tool", required=True)
    pr.add_argument("--args", help="JSON arguments for the list tool")
    pr.add_argument("--kind", default="record", choices=[k.value for k in DocumentKind])
    pr.add_argument("--complete", action="store_true",
                    help="the tool lists every item of its container (enables deletions)")
    pr.add_argument("--detail-tool")
    pr.add_argument("--detail-args", help='JSON; "{id}" is replaced with the item id')
    pr.add_argument("--detail-body", help="path of the body in the detail result")
    pr.set_defaults(fn=cmd_propose)

    pl = ss.add_parser("plan", help="show, export or import a source's plan")
    pl.add_argument("name")
    pl.add_argument("--export", dest="export_file")
    pl.add_argument("--import", dest="import_file")
    pl.set_defaults(fn=cmd_plan)

    pv = ss.add_parser("preview", help="sample documents and coverage (indexes nothing)")
    pv.add_argument("name")
    pv.add_argument("--limit", type=int, default=3)
    pv.set_defaults(fn=cmd_preview)

    en = ss.add_parser("enable", help="start the first import and the schedule")
    en.add_argument("name")
    en.add_argument("--every-hours", type=int)
    en.set_defaults(fn=cmd_enable)

    sy = ss.add_parser("sync", help="run a source now")
    sy.add_argument("name")
    sy.add_argument("--full", action="store_true")
    sy.set_defaults(fn=cmd_sync)

    pa = ss.add_parser("pause", help="stop syncing and hide the source's documents")
    pa.add_argument("name")
    pa.set_defaults(fn=cmd_pause)

    rm = ss.add_parser("remove", help="delete the source, its credentials and its documents")
    rm.add_argument("name")
    rm.add_argument("--keep-data", action="store_true")
    rm.set_defaults(fn=cmd_remove)

    st = ss.add_parser("status", help="health, progress, last success, errors")
    st.add_argument("name", nargs="?")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)
