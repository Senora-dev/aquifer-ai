"""Conservative entity normalization.

Aquifer must not merge two different things because their names look alike, and must not
fabricate a canonical identity it cannot justify. The rules here are intentionally minimal:

- Names compare **case-insensitively** after trimming whitespace and wrapping quotes/backticks.
  Nothing else is rewritten: ``billing_api`` and ``billing-api`` stay distinct, as do
  ``billing-api`` and ``billing-api-v2``.
- A ``type:name`` reference (``service:billing-api``) is split into its type and name. A
  Backstage-style ``kind:namespace/name`` reference keeps its namespace.
- Entity identity is ``(type, name, namespace)``. Two entities that share a name but differ in
  type or namespace are reported as distinct candidates, never merged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Entity types we recognize as a ``type:`` prefix. Anything else with a colon (``PROJ-1``,
# ``https://...``, ``module.x``) is treated as a plain name.
KNOWN_TYPES = frozenset(
    {
        "service",
        "component",
        "datastore",
        "repo",
        "jira_key",
        "system",
        "person",
        "team",
        "group",
        "user",
        "api",
        "resource",
        "domain",
        "other",
        "terraform_module",
        "terraform_module_source",
        "terraform_resource",
        "terraform_data",
        "compose_service",
    }
)

_QUOTES = "\"'`"
_WS_RE = re.compile(r"\s+")
# Separates namespace from the typed key in flattened index keys: "ns|type:name".
NS_SEP = "|"


def normalize_name(name: str) -> str:
    """Case-fold and trim a name. Deliberately does not touch hyphens/underscores/versions."""
    name = (name or "").strip().strip(_QUOTES).strip()
    return _WS_RE.sub(" ", name).lower()


@dataclass(frozen=True)
class EntityRef:
    """A parsed reference: an optional type, a name, and an optional namespace."""

    name: str
    type: str | None = None
    namespace: str | None = None

    @property
    def name_key(self) -> str:
        return normalize_name(self.name)

    @property
    def key(self) -> str | None:
        """``type:name`` key, or ``None`` when the type is unknown."""
        return entity_key(self.type, self.name) if self.type else None


def parse_entity_ref(text: str, default_type: str | None = None) -> EntityRef:
    """Parse ``"service:billing-api"``, ``"component:default/billing-api"`` or ``"billing-api"``.

    Repository-qualified names (``acme/billing-api``) are kept whole: whether ``acme`` is a
    repository owner or a namespace cannot be decided from the string, so nothing is guessed.
    """
    raw = (text or "").strip().strip(_QUOTES).strip()
    type_: str | None = default_type
    namespace: str | None = None
    name = raw
    head, sep, rest = raw.partition(":")
    if sep and head.strip().lower() in KNOWN_TYPES and rest.strip():
        type_ = head.strip().lower()
        name = rest.strip()
        # Backstage refs carry a namespace: kind:namespace/name.
        if "/" in name and type_ in {"component", "api", "resource", "system", "group", "user",
                                     "domain"}:
            ns, _, short = name.partition("/")
            if ns and short:
                namespace, name = ns, short
    return EntityRef(name=name, type=type_, namespace=namespace)


def entity_key(type_: str | None, name: str) -> str:
    return f"{(type_ or 'other').strip().lower()}:{normalize_name(name)}"


def namespaced_key(type_: str | None, name: str, namespace: str | None) -> str:
    """Flattened identity key including namespace: ``"ns|type:name"`` or ``"type:name"``."""
    key = entity_key(type_, name)
    return f"{namespace}{NS_SEP}{key}" if namespace else key


def split_namespaced_key(key: str) -> tuple[str | None, str, str]:
    """Inverse of :func:`namespaced_key` → ``(namespace, type, name_key)``."""
    namespace: str | None = None
    if NS_SEP in key:
        namespace, _, key = key.rpartition(NS_SEP)
    type_, _, name = key.partition(":")
    return namespace, type_, name
