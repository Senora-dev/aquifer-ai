"""Token-aware text chunking.

Splits document text into overlapping chunks sized in *estimated* tokens. We avoid a heavy
tokenizer dependency in the lean core and approximate tokens with a cheap, deterministic
heuristic (~4 characters per token, min one token per word). This is accurate enough to keep
chunks comfortably under embedding-model limits; swap in a real tokenizer later behind the same
function signatures if exactness is ever required.
"""

from __future__ import annotations

import re

from aquifer.core.models import Chunk, Document

_WORD_RE = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """Rough token count for ``text`` (~4 chars/token)."""
    return max(1, (len(text) + 3) // 4)


def _word_tokens(word: str) -> int:
    return max(1, (len(word) + 3) // 4)


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Greedily pack whitespace-delimited words into ~``chunk_size``-token chunks.

    Consecutive chunks share roughly ``overlap`` tokens of trailing context so meaning that
    straddles a boundary is retrievable from either side.
    """
    text = text.strip()
    if not text:
        return []
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    words = _WORD_RE.findall(text)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for word in words:
        wt = _word_tokens(word)
        # A single oversized word still gets its own chunk rather than being dropped.
        if current and current_tokens + wt > chunk_size:
            chunks.append(" ".join(current))
            # Seed the next chunk with the trailing ``overlap`` tokens for continuity.
            current, current_tokens = _tail_for_overlap(current, overlap)
        current.append(word)
        current_tokens += wt

    if current:
        chunks.append(" ".join(current))
    return chunks


def _tail_for_overlap(words: list[str], overlap: int) -> tuple[list[str], int]:
    """Return the trailing words totaling up to ``overlap`` tokens, with their token count."""
    if overlap <= 0:
        return [], 0
    tail: list[str] = []
    tokens = 0
    for word in reversed(words):
        wt = _word_tokens(word)
        if tokens + wt > overlap:
            break
        tail.insert(0, word)
        tokens += wt
    return tail, tokens


def chunk_document(doc: Document, chunk_size: int, overlap: int) -> list[Chunk]:
    """Split a document into ``Chunk`` objects with denormalized filter fields.

    The title is prepended to the body so it contributes to every document's embeddings and
    gives short documents (e.g. a one-line issue) meaningful content to embed.
    """
    body = doc.body or ""
    combined = f"{doc.title}\n\n{body}".strip() if doc.title else body
    texts = chunk_text(combined, chunk_size=chunk_size, overlap=overlap)

    # Denormalize the document's neutral semantic metadata onto every chunk so the objective
    # entities/relationships/topics are queryable at the chunk granularity.
    sem = doc.semantic
    semantic_fields = {
        "summary": sem.summary if sem else "",
        "entities": list(sem.entities) if sem else [],
        "topics": list(sem.topics) if sem else [],
        "relationships": list(sem.relationships) if sem else [],
    }

    chunks: list[Chunk] = []
    for index, text in enumerate(texts):
        chunks.append(
            Chunk(
                chunk_id=Chunk.make_id(doc.id, index),
                document_id=doc.id,
                index=index,
                text=text,
                source_type=doc.source_type,
                kind=doc.kind,
                repo=doc.repo,
                title=doc.title,
                url=doc.url,
                author=doc.author,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                labels=doc.labels,
                **semantic_fields,
            )
        )
    return chunks
