"""Shared helpers for connectors.

Connectors deal only in *cursors* (opaque pagination tokens carried on a ``FetchJob``).
Where those cursors are persisted between scheduled runs (the incremental watermark) is the
ingestion layer's concern, not the connector's — that separation keeps connectors pure and
unit-testable without any AWS dependency.
"""

from __future__ import annotations

from datetime import datetime


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
