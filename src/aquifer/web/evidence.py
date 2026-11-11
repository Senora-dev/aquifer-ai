"""Pick the exact supporting lines of a search hit, and say which question terms they contain.

Deterministic and model-free: a hit's chunk spans known body lines; within them, the shortest
window that contains the most distinct question terms is the excerpt. Term matching is shown to
the reader as a fact ("mentions: kafka, invoice"), never as a score.
"""

from __future__ import annotations

import re

from aquifer.embedding.hashing import _tokens

# Question words that carry no topic (in addition to the embedder's stop words).
_QUESTION_WORDS = frozenset([
    "why", "did", "we", "our", "us", "i", "you", "me", "my", "when", "there", "any", "about",
    "into", "than", "then", "so", "if", "can", "could", "would", "should", "shall", "may",
    "might", "must", "been", "being", "am", "get", "got", "use", "used", "using", "make", "made",
    "know", "tell", "show", "find", "team", "teams", "decide", "decided", "decision",
    # "where is that defined?" asks for a location, not for the word.
    "define", "defined", "declared", "located",
])
_COMPOUND = re.compile(r"[-_.]")
_MAX_WINDOW = 6
_CUE = re.compile(r"\b(decid\w*|decision|agreed|accepted|rejected|superseded|deprecated|"
                  r"reverted|chose|instead|status:|closed:|merged:)", re.I)


def query_terms(question: str) -> list[str]:
    """Topic terms of a question. A compound identifier ("billing-api") is one term: its parts
    are not counted separately, so naming one service does not look like three topics."""
    tokens = _tokens(question)
    parts = {p for tok in tokens if _COMPOUND.search(tok) for p in _COMPOUND.split(tok)}
    seen: list[str] = []
    for tok in tokens:
        if len(tok) < 2 or tok in _QUESTION_WORDS or tok in seen:
            continue
        if not _COMPOUND.search(tok) and tok in parts:
            continue
        seen.append(tok)
    return seen


def _term_matches(term: str, token: str) -> bool:
    if term == token:
        return True
    if _COMPOUND.search(term):
        return False  # identifiers match exactly: billing-api is not billing-api-v2
    if len(term) >= 4 and len(token) >= 4 and (token.startswith(term) or term.startswith(token)):
        return True
    return len(term) >= 6 and len(token) >= 6 and term[:5] == token[:5]


def line_terms(line: str, terms: list[str]) -> set[str]:
    tokens = set(_tokens(line))
    return {t for t in terms if any(_term_matches(t, tok) for tok in tokens)}


def doc_body(text: str, title: str) -> str:
    """The body of a reassembled document (``title + "\\n\\n" + body``)."""
    prefix = f"{title}\n\n" if title else ""
    return text[len(prefix):] if prefix and text.startswith(prefix) else text


def best_window(lines: list[str], lo: int, hi: int, terms: list[str]) -> dict:
    """The best excerpt within 1-based lines ``lo..hi``.

    Returns ``{"start", "end", "terms"}``: the focus lines (inclusive) and the question terms
    they contain. Without any term in range, the first non-empty lines of the range.
    """
    lo = max(1, lo)
    hi = min(len(lines), hi)
    hits = {n: line_terms(lines[n - 1], terms) for n in range(lo, hi + 1)} if terms else {}
    matched_lines = [n for n, t in hits.items() if t]
    if not matched_lines:
        start = next((n for n in range(lo, hi + 1) if lines[n - 1].strip()), lo)
        end = min(hi, start + 3)
        return {"start": start, "end": max(start, end), "terms": []}
    best = None
    for i, s in enumerate(matched_lines):
        covered: set[str] = set()
        for e in matched_lines[i:]:
            if e - s >= _MAX_WINDOW:
                break
            covered |= hits[e]
            # Prefer windows with a matching body line over a matching heading alone.
            prose = any(hits[n] and not lines[n - 1].lstrip().startswith("#")
                        for n in range(s, e + 1) if n in hits)
            key = (len(covered), prose, -(e - s), -s)
            if best is None or key > best[0]:
                best = (key, s, e, set(covered))
    _, start, end, covered = best
    # Take in an adjacent line that records an outcome ("Closed: we decided to keep SQS.").
    for _ in range(2):
        if end + 1 <= hi and end - start + 1 < _MAX_WINDOW and _CUE.search(lines[end]):
            end += 1
        elif start - 1 >= lo and end - start + 1 < _MAX_WINDOW and _CUE.search(lines[start - 2]):
            start -= 1
    # Finish a sentence that wraps onto the next line(s) (prose keeps its indentation).
    while (end < hi and end - start + 1 < _MAX_WINDOW and lines[end - 1].strip()
           and not lines[end - 1].rstrip().endswith((".", "!", "?", ":", ";"))
           and not lines[end - 1].lstrip().startswith("#")
           and _indent(lines[end]) >= _indent(lines[end - 1])
           and lines[end].strip() and not lines[end].lstrip().startswith(("#", "-", "*"))):
        end += 1
    # In indented configuration (YAML, Compose), name the block the lines belong to:
    # "orders-service:" above "depends_on: - billing-api".
    parent = _parent_key(lines, start, lo)
    if parent is not None and end - parent + 1 <= _MAX_WINDOW:
        start = parent
    return {"start": start, "end": end, "terms": [t for t in terms if t in covered]}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _parent_key(lines: list[str], start: int, lo: int) -> int | None:
    """The nearest less-indented ``key:`` line above an indented ``start`` line, if any."""
    level = _indent(lines[start - 1])
    if not level or not lines[start - 1].strip():
        return None
    for n in range(start - 1, lo - 1, -1):
        text = lines[n - 1]
        if not text.strip():
            continue
        if _indent(text) < level:
            return n if re.match(r"^\s*[\w.\-\"']+:\s*$", text) else None
    return None


def excerpt_lines(lines: list[str], start: int, end: int, context: int = 1) -> list[dict]:
    first = max(1, start - context)
    last = min(len(lines), end + context)
    return [{"n": n, "text": lines[n - 1][:400], "focus": start <= n <= end}
            for n in range(first, last + 1)]


def permalink(url: str | None, *, revision: str | None, path: str | None, start: int | None,
              end: int | None) -> str | None:
    """Line anchors only on commit-pinned file URLs (issue and PR pages have no lines)."""
    if not url:
        return None
    url = url.split("#", 1)[0]
    if revision and path and start and re.search(r"/blob/", url):
        return url + (f"#L{start}" if start == end else f"#L{start}-L{end}")
    return url
