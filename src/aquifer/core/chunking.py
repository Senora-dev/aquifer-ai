"""Token-aware text chunking with exact source positions.

Splits document text into overlapping chunks sized in *estimated* tokens. We avoid a heavy
tokenizer dependency in the lean core and approximate tokens with a cheap, deterministic
heuristic (~4 characters per token, min one token per word).

Each chunk is an exact slice of the source text (whitespace and line breaks preserved), and
records its character offsets and the line range it covers. That lets ``get_document`` rebuild
the original text exactly and lets every search hit point at real line numbers.

Words longer than a whole chunk (minified or base64 blobs) are split so that no chunk exceeds
the embedding model's input limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from aquifer.core.models import Chunk, Document

_WORD_RE = re.compile(r"\S+")
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count for ``text`` (~4 chars/token)."""
    return max(1, (len(text) + 3) // 4)


def _word_tokens(word_len: int) -> int:
    return max(1, (word_len + 3) // 4)


@dataclass(frozen=True)
class Span:
    """One chunk as a slice ``text[start:end]`` of the source."""

    start: int
    end: int
    # Leading words of this chunk that repeat the tail of the previous chunk.
    overlap_words: int


def _words(text: str, max_word_chars: int) -> list[tuple[int, int]]:
    """Word spans; words longer than ``max_word_chars`` are split into pieces."""
    spans: list[tuple[int, int]] = []
    for m in _WORD_RE.finditer(text):
        start, end = m.span()
        while end - start > max_word_chars:
            spans.append((start, start + max_word_chars))
            start += max_word_chars
        spans.append((start, end))
    return spans


def chunk_spans(text: str, chunk_size: int, overlap: int) -> list[Span]:
    """Greedily pack words into ~``chunk_size``-token spans with ~``overlap`` tokens shared."""
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    words = _words(text, max_word_chars=chunk_size * _CHARS_PER_TOKEN)
    if not words:
        return []

    spans: list[Span] = []
    cur_first = 0  # index into ``words`` of the current chunk's first word
    cur_tokens = 0
    carried = 0
    prev_end: int | None = None
    i = 0
    while i < len(words):
        wt = _word_tokens(words[i][1] - words[i][0])
        if i > cur_first and cur_tokens + wt > chunk_size:
            spans.append(_make_span(words, cur_first, i, carried, prev_end))
            prev_end = words[i - 1][1]
            # Seed the next chunk with the trailing ``overlap`` tokens for continuity.
            tail_first = i
            tail_tokens = 0
            if overlap > 0:
                j = i - 1
                while j > cur_first:
                    t = _word_tokens(words[j][1] - words[j][0])
                    if tail_tokens + t > overlap:
                        break
                    tail_tokens += t
                    j -= 1
                tail_first = j + 1
                # Never seed so much overlap that the next word cannot fit beside it.
                while tail_first < i and tail_tokens + wt > chunk_size:
                    tail_tokens -= _word_tokens(words[tail_first][1] - words[tail_first][0])
                    tail_first += 1
            carried = i - tail_first
            cur_first, cur_tokens = tail_first, tail_tokens
            continue
        cur_tokens += wt
        i += 1
    spans.append(_make_span(words, cur_first, len(words), carried, prev_end))
    return spans


def _make_span(
    words: list[tuple[int, int]], first: int, stop: int, carried: int, prev_end: int | None
) -> Span:
    start = words[first][0]
    # Without overlap, start right after the previous chunk so the whitespace between them is
    # kept and reassembly is exact.
    if carried == 0 and prev_end is not None:
        start = prev_end
    return Span(start=start, end=words[stop - 1][1], overlap_words=carried)


def chunk_text_with_overlap(text: str, chunk_size: int, overlap: int) -> list[tuple[str, int]]:
    """Chunk ``text``; each item is ``(space-joined words, overlap_words)``.

    Kept for API compatibility. ``overlap_words`` counts how many leading words repeat the tail
    of the previous chunk.
    """
    return [
        (" ".join(text[s.start : s.end].split()), s.overlap_words)
        for s in chunk_spans(text, chunk_size, overlap)
    ]


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Chunk ``text`` into space-joined word strings (API-compatible helper)."""
    return [t for t, _ in chunk_text_with_overlap(text, chunk_size, overlap)]


def combined_text(doc: Document) -> tuple[str, int]:
    """The text that is chunked: ``title + "\\n\\n" + body``, and the body's offset in it.

    The title is prepended so it contributes to every document's embeddings and gives short
    documents (e.g. a one-line issue) meaningful content to embed.
    """
    body = doc.body or ""
    if doc.title:
        prefix = f"{doc.title}\n\n"
        return prefix + body, len(prefix)
    return body, 0


def _line_of(body: str, offset: int) -> int:
    return body.count("\n", 0, max(0, offset)) + 1


def chunk_document(doc: Document, chunk_size: int, overlap: int) -> list[Chunk]:
    """Split a document into ``Chunk`` objects with denormalized filter and provenance fields."""
    text, body_offset = combined_text(doc)
    spans = chunk_spans(text, chunk_size=chunk_size, overlap=overlap)
    body = doc.body or ""

    # Denormalize the document's neutral semantic metadata onto every chunk so the objective
    # entities/relationships/topics are queryable at the chunk granularity.
    sem = doc.semantic
    semantic_fields = {
        "summary": sem.summary if sem else "",
        "entities": list(sem.entities) if sem else [],
        "topics": list(sem.topics) if sem else [],
        "relationships": list(sem.relationships) if sem else [],
    }
    content_hash = doc.content_hash()
    indexed_at = datetime.now(UTC)
    # Enough to rebuild the exact document from the index for re-processing: the source's own
    # metadata plus the body's leading whitespace (chunks start at the first word).
    source_metadata = {
        **{k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))},
        "_external_id": doc.external_id,
        "_body_leading_ws": body[: len(body) - len(body.lstrip())],
    }

    chunks: list[Chunk] = []
    for index, span in enumerate(spans):
        line_start = line_end = None
        if span.end > body_offset and body:
            first = max(span.start, body_offset) - body_offset
            # Skip leading whitespace so the range starts on the first line with content.
            while first < len(body) and body[first].isspace():
                first += 1
            line_start = _line_of(body, first)
            line_end = _line_of(body, span.end - body_offset - 1)
        chunks.append(
            Chunk(
                chunk_id=Chunk.make_id(doc.id, index),
                document_id=doc.id,
                index=index,
                text=text[span.start : span.end],
                overlap_words=span.overlap_words,
                source_type=doc.source_type,
                kind=doc.kind,
                repo=doc.repo,
                title=doc.title,
                url=doc.url,
                author=doc.author,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                labels=doc.labels,
                path=doc.path,
                revision=doc.revision,
                revision_committed_at=doc.revision_committed_at,
                fetched_at=doc.fetched_at,
                indexed_at=indexed_at,
                state=doc.state,
                extraction_status=doc.extraction_status,
                extraction_attempts=doc.extraction_attempts,
                extraction_error=doc.extraction_error,
                next_retry_at=doc.next_retry_at,
                source_metadata=source_metadata,
                content_hash=content_hash,
                char_start=span.start,
                char_end=span.end,
                line_start=line_start,
                line_end=line_end,
                **semantic_fields,
            )
        )
    return chunks


def reassemble(chunks: list[dict]) -> str:
    """Rebuild a document's text from its ordered chunk sources.

    Chunks with character offsets are stitched exactly. Chunks indexed before offsets existed
    fall back to dropping ``overlap_words`` leading words and joining with newlines.
    """
    if chunks and all(c.get("char_start") is not None for c in chunks):
        parts: list[str] = []
        prev_end: int | None = None
        for c in chunks:
            text = c.get("text", "")
            start = c["char_start"]
            if prev_end is None or start >= prev_end:
                parts.append(text)
            else:
                parts.append(text[prev_end - start :])
            prev_end = c.get("char_end", start + len(text))
        return "".join(parts)

    parts = []
    for position, source in enumerate(chunks):
        text = source.get("text", "")
        overlap = source.get("overlap_words") or 0
        if position and overlap > 0:
            text = " ".join(text.split()[overlap:])
        parts.append(text)
    return "\n".join(parts)


def newest_generation(sources: list[dict]) -> list[dict]:
    """If two generations of a document are briefly visible, keep only the newest one.

    "Newest" is the most recent source version (``updated_at``, else the revision commit time),
    then the most recent write — so a late retry of old content never shadows newer content.
    """
    generations = {s.get("generation") for s in sources}
    if len(generations) <= 1:
        return sources

    def key(gen):  # noqa: ANN001, ANN202
        rows = [s for s in sources if s.get("generation") == gen]
        version = max(str(s.get("updated_at") or s.get("revision_committed_at") or "")
                      for s in rows)
        written = max(str(s.get("indexed_at") or "") for s in rows)
        return (version, written, gen or "")

    keep = max(generations, key=key)
    return [s for s in sources if s.get("generation") == keep]
