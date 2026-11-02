"""Deterministic (non-LLM) extraction for well-structured files, plus LLM grounding.

``extract_structured`` dispatches on the file path to a parser for Terraform or a supported YAML
descriptor. Results are marked ``origin = explicit_reference`` and carry parser-reported line
numbers. Documents no parser recognizes return ``None`` and are left to the LLM extractor.

``ground_llm_metadata`` checks each LLM-extracted relationship against the document text: when
the target name occurs in the body, the relationship is marked ``grounded`` and gets the line of
that occurrence as evidence (``locator = "text_match"`` — the location of the *mention*, not a
verified statement of the dependency). When it does not occur, ``grounded = False`` and no
evidence is attached. LLM output is never upgraded to ``explicit_reference``.
"""

from __future__ import annotations

import re

from aquifer.core.entities import normalize_name
from aquifer.core.models import (
    Document,
    RelationshipOrigin,
    SemanticMetadata,
)
from aquifer.semantic.structured.common import EvidenceFactory
from aquifer.semantic.structured.terraform import extract_terraform
from aquifer.semantic.structured.yaml_descriptors import extract_yaml

_LLM = RelationshipOrigin.LLM_EXTRACTED.value

__all__ = ["extract_structured", "ground_llm_metadata", "merge_metadata"]


def extract_structured(document: Document) -> SemanticMetadata | None:
    """Run the deterministic parser matching ``document.path``; ``None`` if none applies."""
    path = (document.path or "").lower()
    if not path or not document.body:
        return None
    ev = EvidenceFactory(document)
    if path.endswith(".tf"):
        return extract_terraform(document.body, ev)
    if path.endswith((".yml", ".yaml")):
        return extract_yaml(document.body, ev)
    return None


def _find_line(ev: EvidenceFactory, needle: str, also: str | None) -> int | None:
    """First body line mentioning ``needle`` (preferring one that also mentions ``also``)."""
    if not needle.strip():
        return None
    pattern = re.compile(r"(?<![\w-])" + re.escape(needle.strip()) + r"(?![\w-])", re.I)
    first: int | None = None
    for m in pattern.finditer(ev.body):
        line = ev.line_of(m.start())
        if first is None:
            first = line
        if also is None:
            break
        if also.lower() in ev.line_text(line).lower():
            return line
    return first


def ground_llm_metadata(metadata: SemanticMetadata, document: Document) -> SemanticMetadata:
    """Mark LLM relationships as grounded/ungrounded against the text (mutates and returns)."""
    ev = EvidenceFactory(document)
    title = (document.title or "").lower()
    for rel in metadata.relationships:
        rel.origin = _LLM
        line = _find_line(ev, rel.target, rel.source)
        if line is not None:
            rel.grounded = True
            rel.evidence = [ev.lines(line, line, locator="text_match")]
        elif rel.target and normalize_name(rel.target) in title:
            rel.grounded = True
            rel.evidence = []
        else:
            rel.grounded = False
            rel.evidence = []
    for entity in metadata.entities:
        entity.origin = _LLM
    return metadata


def merge_metadata(
    structured: SemanticMetadata | None, llm: SemanticMetadata | None
) -> SemanticMetadata | None:
    """Union of both; where both found the same edge or entity, the explicit one wins."""
    if structured is None:
        return llm
    if llm is None:
        return structured
    entities = {(e.type, normalize_name(e.name), e.namespace): e for e in structured.entities}
    for e in llm.entities:
        entities.setdefault((e.type, normalize_name(e.name), e.namespace), e)
    rels = {
        (normalize_name(r.source or ""), r.type, normalize_name(r.target)): r
        for r in structured.relationships
    }
    for r in llm.relationships:
        rels.setdefault((normalize_name(r.source or ""), r.type, normalize_name(r.target)), r)
    return SemanticMetadata(
        summary=llm.summary or structured.summary,
        entities=list(entities.values()),
        topics=list(dict.fromkeys([*structured.topics, *llm.topics])),
        relationships=list(rels.values()),
    )
