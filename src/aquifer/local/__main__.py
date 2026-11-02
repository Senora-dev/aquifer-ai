"""``python -m aquifer.local`` — ingest local repositories and serve them over MCP.

    python -m aquifer.local ingest examples/demo/repos
    python -m aquifer.local serve --repos-root examples/demo/repos      # stdio (default)
    python -m aquifer.local serve --repos-root examples/demo/repos --transport http
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aquifer.local import DEFAULT_STORE, ingest, local_settings
from aquifer.local.connector import LocalRepoConnector


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m aquifer.local", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="index every repository directory under ROOT")
    p_ingest.add_argument("root", type=Path)
    p_ingest.add_argument("--store", default=DEFAULT_STORE)
    p_ingest.add_argument("--bedrock", action="store_true",
                          help="use Bedrock embeddings + LLM extraction (needs AWS credentials)")

    p_serve = sub.add_parser("serve", help="serve the local index over MCP")
    p_serve.add_argument("--store", default=DEFAULT_STORE)
    p_serve.add_argument("--repos-root", type=Path,
                         help="limit results to the repositories under this directory")
    p_serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p_serve.add_argument("--bedrock", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.command == "ingest":
        results = ingest(args.root, args.store, use_bedrock=args.bedrock)
        indexed = sum(r.indexed for r in results)
        skipped = sum(r.skipped for r in results)
        failed = sum(r.failed for r in results)
        deleted = sum(r.deleted for r in results)
        print(f"indexed={indexed} unchanged={skipped} failed={failed} deleted={deleted} "
              f"store={args.store}", file=sys.stderr)
        sys.exit(1 if failed else 0)

    repos = LocalRepoConnector.from_root(args.repos_root).repos if args.repos_root else None
    local_settings(args.store, repos, use_bedrock=args.bedrock)
    from aquifer.mcp import server

    server.main(["--transport", "stdio" if args.transport == "stdio" else "streamable-http"])


if __name__ == "__main__":
    main()
