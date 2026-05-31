#!/usr/bin/env python3
"""Semantic-indexing evaluation harness (CLI).

Runs the golden artifacts in ``tests/eval_data/`` through the real semantic indexer (the org's
own Bedrock), prints the selected prompt template and the generated neutral metadata JSON, and
scores recall against the expected objective fields. Use it to tune the prompts in
``aquifer/semantic/prompts.py`` until extraction is consistently accurate.

    python scripts/eval_semantic_index.py                 # all examples
    python scripts/eval_semantic_index.py --name jira     # only matching examples
    python scripts/eval_semantic_index.py --verbose       # also print the rendered prompt

Requires AWS credentials and Bedrock model access (it calls your account's Bedrock). It does not
write anything; it only reads the eval data and prints a report.
"""

from __future__ import annotations

import argparse
import json

from aquifer.ingestion.factory import build_semantic_indexer
from aquifer.semantic.eval import evaluate, load_examples
from aquifer.semantic.prompts import default_registry


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate semantic-indexing accuracy.")
    parser.add_argument("--data-dir", default="tests/eval_data")
    parser.add_argument("--name", default=None, help="only run examples whose name contains this")
    parser.add_argument("--verbose", action="store_true", help="also print the rendered prompt")
    parser.add_argument("--max-input-chars", type=int, default=12000)
    args = parser.parse_args(argv)

    examples = load_examples(args.data_dir)
    if args.name:
        examples = [e for e in examples if args.name in e.name]
    if not examples:
        print("No examples matched.")
        return

    registry = default_registry()
    indexer = build_semantic_indexer()
    results = evaluate(indexer, examples, registry)

    entity_recalls: list[float] = []
    relationship_recalls: list[float] = []

    for example, result in zip(examples, results, strict=True):
        print("=" * 80)
        print(f"{result.name}   [prompt: {result.template}]")
        if args.verbose:
            template = registry.select(example.document.source_type, example.document.kind)
            print("\n--- RENDERED PROMPT ---")
            print(template.render_user(example.document, args.max_input_chars))
        print("\n--- GENERATED METADATA (neutral) ---")
        print(json.dumps(result.metadata.model_dump(), indent=2))
        score = result.score
        print("\n--- SCORE (recall vs golden) ---")
        print(f"  entities:      {score['entity_recall']}   missing: {score['missing_entities']}")
        print(
            f"  relationships: {score['relationship_recall']}"
            f"   missing: {score['missing_relationship_targets']}"
        )
        if score["entity_recall"] is not None:
            entity_recalls.append(score["entity_recall"])
        if score["relationship_recall"] is not None:
            relationship_recalls.append(score["relationship_recall"])

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    print("=" * 80)
    print(
        f"MEAN entity recall: {_mean(entity_recalls)}   |   "
        f"MEAN relationship recall: {_mean(relationship_recalls)}"
    )


if __name__ == "__main__":
    main()
