"""``aquifer`` — run and operate a single-server Aquifer.

    aquifer serve                 # MCP server + scheduler + ingestion workers (one process)
    aquifer init                  # create the MCP access token if none exists (idempotent)
    aquifer sync [--full]         # run discovery now instead of waiting for the schedule
    aquifer status [--json]       # queue, last sync, index size, extraction backlog, errors
    aquifer retry-failed          # re-run extraction for documents that exhausted retries
    aquifer dead-letters [--requeue ID|all]

Configuration comes from ``AQUIFER_*`` environment variables. If ``/etc/aquifer/aquifer.env``
(or the file named by ``AQUIFER_ENV_FILE``) exists, it is loaded first, so commands run over SSM
see the same configuration as the service.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path

DEFAULT_ENV_FILE = "/etc/aquifer/aquifer.env"


def load_env_file(path: str | None = None) -> None:
    """Load KEY=VALUE lines without overriding variables already set."""
    path = path or os.environ.get("AQUIFER_ENV_FILE") or DEFAULT_ENV_FILE
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _settings():  # noqa: ANN202
    from aquifer.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


# --- serve --------------------------------------------------------------------------------

def _load_mcp_tokens(settings) -> None:  # noqa: ANN001
    ref = settings.mcp.auth_tokens_ref
    if ref and not settings.mcp.auth_tokens:
        from aquifer.core.secrets import resolve_secret

        settings.mcp.auth_tokens = json.loads(resolve_secret(ref))


def health_app(app, service=None):  # noqa: ANN001, ANN201
    """Answer ``GET /healthz`` without authentication (no data, just liveness)."""

    async def wrapped(scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            ok = service is None or all(t.is_alive() for t in service._threads)
            body = json.dumps({"status": "ok" if ok else "degraded"}).encode()
            await send({"type": "http.response.start", "status": 200 if ok else 503,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    return wrapped


def cmd_serve(args) -> int:  # noqa: ANN001
    from aquifer.core.errors import configure_lambda_logging
    from aquifer.mcp.auth import AuthConfigError
    from aquifer.mcp.server import build_tools, create_http_app
    from aquifer.node.service import NodeService

    settings = _settings()
    logging.basicConfig(level=settings.log_level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    configure_lambda_logging(settings.log_level)
    _load_mcp_tokens(settings)
    tools = build_tools(settings)
    tools.vector_store.ensure_index()
    try:
        app = create_http_app(settings, tools=tools)
    except AuthConfigError as exc:
        logging.getLogger("aquifer").error("refusing to start: %s", exc)
        return 2
    service = NodeService(settings)
    if not args.no_workers:
        service.start()
    import uvicorn

    logging.getLogger("aquifer").info("serving MCP on %s:%d/mcp", settings.mcp.host,
                                      settings.mcp.port)
    try:
        uvicorn.run(health_app(app, service if not args.no_workers else None),
                    host=settings.mcp.host, port=settings.mcp.port, log_level="warning")
    finally:
        service.stop()
    return 0


# --- operations ---------------------------------------------------------------------------

def cmd_init(args) -> int:  # noqa: ANN001
    """Create the MCP token map at ``mcp.auth_tokens_ref`` if it does not exist."""
    from aquifer.core.secrets import SecretNotFoundError, resolve_secret, store_secret

    settings = _settings()
    ref = settings.mcp.auth_tokens_ref
    if not ref:
        print("AQUIFER_MCP__AUTH_TOKENS_REF is not set; nothing to initialize", file=sys.stderr)
        return 0
    try:
        resolve_secret(ref)
        print("MCP token already exists", file=sys.stderr)
        return 0
    except SecretNotFoundError:
        pass
    store_secret(ref, json.dumps({"default": secrets.token_urlsafe(36)}))
    print(f"created MCP token at {ref.split(':', 1)[0]}:… (read it with the deploy script)",
          file=sys.stderr)
    return 0


def cmd_sync(args) -> int:  # noqa: ANN001
    from aquifer.node.service import open_queue

    queue = open_queue(_settings())
    added = queue.put("discover", {"full": bool(args.full)}, dedupe_key="discover")
    print("sync queued" if added else "a sync is already queued or running", file=sys.stderr)
    return 0


def cmd_retry_failed(args) -> int:  # noqa: ANN001
    from aquifer.node.service import open_queue

    open_queue(_settings()).put("retry_failed", {}, dedupe_key="retry_failed")
    print("retry of failed extraction queued", file=sys.stderr)
    return 0


def cmd_dead_letters(args) -> int:  # noqa: ANN001
    from aquifer.node.service import open_queue

    queue = open_queue(_settings())
    if args.requeue:
        n = queue.requeue_dead(None if args.requeue == "all" else int(args.requeue))
        print(f"requeued {n} job(s)", file=sys.stderr)
        return 0
    print(json.dumps(queue.dead_letters(), indent=2))
    return 0


def collect_status(settings) -> dict:  # noqa: ANN001
    from aquifer.ingestion.factory import build_vector_store
    from aquifer.node.service import open_queue

    queue = open_queue(settings)
    store = build_vector_store(settings)
    out: dict = {
        "queue": queue.counts(),
        "dead_letters": queue.dead_letters(limit=10),
        "sync": {
            "interval_s": settings.node.sync_interval_s,
            "last_started": queue.get_state("discovery:last_started"),
            "last_success": queue.get_state("discovery:last_success"),
            "last_error": queue.get_state("discovery:last_error"),
        },
        "index": store.stats() if hasattr(store, "stats") else {},
        "extraction": {
            "by_status": dict(store.facet("extraction_status", None, size=20) or []),
            "by_error": dict(store.facet("extraction_error", None, size=20) or []),
        },
        "repositories": dict(store.facet("repo", None, size=1000) or []),
    }
    return out


def cmd_status(args) -> int:  # noqa: ANN001
    status = collect_status(_settings())
    if args.json:
        print(json.dumps(status, indent=2, default=str))
        return 0
    q = status["queue"]
    print(f"queue      pending={sum(q.get('pending', {}).values())} "
          f"running={sum(q.get('running', {}).values())} "
          f"dead_letters={sum(q.get('dead', {}).values())}")
    sync = status["sync"]
    last = sync["last_success"] or {}
    print(f"sync       every {sync['interval_s']}s; last success {last.get('at', 'never')}"
          + (f"; LAST ERROR {sync['last_error']['error']}" if sync["last_error"] else ""))
    idx = status["index"]
    print(f"index      {idx.get('documents', 0)} documents, {idx.get('chunks', 0)} chunks, "
          f"{idx.get('bytes_on_disk', 0) // 1024} KiB on disk")
    print(f"repos      {len(status['repositories'])}: "
          + ", ".join(f"{k} ({v})" for k, v in status["repositories"].items()))
    ext = status["extraction"]
    print(f"extraction {ext['by_status']}" + (f" errors {ext['by_error']}"
                                              if ext["by_error"] else ""))
    for d in status["dead_letters"]:
        print(f"dead       #{d['id']} {d['summary']} after {d['attempts']} attempts: "
              f"{d['error']}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aquifer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help=f"configuration file (default {DEFAULT_ENV_FILE})")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="run the MCP server, scheduler and workers")
    p.add_argument("--no-workers", action="store_true", help="serve queries only")
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("init", help="create the MCP token if missing").set_defaults(fn=cmd_init)
    p = sub.add_parser("sync", help="run discovery now")
    p.add_argument("--full", action="store_true", help="ignore the incremental watermark")
    p.set_defaults(fn=cmd_sync)
    sub.add_parser("retry-failed", help="retry failed extraction").set_defaults(
        fn=cmd_retry_failed)
    p = sub.add_parser("status", help="show ingestion and index status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("dead-letters", help="list or requeue jobs that exhausted retries")
    p.add_argument("--requeue", metavar="ID|all")
    p.set_defaults(fn=cmd_dead_letters)
    args = parser.parse_args(argv)
    load_env_file(args.env_file)
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
