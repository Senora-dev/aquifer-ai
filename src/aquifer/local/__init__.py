"""Local mode: run Aquifer end to end on one machine, with no AWS account and no network.

Ingests repositories from local directories into a JSON-persisted in-memory index using the
deterministic extractors and a lexical hashing embedder, then serves the same MCP tools over
stdio (for a local MCP client) or loopback HTTP. Intended for evaluation, development,
contributor testing and demos — not for production; see docs/local-mode.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from aquifer.core.config import Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.core.models import Source, SourceType
from aquifer.ingestion.pipeline import IngestionPipeline, JobResult
from aquifer.local.connector import LocalRepoConnector

DEFAULT_STORE = ".aquifer-local/index.json"


def local_settings(store_path: str, repos: list[str] | None = None, *,
                   use_bedrock: bool = False) -> Settings:
    """Settings for local mode. Bedrock is used only when explicitly requested."""
    env = {
        "AQUIFER_VECTOR_STORE__BACKEND": "memory",
        "AQUIFER_VECTOR_STORE__MEMORY_PATH": str(store_path),
        "AQUIFER_EMBEDDING__PROVIDER": "bedrock" if use_bedrock else "hashing",
        "AQUIFER_SEMANTIC_INDEX__ENABLED": "true" if use_bedrock else "false",
        "AQUIFER_MCP__HOST": "127.0.0.1",
        "AQUIFER_MCP__AUTH_MODE": os.environ.get("AQUIFER_MCP__AUTH_MODE", "none"),
    }
    if repos is not None:
        import json

        env["AQUIFER_GITHUB__REPO_ALLOWLIST"] = json.dumps(repos)
    os.environ.update(env)
    from aquifer.core.config import get_settings
    from aquifer.core.hooks import get_pipeline

    get_settings.cache_clear()
    get_pipeline.cache_clear()
    return get_settings()


def ingest(root: Path, store_path: str = DEFAULT_STORE, *, use_bedrock: bool = False,
           connector: LocalRepoConnector | None = None) -> list[JobResult]:
    """Ingest every repository directory under ``root`` (idempotent; re-run to sync)."""
    from aquifer.ingestion.factory import build_embedder, build_vector_store
    from aquifer.semantic.interceptor import SemanticIndexInterceptor

    connector = connector or LocalRepoConnector.from_root(root)
    Path(store_path).parent.mkdir(parents=True, exist_ok=True)
    settings = local_settings(store_path, connector.repos, use_bedrock=use_bedrock)
    store = build_vector_store(settings)
    pipeline = IngestionPipeline(
        connector=connector,
        embedder=build_embedder(settings),
        vector_store=store,
        interceptors=InterceptorPipeline([SemanticIndexInterceptor()]),
        chunk_size=settings.ingestion.chunk_size,
        chunk_overlap=settings.ingestion.chunk_overlap,
    )
    source = Source(id="local", type=SourceType.GITHUB, config={"repos": connector.repos})
    return [pipeline.run_job(job) for job in connector.discover(source)]
