"""Deterministic extraction from two widely used YAML descriptor formats.

- **docker-compose**: each entry under ``services`` is a ``service`` entity; its ``depends_on``
  (list or mapping form) yields ``depends_on`` relationships to other services.
- **Backstage catalog descriptors** (``apiVersion: backstage.io/...``): the descriptor's entity
  (kind + ``metadata.name``) and its declared relations — ``spec.dependsOn`` (``depends_on``),
  ``spec.consumesApis`` / ``spec.providesApis`` (``consumes_api`` / ``provides_api``),
  ``spec.system`` / ``spec.subcomponentOf`` / ``spec.domain`` (``part_of``) and ``spec.owner``
  (``owned_by``).

Only keys these formats define are read. Arbitrary YAML is ignored rather than guessed at.
Line numbers come from the YAML parser's node positions.
"""

from __future__ import annotations

import posixpath

from aquifer.core.entities import parse_entity_ref
from aquifer.core.models import Entity, Relationship, RelationshipOrigin, SemanticMetadata
from aquifer.semantic.structured.common import EvidenceFactory

_EXPLICIT = RelationshipOrigin.EXPLICIT_REFERENCE.value

# Backstage spec field → (relationship type, default kind of the referenced entity).
_BACKSTAGE_RELATIONS = {
    "dependsOn": ("depends_on", "component"),
    "consumesApis": ("consumes_api", "api"),
    "providesApis": ("provides_api", "api"),
    "system": ("part_of", "system"),
    "subcomponentOf": ("part_of", "component"),
    "domain": ("part_of", "domain"),
    "owner": ("owned_by", "group"),
}


def _mapping(node) -> dict:  # noqa: ANN001 - yaml.Node
    import yaml

    if not isinstance(node, yaml.MappingNode):
        return {}
    out = {}
    for key, value in node.value:
        if isinstance(key, yaml.ScalarNode):
            out[key.value] = (key, value)
    return out


def _scalar(node) -> str | None:  # noqa: ANN001
    import yaml

    if isinstance(node, yaml.ScalarNode) and node.value not in ("", None):
        return str(node.value)
    return None


def _items(node) -> list:  # noqa: ANN001
    """Scalar items of a sequence (or a single scalar), as nodes."""
    import yaml

    if isinstance(node, yaml.SequenceNode):
        return [n for n in node.value if isinstance(n, yaml.ScalarNode)]
    if isinstance(node, yaml.ScalarNode):
        return [node]
    return []


class _Collector:
    def __init__(self, ev: EvidenceFactory) -> None:
        self.ev = ev
        self.entities: dict[tuple, Entity] = {}
        self.relationships: dict[tuple, Relationship] = {}

    def node_evidence(self, node):  # noqa: ANN001, ANN201
        line_start = node.start_mark.line + 1
        line_end = max(line_start, node.end_mark.line + (0 if node.end_mark.column == 0 else 1))
        return self.ev.lines(line_start, line_end)

    def entity(self, type_: str, name: str, node, namespace: str | None = None) -> None:  # noqa: ANN001
        key = (type_, name.lower(), namespace)
        if key not in self.entities:
            self.entities[key] = Entity(type=type_, name=name, namespace=namespace,
                                        origin=_EXPLICIT, evidence=[self.node_evidence(node)])

    def relationship(self, source: tuple[str, str], rel_type: str, target: tuple[str, str],
                     node, description: str = "") -> None:  # noqa: ANN001
        key = (source, rel_type, target[1].lower())
        if key in self.relationships:
            return
        self.relationships[key] = Relationship(
            type=rel_type, source=source[1], source_type=source[0], target=target[1],
            target_type=target[0], description=description, origin=_EXPLICIT, grounded=True,
            evidence=[self.node_evidence(node)],
        )

    def result(self, topic: str) -> SemanticMetadata | None:
        if not self.entities:
            return None
        return SemanticMetadata(
            entities=list(self.entities.values()),
            topics=[topic],
            relationships=list(self.relationships.values()),
        )


def _is_compose_path(path: str | None) -> bool:
    name = posixpath.basename(path or "").lower()
    return name.startswith(("docker-compose", "compose.")) and name.endswith((".yml", ".yaml"))


def _extract_compose(root, c: _Collector) -> bool:  # noqa: ANN001
    services = _mapping(root).get("services")
    if services is None:
        return False
    found = False
    for name, (key_node, svc_node) in _mapping(services[1]).items():
        found = True
        c.entity("service", name, key_node)
        depends = _mapping(svc_node).get("depends_on")
        if depends is None:
            continue
        dep_node = depends[1]
        dep_items = _items(dep_node) or [k for k, _ in getattr(dep_node, "value", [])
                                         if _scalar(k)]
        for item in dep_items:
            dep = _scalar(item)
            if not dep:
                continue
            c.entity("service", dep, item)
            c.relationship(("service", name), "depends_on", ("service", dep), item,
                           "docker-compose depends_on")
    return found


def _extract_backstage(root, c: _Collector) -> bool:  # noqa: ANN001
    doc = _mapping(root)
    api_version = _scalar(doc.get("apiVersion", (None, None))[1])
    kind = _scalar(doc.get("kind", (None, None))[1])
    if not api_version or not api_version.startswith("backstage.io/") or not kind:
        return False
    metadata = _mapping(doc.get("metadata", (None, None))[1])
    name_entry = metadata.get("name")
    name = _scalar(name_entry[1]) if name_entry else None
    if not name:
        return False
    ns_entry = metadata.get("namespace")
    namespace = _scalar(ns_entry[1]) if ns_entry else None
    if namespace == "default":
        namespace = None
    self_type = kind.lower()
    c.entity(self_type, name, name_entry[1], namespace)

    spec = _mapping(doc.get("spec", (None, None))[1])
    for field, (rel_type, default_kind) in _BACKSTAGE_RELATIONS.items():
        if field not in spec:
            continue
        for item in _items(spec[field][1]):
            raw = _scalar(item)
            if not raw:
                continue
            ref = parse_entity_ref(raw, default_type=default_kind)
            target_type = ref.type or default_kind
            # "default" is Backstage's implicit namespace; omit it so refs with and without it
            # identify the same entity.
            target_ns = None if ref.namespace == "default" else ref.namespace
            c.entity(target_type, ref.name, item, target_ns)
            c.relationship((self_type, name), rel_type, (target_type, ref.name), item,
                           f"backstage spec.{field}: {raw}")
    return True


def extract_yaml(text: str, ev: EvidenceFactory) -> SemanticMetadata | None:
    import yaml

    try:
        roots = [r for r in yaml.compose_all(text, Loader=yaml.SafeLoader) if r is not None]
    except yaml.YAMLError:
        return None
    collector = _Collector(ev)
    topic = None
    for root in roots:
        if _extract_backstage(root, collector):
            topic = "backstage-catalog"
        elif _is_compose_path(ev.path) and _extract_compose(root, collector):
            topic = "docker-compose"
    return collector.result(topic) if topic else None
