"""A deterministic, local, *lexical* embedder (feature hashing of word tokens).

Used for offline tests, the retrieval benchmark, and the no-AWS local mode. It needs no model,
no network and no credentials, and always produces the same vector for the same text. It is a
bag-of-words similarity, **not** a semantic model: it will not match synonyms or paraphrases.
Production deployments use :class:`aquifer.embedding.bedrock.BedrockEmbedder`.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence

from aquifer.core.interfaces import Embedder

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "where",
        "which",
        "who",
        "will",
        "with",
        "does",
        "do",
        "how",
    ]
)


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in _STOP:
            continue
        out.append(tok)
        # Compound identifiers also contribute their parts ("billing-api" → billing, api).
        parts = re.split(r"[-_.]", tok)
        if len(parts) > 1:
            out.extend(p for p in parts if p and p not in _STOP)
    return out


class HashingEmbedder(Embedder):
    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token, count in Counter(_tokens(text)).items():
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
