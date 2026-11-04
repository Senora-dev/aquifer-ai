"""Relationship status: what a source mentions vs claims vs proposes vs what is current.

The extractor reports an ``assertion`` (what the text does with the relationship). This module
combines it with the document context — a structured file at a revision, a merged/open/closed
pull request, an issue, a README — into a ``RelationshipStatus`` and the time the statement was
made (``as_of``). It never upgrades a model-extracted claim to ``current``: only deterministic
parsing of configuration at a known revision is ``current``.

| Source                         | present/added | removed  | negated | proposed | discussed |
| ------------------------------ | ------------- | -------- | ------- | -------- | --------- |
| structured file (parsed)       | current       | —        | —       | —        | —         |
| merged pull request            | claimed       | removed  | absent  | proposed | discussed |
| open pull request              | proposed      | proposed | absent  | proposed | discussed |
| closed, unmerged pull request  | not_merged    | not_merged | absent | not_merged | discussed |
| issue / README / ADR / other   | claimed       | removed  | absent  | proposed | discussed |
"""

from __future__ import annotations

from datetime import datetime

from aquifer.connectors.base import parse_github_timestamp
from aquifer.core.models import (
    Assertion,
    Document,
    DocumentKind,
    Relationship,
    RelationshipOrigin,
    RelationshipStatus,
)

_A = Assertion
_S = RelationshipStatus
_EXPLICIT = RelationshipOrigin.EXPLICIT_REFERENCE.value

_DEFAULT = {
    _A.PRESENT: _S.CLAIMED,
    _A.ADDED: _S.CLAIMED,
    _A.REMOVED: _S.REMOVED,
    _A.NEGATED: _S.ABSENT,
    _A.PROPOSED: _S.PROPOSED,
    _A.DISCUSSED: _S.DISCUSSED,
}
_OPEN_PR = {**_DEFAULT, _A.PRESENT: _S.PROPOSED, _A.ADDED: _S.PROPOSED, _A.REMOVED: _S.PROPOSED}
_CLOSED_PR = {
    **dict.fromkeys(_A, _S.NOT_MERGED),
    _A.NEGATED: _S.ABSENT,
    _A.DISCUSSED: _S.DISCUSSED,
}

# Statuses that affirm / deny that the relationship holds (used to detect conflicts).
AFFIRMING = frozenset({_S.CURRENT.value, _S.CLAIMED.value})
DENYING = frozenset({_S.REMOVED.value, _S.ABSENT.value})
# Display / precedence order, most authoritative first.
ORDER = [s.value for s in (_S.CURRENT, _S.CLAIMED, _S.REMOVED, _S.ABSENT, _S.PROPOSED,
                           _S.NOT_MERGED, _S.DISCUSSED)]


def _assertion(rel: Relationship) -> Assertion:
    try:
        return Assertion(rel.assertion) if rel.assertion else Assertion.PRESENT
    except ValueError:
        return Assertion.DISCUSSED  # unknown assertion: never treat it as a claim


def statement_time(document: Document) -> datetime | None:
    """When the document made its statements (merge time for merged PRs)."""
    if document.kind == DocumentKind.PR and document.state == "merged":
        merged = parse_github_timestamp(document.metadata.get("merged_at"))
        if merged:
            return merged
    return document.updated_at or document.revision_committed_at or document.created_at


def classify(rel: Relationship, document: Document) -> Relationship:
    """Set ``status`` and ``as_of`` on a relationship (mutates and returns it)."""
    if rel.origin == _EXPLICIT:
        rel.assertion = rel.assertion or Assertion.PRESENT.value
        rel.status = _S.CURRENT.value
        rel.as_of = document.revision_committed_at or document.updated_at
        return rel
    assertion = _assertion(rel)
    rel.assertion = assertion.value
    if document.kind == DocumentKind.PR:
        if document.state == "merged":
            table = _DEFAULT
        elif document.state == "open":
            table = _OPEN_PR
        else:  # closed without merging
            table = _CLOSED_PR
    else:
        table = _DEFAULT
    rel.status = table[assertion].value
    rel.as_of = statement_time(document)
    return rel


def aggregate(evidence: list[dict]) -> tuple[str, bool]:
    """Overall status of one relationship from all its evidence, and whether evidence conflicts.

    1. Observed in current configuration wins: a document's claim does not override it.
    2. Otherwise the most recent claim (``claimed`` vs ``removed``/``absent``) wins.
    3. Otherwise proposed > not_merged > discussed.
    ``conflicting`` is true when some evidence affirms and other evidence denies.
    """
    statuses = [e.get("status") or _S.CLAIMED.value for e in evidence]
    conflicting = bool(AFFIRMING & set(statuses)) and bool(DENYING & set(statuses))
    if _S.CURRENT.value in statuses:
        return _S.CURRENT.value, conflicting
    claims = [e for e in evidence if (e.get("status") or _S.CLAIMED.value) in AFFIRMING | DENYING]
    if claims:
        latest = max(claims, key=lambda e: str(e.get("as_of") or ""))
        return latest.get("status") or _S.CLAIMED.value, conflicting
    for status in (_S.PROPOSED, _S.NOT_MERGED, _S.DISCUSSED):
        if status.value in statuses:
            return status.value, conflicting
    return _S.CLAIMED.value, conflicting
