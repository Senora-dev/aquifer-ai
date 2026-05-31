"""Interceptor loading and the pipeline that folds them.

The pipeline is the single place the rest of the system calls into the enterprise seam.
Ingestion calls :meth:`InterceptorPipeline.before_ingest` / ``after_ingest``; the MCP tools
call ``authorize`` / ``before_query`` / ``after_query``. With no interceptors configured
(the OSS default) every method is a transparent pass-through.

Interceptors are referenced by dotted path in configuration as ``"package.module:ClassName"``
(or ``"package.module.ClassName"``) and instantiated with no arguments.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from functools import lru_cache

from aquifer.core.config import get_settings
from aquifer.core.interfaces import Interceptor
from aquifer.core.models import Document, QueryContext, SearchResult


def load_interceptor(path: str) -> Interceptor:
    """Import and instantiate an interceptor from a dotted path.

    Accepts ``"pkg.module:ClassName"`` or ``"pkg.module.ClassName"``.
    """
    if ":" in path:
        module_name, _, attr = path.partition(":")
    else:
        module_name, _, attr = path.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"Invalid interceptor path: {path!r}")

    module = importlib.import_module(module_name)
    cls = getattr(module, attr)
    instance = cls()
    if not isinstance(instance, Interceptor):
        raise TypeError(f"{path!r} does not implement Interceptor")
    return instance


class InterceptorPipeline:
    """Runs a fixed, ordered list of interceptors as one composite interceptor."""

    def __init__(self, interceptors: Sequence[Interceptor] | None = None) -> None:
        self._interceptors: list[Interceptor] = list(interceptors or [])

    @classmethod
    def from_paths(cls, paths: Sequence[str]) -> InterceptorPipeline:
        return cls([load_interceptor(p) for p in paths])

    def __len__(self) -> int:
        return len(self._interceptors)

    # --- ingestion hooks -------------------------------------------------

    def before_ingest(self, document: Document) -> Document:
        for ic in self._interceptors:
            document = ic.before_ingest(document)
        return document

    def after_ingest(self, document: Document) -> None:
        for ic in self._interceptors:
            ic.after_ingest(document)

    # --- query hooks -----------------------------------------------------

    def before_query(self, ctx: QueryContext) -> QueryContext:
        for ic in self._interceptors:
            ctx = ic.before_query(ctx)
        return ctx

    def after_query(
        self, results: list[SearchResult], ctx: QueryContext
    ) -> list[SearchResult]:
        for ic in self._interceptors:
            results = ic.after_query(results, ctx)
        return results

    def authorize(self, principal: str | None, action: str, resource: str) -> bool:
        # Deny-overrides: every interceptor must allow. Empty pipeline ⇒ allowed.
        return all(ic.authorize(principal, action, resource) for ic in self._interceptors)


@lru_cache(maxsize=1)
def get_pipeline() -> InterceptorPipeline:
    """Build the process-wide pipeline from configured interceptor paths (cached)."""
    return InterceptorPipeline.from_paths(get_settings().interceptors)
