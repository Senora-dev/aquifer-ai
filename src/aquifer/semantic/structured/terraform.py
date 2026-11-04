"""Deterministic extraction of Terraform blocks and references.

This is a deliberately small scanner, not an HCL implementation. It recognizes:

- ``resource "TYPE" "NAME" {``, ``data "TYPE" "NAME" {`` and ``module "NAME" {`` blocks;
- a module's ``source = "..."``;
- references inside a block's expressions to ``TYPE.NAME`` resources, ``data.TYPE.NAME`` and
  ``module.NAME``; references inside a ``depends_on = [...]`` list become ``depends_on``,
  every other reference becomes ``references``.

String literal contents (except ``${...}`` interpolations), comments and heredocs are masked
before matching, so a name that merely appears in a description string is *not* reported as a
reference. Everything returned carries the exact line it came from.
"""

from __future__ import annotations

import posixpath
import re

from aquifer.core.models import Entity, Relationship, RelationshipOrigin, SemanticMetadata
from aquifer.semantic.structured.common import EvidenceFactory

_EXPLICIT = RelationshipOrigin.EXPLICIT_REFERENCE.value

_BLOCK_RE = re.compile(
    r'^[ \t]*(resource|data|module)[ \t]+"([^"]+)"(?:[ \t]+"([^"]+)")?[ \t]*\{', re.M
)
_SOURCE_RE = re.compile(r'^[ \t]*source[ \t]*=[ \t]*"([^"]+)"', re.M)
_DEPENDS_RE = re.compile(r"\bdepends_on[ \t]*=[ \t]*\[", re.M)
# module.NAME | data.TYPE.NAME | TYPE.NAME where TYPE looks like provider_type (has "_").
_REF_RE = re.compile(
    r"(?<![\w.\-])(?:"
    r"(?P<module>module\.[A-Za-z_][\w-]*)"
    r"|(?P<data>data\.[a-z][a-z0-9]*_[a-z0-9_]+\.[A-Za-z_][\w-]*)"
    r"|(?P<res>[a-z][a-z0-9]*_[a-z0-9_]+\.[A-Za-z_][\w-]*)"
    r")"
)
_NOT_RESOURCE_PREFIXES = ("var.", "local.", "each.", "count.", "path.", "terraform.", "self.")


def mask_hcl(text: str) -> str:
    """Return ``text`` with comments, heredocs and literal string content replaced by spaces.

    ``${...}`` interpolations inside strings are kept. Offsets and newlines are preserved.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(a: int, b: int) -> None:
        for j in range(a, min(b, n)):
            if out[j] != "\n":
                out[j] = " "

    while i < n:
        c = text[i]
        if c == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            blank(i, end)
            i = end
        elif text.startswith("<<", i):
            m = re.match(r"<<-?([A-Za-z_]\w*)[^\n]*\n", text[i:])
            if not m:
                i += 2
                continue
            tag = m.group(1)
            close = re.compile(rf"^[ \t]*{re.escape(tag)}[ \t]*$", re.M)
            cm = close.search(text, i + m.end())
            end = n if cm is None else cm.end()
            blank(i, end)
            i = end
        elif c == '"':
            j = i + 1
            depth = 0
            while j < n:
                if depth == 0 and text[j] == "\\":
                    blank(j, j + 2)
                    j += 2
                    continue
                if depth == 0 and text.startswith("${", j):
                    depth = 1
                    j += 2
                    continue
                if depth > 0:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                    continue
                if text[j] == '"' or text[j] == "\n":
                    break
                blank(j, j + 1)
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _block_end(masked: str, open_brace: int) -> int:
    depth = 0
    for j in range(open_brace, len(masked)):
        if masked[j] == "{":
            depth += 1
        elif masked[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(masked)


def _bracket_spans(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    spans = []
    for m in _DEPENDS_RE.finditer(masked, start, end):
        depth = 0
        for j in range(m.end() - 1, end):
            if masked[j] == "[":
                depth += 1
            elif masked[j] == "]":
                depth -= 1
                if depth == 0:
                    spans.append((m.end(), j))
                    break
    return spans


def _address(kind: str, a: str, b: str | None) -> tuple[str, str]:
    """(entity type, address) for a block header."""
    if kind == "module":
        return "terraform_module", f"module.{a}"
    if kind == "data":
        return "terraform_data", f"data.{a}.{b}"
    return "terraform_resource", f"{a}.{b}"


def _ref_type(ref: str) -> str:
    if ref.startswith("module."):
        return "terraform_module"
    if ref.startswith("data."):
        return "terraform_data"
    return "terraform_resource"


def extract_terraform(text: str, ev: EvidenceFactory) -> SemanticMetadata | None:
    masked = mask_hcl(text)
    namespace = ev.terraform_namespace()
    module_dir = posixpath.dirname(ev.path or "") or "."
    entities: dict[tuple, Entity] = {}
    relationships: dict[tuple, Relationship] = {}

    def add_entity(type_: str, name: str, start: int, end: int, ns: str | None) -> None:
        key = (type_, name, ns)
        if key not in entities:
            entities[key] = Entity(type=type_, name=name, namespace=ns, origin=_EXPLICIT,
                                   evidence=[ev.span(start, end)])

    for block in _BLOCK_RE.finditer(masked):
        # Header names live inside string literals, which are masked; read them from the text.
        header = _BLOCK_RE.match(text, block.start())
        if header is None:
            continue
        kind, a, b = header.group(1), header.group(2), header.group(3)
        if kind != "module" and not b:
            continue
        open_brace = block.end() - 1
        end = _block_end(masked, open_brace)
        src_type, src = _address(kind, a, b)
        add_entity(src_type, src, block.start(), end, namespace)

        body_masked = masked[open_brace:end]
        if kind == "module":
            sm = _SOURCE_RE.search(text, open_brace, end)
            if sm:
                source = sm.group(1)
                target, target_ns = source, None
                if source.startswith(("./", "../")):
                    # Local module: resolve to a repo-relative directory.
                    target = posixpath.normpath(posixpath.join(module_dir, source))
                    target_ns = ev.repository
                add_entity("terraform_module_source", target, sm.start(), sm.end(), target_ns)
                key = (src, "references", target)
                relationships.setdefault(key, Relationship(
                    type="references", source=src, source_type=src_type, target=target,
                    target_type="terraform_module_source", description="module source",
                    origin=_EXPLICIT, evidence=[ev.span(sm.start(), sm.end())],
                ))

        depends_spans = _bracket_spans(masked, open_brace, end)
        for m in _REF_RE.finditer(body_masked):
            ref = m.group(0)
            if ref.startswith(_NOT_RESOURCE_PREFIXES) or ref == src:
                continue
            pos = open_brace + m.start()
            rel_type = (
                "depends_on" if any(s <= pos < e for s, e in depends_spans) else "references"
            )
            key = (src, rel_type, ref)
            if key in relationships:
                continue
            relationships[key] = Relationship(
                type=rel_type, source=src, source_type=src_type, target=ref,
                target_type=_ref_type(ref), origin=_EXPLICIT,
                evidence=[ev.span(pos, pos + len(ref))],
            )

    if not entities:
        return None
    return SemanticMetadata(
        summary="",
        entities=list(entities.values()),
        topics=["terraform"],
        relationships=list(relationships.values()),
    )
