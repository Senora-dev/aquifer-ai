"""Builders that wire concrete adapters from configuration.

This is the one place the OSS baseline names concrete implementations. Swapping a vector store
or embedding provider is a change here, not in the pipeline, handlers, or MCP server.
"""

from __future__ import annotations

from aquifer.core.config import Settings, get_settings
from aquifer.core.interfaces import (
    Connector,
    Embedder,
    Inferencer,
    SemanticIndexer,
    VectorStore,
)
from aquifer.core.models import SourceType


def build_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    from aquifer.embedding.bedrock import BedrockEmbedder

    return BedrockEmbedder(settings.embedding)


def build_inferencer(settings: Settings | None = None) -> Inferencer:
    settings = settings or get_settings()
    from aquifer.semantic.bedrock_inferencer import BedrockInferencer

    return BedrockInferencer(settings.semantic_index)


def build_semantic_indexer(settings: Settings | None = None) -> SemanticIndexer:
    settings = settings or get_settings()
    from aquifer.semantic.engine import LlmSemanticIndexer

    return LlmSemanticIndexer(build_inferencer(settings), settings=settings.semantic_index)


def build_vector_store(settings: Settings | None = None) -> VectorStore:
    settings = settings or get_settings()
    from aquifer.vectorstore.opensearch import AossVectorStore

    return AossVectorStore(settings.vector_store, settings.embedding.dimensions)


def build_connector(
    source_type: SourceType | str,
    token: str | None = None,
    settings: Settings | None = None,
) -> Connector:
    settings = settings or get_settings()
    source_type = SourceType(source_type)
    if source_type == SourceType.GITHUB:
        from aquifer.connectors.github import GitHubConnector

        return GitHubConnector(settings.github, token=token)
    raise ValueError(f"No connector registered for source type {source_type!r}")
