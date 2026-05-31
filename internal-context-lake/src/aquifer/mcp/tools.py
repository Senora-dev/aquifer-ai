"""The Context Lake's MCP tool implementations, decoupled from the MCP framework.

``ContextTools`` holds the embedder, vector store, and interceptor pipeline and exposes the
three baseline tools as plain methods returning JSON-friendly dicts. ``server.py`` wraps these
in the MCP SDK. Keeping the logic here (framework-free) makes the tools unit-testable and lets
the enterprise seam wrap every call: ``authorize`` gates access, ``before_query`` can inject
tenant filters, and ``after_query`` can redact or re-rank.
"""

from __future__ import annotations

from aquifer.core.config import Settings, get_settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.interfaces import Embedder, VectorStore
from aquifer.core.models import QueryContext, SourceType


class AuthorizationError(Exception):
    """Raised when the interceptor pipeline denies an action."""


class ContextTools:
    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        interceptors: InterceptorPipeline | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.interceptors = interceptors or InterceptorPipeline()
        self.settings = settings or get_settings()

    def search_context(
        self,
        query: str,
        k: int | None = None,
        filters: dict | None = None,
        principal: str | None = None,
    ) -> list[dict]:
        """Semantic search over the Context Lake. Returns ranked chunks with source links."""
        ctx = QueryContext(
            query=query,
            k=k or self.settings.mcp.default_k,
            filters=filters or {},
            principal=principal,
        )
        if not self.interceptors.authorize(principal, "search", "*"):
            raise AuthorizationError("Not authorized to search the Context Lake")

        ctx = self.interceptors.before_query(ctx)
        vector = self.embedder.embed_query(ctx.query)
        results = self.vector_store.query(vector, ctx.k, ctx.filters or None)
        results = self.interceptors.after_query(results, ctx)
        return [r.model_dump(mode="json") for r in results]

    def get_document(self, document_id: str, principal: str | None = None) -> dict:
        """Return a full document (its ordered chunks reassembled) by id."""
        if not self.interceptors.authorize(principal, "read", document_id):
            raise AuthorizationError(f"Not authorized to read {document_id}")

        sources = self.vector_store.get_by_document(document_id)
        if not sources:
            return {"document_id": document_id, "found": False}

        head = sources[0]
        return {
            "document_id": document_id,
            "found": True,
            "title": head.get("title", ""),
            "url": head.get("url", ""),
            "source_type": head.get("source_type"),
            "kind": head.get("kind"),
            "repo": head.get("repo"),
            "text": "\n".join(s.get("text", "") for s in sources),
            "chunk_count": len(sources),
        }

    def list_sources(self, principal: str | None = None) -> list[dict]:
        """List the configured sources Aquifer ingests from.

        Baseline reports configured sources from settings; ingested document counts are a
        planned enhancement (requires a store aggregation).
        """
        if not self.interceptors.authorize(principal, "list_sources", "*"):
            raise AuthorizationError("Not authorized to list sources")

        sources: list[dict] = []
        for repo in self.settings.github.repo_allowlist:
            sources.append({"source_type": SourceType.GITHUB.value, "repo": repo})
        return sources

    # --- retrieval tools over the neutral semantic index -----------------
    # These query the indexed metadata directly (no vector ranking) so an agent can pull the
    # objective context it needs. Aquifer returns facts; the agent does the reasoning.

    def find_related(
        self,
        entity: str,
        relationship_types: list[str] | None = None,
        k: int | None = None,
        principal: str | None = None,
    ) -> dict:
        """Traverse the knowledge graph: find items related to an entity (e.g. a service).

        Matches two ways and merges them: items whose inferred relationships *point at* the
        entity (graph edges, via ``relationship_targets``), and items that reference the entity
        directly (via ``entities``). Results are deduplicated by document and annotated with how
        they matched.
        """
        if not self.interceptors.authorize(principal, "find_related", entity):
            raise AuthorizationError(f"Not authorized to traverse relationships for {entity!r}")
        k = k or self.settings.mcp.default_k

        related: dict[str, dict] = {}

        def _merge(result, via: str) -> None:
            item = related.get(result.document_id)
            if item is None:
                item = {
                    "document_id": result.document_id,
                    "title": result.title,
                    "url": result.url,
                    "source_type": result.source_type,
                    "kind": result.kind,
                    "repo": result.repo,
                    "summary": result.summary,
                    "relationships": [r.model_dump() for r in result.relationships],
                    "via": set(),
                }
                related[result.document_id] = item
            item["via"].add(via)

        # 1. Graph edges: items whose relationships target the entity.
        edge_filters: dict = {"relationship_targets": entity}
        if relationship_types:
            edge_filters["relationship_types"] = relationship_types
        for result in self.vector_store.search_by_metadata(edge_filters, k):
            _merge(result, "relationship")

        # 2. Direct references: items that name the entity among their extracted entities.
        for result in self.vector_store.search_by_metadata({"entity_names": entity}, k):
            _merge(result, "entity")

        items = []
        for item in related.values():
            item["via"] = sorted(item["via"])
            items.append(item)
        return {"entity": entity, "related": items, "count": len(items)}

    def list_entities(
        self,
        document_id: str | None = None,
        entity_type: str | None = None,
        k: int | None = None,
        principal: str | None = None,
    ) -> dict:
        """List the distinct entities extracted from the lake — objective facts, no judgments.

        Scope to a single ``document_id`` and/or an ``entity_type`` (e.g. ``"service"``). Returns
        the deduplicated ``{type, name}`` entities the agent can then search or relate on. This
        is a neutral inventory, not an assessment.
        """
        if not self.interceptors.authorize(principal, "list_entities", document_id or "*"):
            raise AuthorizationError("Not authorized to list entities")
        k = k or self.settings.mcp.default_k

        filters: dict = {}
        if document_id:
            filters["document_id"] = document_id
        if entity_type:
            filters["entity_types"] = entity_type

        entities: list[dict] = []
        seen: set[tuple] = set()
        for result in self.vector_store.search_by_metadata(filters, k):
            for entity in result.entities:
                if entity_type and entity.type != entity_type:
                    continue
                key = (entity.type, entity.name)
                if key in seen:
                    continue
                seen.add(key)
                entities.append(entity.model_dump())

        return {
            "scope": {"document_id": document_id, "entity_type": entity_type},
            "entities": entities,
            "count": len(entities),
        }
