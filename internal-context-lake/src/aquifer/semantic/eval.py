"""Reusable logic for the semantic-indexing evaluation harness.

Loads golden artifacts, runs them through any :class:`SemanticIndexer`, and scores the extracted
metadata against expected objective fields (entity names, relationship targets) via recall. Pure
and dependency-light so it is unit-testable; ``scripts/eval_semantic_index.py`` wires the real
Bedrock-backed indexer for interactive prompt tuning.

Because Aquifer is neutral, the eval measures *retrieval-metadata accuracy* (did we extract the
right objective facts?), never reasoning or verdict quality.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

from aquifer.core.interfaces import SemanticIndexer
from aquifer.core.models import Document, DocumentKind, SemanticMetadata, SourceType
from aquifer.semantic.prompts import PromptRegistry, default_registry

_DOCUMENT_FIELDS = (
    "source_type",
    "kind",
    "repo",
    "external_id",
    "title",
    "body",
    "url",
    "labels",
)


@dataclass
class EvalExample:
    name: str
    document: Document
    golden: dict


@dataclass
class EvalResult:
    name: str
    template: str
    metadata: SemanticMetadata
    score: dict


def build_document(data: dict) -> Document:
    """Build a Document from a golden-example dict, computing a deterministic id."""
    source_type = SourceType(data["source_type"])
    kind = DocumentKind(data["kind"])
    doc_id = Document.make_id(source_type, data.get("repo") or "", kind, data["external_id"])
    fields = {k: data[k] for k in _DOCUMENT_FIELDS if k in data}
    return Document(id=doc_id, **fields)


def load_examples(data_dir) -> list[EvalExample]:
    """Load every ``*.json`` golden example from ``data_dir`` (sorted by filename)."""
    path = pathlib.Path(data_dir)
    examples: list[EvalExample] = []
    for file in sorted(path.glob("*.json")):
        data = json.loads(file.read_text())
        examples.append(
            EvalExample(
                name=data.get("name", file.stem),
                document=build_document(data),
                golden=data.get("golden", {}),
            )
        )
    return examples


def _recall(found: set[str], expected: set[str]) -> float | None:
    """Fraction of expected items that were found; ``None`` when nothing is expected."""
    if not expected:
        return None
    return len(found & expected) / len(expected)


def score_metadata(metadata: SemanticMetadata, golden: dict) -> dict:
    """Score extracted metadata against the golden objective fields (case-insensitive recall)."""
    found_entities = {e.name.lower() for e in metadata.entities}
    expected_entities = {x.lower() for x in golden.get("entities", [])}
    found_targets = {r.target.lower() for r in metadata.relationships}
    expected_targets = {x.lower() for x in golden.get("relationship_targets", [])}
    return {
        "entity_recall": _recall(found_entities, expected_entities),
        "missing_entities": sorted(expected_entities - found_entities),
        "relationship_recall": _recall(found_targets, expected_targets),
        "missing_relationship_targets": sorted(expected_targets - found_targets),
        "found_entities": sorted(found_entities),
        "found_relationship_targets": sorted(found_targets),
    }


def evaluate(
    indexer: SemanticIndexer,
    examples: list[EvalExample],
    registry: PromptRegistry | None = None,
) -> list[EvalResult]:
    """Run each example through the indexer and score it; preserves input order."""
    registry = registry or default_registry()
    results: list[EvalResult] = []
    for example in examples:
        metadata = indexer.extract(example.document)
        template = registry.select(example.document.source_type, example.document.kind).name
        results.append(
            EvalResult(
                name=example.name,
                template=template,
                metadata=metadata,
                score=score_metadata(metadata, example.golden),
            )
        )
    return results
