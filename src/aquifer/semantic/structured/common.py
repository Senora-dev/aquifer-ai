"""Helpers shared by the deterministic extractors."""

from __future__ import annotations

import posixpath

from aquifer.core.models import Document, Evidence

_SNIPPET_CHARS = 200


class EvidenceFactory:
    """Builds :class:`Evidence` for positions within one document's body."""

    def __init__(self, document: Document) -> None:
        self.document = document
        self.body = document.body or ""
        self.path = document.path
        self.repository = document.repo
        self._line_starts = [0]
        for i, ch in enumerate(self.body):
            if ch == "\n":
                self._line_starts.append(i + 1)

    def line_of(self, offset: int) -> int:
        """1-based line containing character ``offset``."""
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def line_text(self, line: int) -> str:
        start = self._line_starts[line - 1]
        end = self.body.find("\n", start)
        return self.body[start : end if end != -1 else len(self.body)]

    def lines(self, line_start: int, line_end: int, locator: str = "parsed") -> Evidence:
        doc = self.document
        url = doc.url or None
        # File permalinks are pinned to a commit, so a line anchor is stable.
        if url and doc.revision and doc.path:
            anchor = f"#L{line_start}" if line_start == line_end else f"#L{line_start}-L{line_end}"
            url = f"{url}{anchor}"
        snippet = self.line_text(line_start).strip()[:_SNIPPET_CHARS] or None
        return Evidence(
            document_id=doc.id,
            source_type=str(doc.source_type),
            repository=doc.repo,
            path=doc.path,
            revision=doc.revision,
            url=url,
            line_start=line_start,
            line_end=line_end,
            locator=locator,
            snippet=snippet,
        )

    def span(self, start: int, end: int, locator: str = "parsed") -> Evidence:
        return self.lines(self.line_of(start), self.line_of(max(start, end - 1)), locator)

    def terraform_namespace(self) -> str:
        """Terraform addresses are unique per module directory of one repository."""
        directory = posixpath.dirname(self.path or "") or "."
        return f"{self.repository}:{directory}" if self.repository else directory
