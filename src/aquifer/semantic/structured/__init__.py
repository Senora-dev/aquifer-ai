"""Deterministic (non-LLM) extraction for well-structured files, plus LLM grounding.

``extract_structured`` dispatches on the file path to a parser for Terraform or a supported YAML
descriptor. Results are marked ``origin = explicit_reference`` and carry parser-reported line
numbers. Documents no parser recognizes return ``None`` and are left to the LLM extractor.

``ground_llm_metadata`` checks each LLM-extracted relationship against the document text:
``quote_verified`` (the sentence the model cited occurs verbatim — it becomes the evidence line)
and ``mention_found`` (the target name occurs). Neither means the relationship is *true*; its
``status`` (claimed / removed / proposed / ...) comes from the model's ``assertion`` and the
document context (``aquifer.semantic.status``). LLM output is never upgraded to
``explicit_reference`` or ``current``.
"""

from __future__ import annotations

import re

from aquifer.core.entities import normalize_name
from aquifer.core.models import (
    Document,
    RelationshipOrigin,
    SemanticMetadata,
)
from aquifer.semantic.status import classify
from aquifer.semantic.structured.common import EvidenceFactory
from aquifer.semantic.structured.terraform import extract_terraform
from aquifer.semantic.structured.yaml_descriptors import extract_yaml

_LLM = RelationshipOrigin.LLM_EXTRACTED.value

__all__ = ["extract_structured", "ground_llm_metadata", "merge_metadata"]


def extract_structured(document: Document) -> SemanticMetadata | None:
    """Run the deterministic parser matching ``document.path``; ``None`` if none applies.

    Parsed relationships describe the configuration at the indexed revision: status
    ``current``.
    """
    path = (document.path or "").lower()
    if not path or not document.body:
        return None
    ev = EvidenceFactory(document)
    if path.endswith(".tf"):
        meta = extract_terraform(document.body, ev)
    elif path.endswith((".yml", ".yaml")):
        meta = extract_yaml(document.body, ev)
    else:
        return None
    for rel in meta.relationships if meta else []:
        classify(rel, document)
    return meta


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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _quote_line(ev: EvidenceFactory, quote: str | None) -> int | None:
    """1-based body line where the model's quote starts, if it occurs verbatim (modulo
    whitespace and case); otherwise ``None``."""
    if not quote or len(quote.strip()) < 8:
        return None
    words = [re.escape(w) for w in quote.split()]
    pattern = re.compile(r"\s+".join(words), re.I)
    m = pattern.search(ev.body)
    return ev.line_of(m.start()) if m else None


def ground_llm_metadata(metadata: SemanticMetadata, document: Document) -> SemanticMetadata:
    """Check each LLM relationship against the text (mutates and returns ``metadata``).

    - ``quote_verified``: the sentence the model cited occurs verbatim; its line becomes the
      evidence (``locator: "quote"``).
    - ``mention_found``: the target name occurs in the text. This only says the entity is
      *mentioned*; it is never evidence that the relationship is true.
    Status and time are then derived from the assertion and the document (``status.classify``).
    """
    ev = EvidenceFactory(document)
    title = (document.title or "").lower()
    for rel in metadata.relationships:
        rel.origin = _LLM
        quote_line = _quote_line(ev, rel.quote)
        mention_line = _find_line(ev, rel.target, rel.source)
        rel.quote_verified = quote_line is not None
        rel.mention_found = mention_line is not None or (
            bool(rel.target) and normalize_name(rel.target) in title)
        if quote_line is not None:
            rel.evidence = [ev.lines(quote_line, quote_line, locator="quote")]
        elif mention_line is not None:
            rel.evidence = [ev.lines(mention_line, mention_line, locator="text_match")]
        else:
            rel.evidence = []
        rel.quote = None
        classify(rel, document)
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
