"""Interceptor fixtures used to exercise the loader and pipeline in tests."""

from aquifer.core.interfaces import Interceptor
from aquifer.core.models import Document, QueryContext, SearchResult


class TagInterceptor(Interceptor):
    """Records calls and tags documents/queries so ordering can be asserted."""

    def before_ingest(self, document: Document) -> Document:
        document.labels = [*document.labels, "tagged"]
        return document

    def before_query(self, ctx: QueryContext) -> QueryContext:
        ctx.filters = {**ctx.filters, "tagged": True}
        return ctx

    def after_query(self, results: list[SearchResult], ctx: QueryContext) -> list[SearchResult]:
        # Reverse to prove the hook can reorder results.
        return list(reversed(results))


class DenyInterceptor(Interceptor):
    """Denies every authorization decision."""

    def authorize(self, principal, action, resource) -> bool:  # noqa: ANN001
        return False
