"""Shared helpers for connectors.

Connectors deal only in *cursors* (opaque pagination tokens carried on a ``FetchJob``).
Where those cursors are persisted between scheduled runs (the incremental watermark) is the
ingestion layer's concern, not the connector's — that separation keeps connectors pure and
unit-testable without any AWS dependency.
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache


def parse_github_timestamp(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``2024-01-02T03:04:05Z``) into a datetime."""
    if not value:
        return None
    # Python 3.11+ fromisoformat understands the trailing 'Z'.
    return datetime.fromisoformat(value)


def next_page_cursor(item_count: int, page: int, page_size: int) -> str | None:
    """Return the next page cursor, or ``None`` when the last page has been reached.

    A full page implies there may be more; a short page means we are done. This avoids parsing
    the ``Link`` header while remaining correct for GitHub's page-based list endpoints.
    """
    if item_count < page_size:
        return None
    return str(page + 1)


def cursor_to_page(cursor: str | None) -> int:
    """Cursors are 1-based page numbers; ``None`` means the first page."""
    return int(cursor) if cursor else 1


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex: ``**/`` = any directories (including none),
    ``*`` = any characters except ``/``, ``?`` = one character except ``/``."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def path_matches(path: str, globs: list[str] | tuple[str, ...]) -> bool:
    """Whether a repository-relative ``path`` matches any of ``globs``."""
    return any(_glob_regex(g).fullmatch(path) for g in globs)
