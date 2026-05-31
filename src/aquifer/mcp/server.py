"""MCP server — the Fargate entrypoint.

Registers the Context Lake tools with the MCP SDK and serves them over streamable HTTP behind
the internal NLB. The tool *logic* lives in :mod:`aquifer.mcp.tools`; this module is thin
framework wiring so it needs no unit tests of its own.

Run locally with::

    python -m aquifer.mcp.server
"""

from __future__ import annotations

import logging

from aquifer.core.config import get_settings
from aquifer.core.hooks import get_pipeline
from aquifer.ingestion.factory import build_embedder, build_vector_store
from aquifer.mcp.tools import AuthorizationError, ContextTools

logger = logging.getLogger(__name__)


def build_tools() -> ContextTools:
    """Construct the tool implementation with concrete adapters from configuration."""
    settings = get_settings()
    return ContextTools(
        embedder=build_embedder(settings),
        vector_store=build_vector_store(settings),
        interceptors=get_pipeline(),
        settings=settings,
    )


def create_server():
    """Create the FastMCP server with the three baseline tools registered."""
    from mcp.server.fastmcp import FastMCP

    settings = get_settings()
    tools = build_tools()
    mcp = FastMCP("aquifer", host=settings.mcp.host, port=settings.mcp.port)

    @mcp.tool()
    def search_context(
        query: str, k: int | None = None, filters: dict | None = None
    ) -> list[dict]:
        """Semantic search across the org's ingested engineering context.

        Returns ranked chunks, each with its text, title, source URL, and origin.
        """
        try:
            return tools.search_context(query, k=k, filters=filters)
        except AuthorizationError as exc:
            raise PermissionError(str(exc)) from exc

    @mcp.tool()
    def get_document(document_id: str) -> dict:
        """Fetch a full document by id, with its chunks reassembled in order."""
        try:
            return tools.get_document(document_id)
        except AuthorizationError as exc:
            raise PermissionError(str(exc)) from exc

    @mcp.tool()
    def list_sources() -> list[dict]:
        """List the sources Aquifer ingests context from."""
        try:
            return tools.list_sources()
        except AuthorizationError as exc:
            raise PermissionError(str(exc)) from exc

    @mcp.tool()
    def find_related(
        entity: str, relationship_types: list[str] | None = None, k: int | None = None
    ) -> dict:
        """Traverse the knowledge graph to find items related to an entity (e.g. a service).

        Returns items whose inferred relationships point at the entity, plus items that
        reference it directly — deduplicated and annotated with how each matched.
        """
        try:
            return tools.find_related(entity, relationship_types=relationship_types, k=k)
        except AuthorizationError as exc:
            raise PermissionError(str(exc)) from exc

    @mcp.tool()
    def list_entities(
        document_id: str | None = None, entity_type: str | None = None, k: int | None = None
    ) -> dict:
        """List the distinct entities extracted from the lake (e.g. services, Jira keys).

        Optionally scope to a document and/or an entity type. Returns a neutral inventory of
        ``{type, name}`` entities for the agent to search or relate on — no interpretation.
        """
        try:
            return tools.list_entities(document_id=document_id, entity_type=entity_type, k=k)
        except AuthorizationError as exc:
            raise PermissionError(str(exc)) from exc

    return mcp


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("starting Aquifer MCP server on %s:%d", settings.mcp.host, settings.mcp.port)
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
