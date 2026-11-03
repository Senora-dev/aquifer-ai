#!/usr/bin/env python3
"""Run Aquifer's retrieval benchmark and write a machine-readable JSON report.

    python scripts/benchmark_retrieval.py                       # offline, deterministic (CI)
    python scripts/benchmark_retrieval.py --out report.json
    python scripts/benchmark_retrieval.py --bedrock             # real Bedrock (costs money)

Offline mode needs no credentials or network: in-memory index, lexical hashing embedder,
deterministic extractors. Cases that require LLM extraction are reported as skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aquifer.benchmark import run_benchmark

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=ROOT / "examples/demo/repos")
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/benchmark/cases.json")
    parser.add_argument("--golden", type=Path,
                        default=ROOT / "tests/benchmark/extraction_golden.json")
    parser.add_argument("--out", type=Path, help="write the full JSON report here")
    parser.add_argument("--bedrock", action="store_true",
                        help="use Bedrock embeddings + LLM extraction (needs AWS credentials)")
    parser.add_argument("--price-embed-per-1k", type=float,
                        help="USD per 1K embedding tokens (check current Bedrock pricing)")
    parser.add_argument("--price-llm-input-per-1k", type=float,
                        help="USD per 1K LLM input tokens (check current Bedrock pricing)")
    args = parser.parse_args(argv)

    prices = None
    if args.price_embed_per_1k is not None or args.price_llm_input_per_1k is not None:
        prices = {"embed_per_1k": args.price_embed_per_1k or 0.0,
                  "llm_input_per_1k": args.price_llm_input_per_1k or 0.0}
    report = run_benchmark(args.corpus, args.cases, mode="bedrock" if args.bedrock else "offline",
                           golden_path=args.golden, prices=prices)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, default=str))

    summary = {k: report[k] for k in ("mode", "cases", "metrics", "latency_ms", "by_scenario")}
    summary["extraction"] = {k: v for k, v in (report["extraction"] or {}).items()
                             if k != "per_file"}
    summary["cost"] = report["cost"]
    print(json.dumps(summary, indent=2, default=str))
    for r in report["results"]:
        if r["status"] == "failed":
            bad = [c for c in r["checks"] if not c["ok"]]
            print(f"FAILED {r['id']}: {bad}", file=sys.stderr)
    return 1 if report["cases"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
