"""Central configuration for Aquifer.

All settings are environment-driven so the exact same package runs unchanged in the
ingestion Lambdas and the Fargate MCP server — the CDK stack injects these as env vars.
Settings are grouped by concern and composed into a single ``Settings`` object via
``get_settings()`` (cached).

Nothing here reaches out to AWS; this module only declares *what* is configurable.
Concrete adapters (Bedrock, OpenSearch, GitHub) read these values when constructed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Environment variable prefix, e.g. AQUIFER_OPENSEARCH__ENDPOINT.
_ENV_PREFIX = "AQUIFER_"
_NESTED_DELIMITER = "__"


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_nested_delimiter=_NESTED_DELIMITER,
        extra="ignore",
    )


class EmbeddingSettings(_Base):
    """Amazon Bedrock embedding configuration."""

    # Bedrock model id. Titan Text Embeddings v2 → 1024-dim vectors.
    model_id: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 1024
    # Bedrock is called over a VPC (PrivateLink) endpoint; region drives the client.
    region: str = "us-east-1"
    # Max texts per Bedrock request (Titan embeds one at a time; batching is client-side).
    batch_size: int = 16


class VectorStoreSettings(_Base):
    """Amazon OpenSearch Serverless (AOSS) configuration."""

    # AOSS collection data-plane endpoint, e.g. https://xxxx.us-east-1.aoss.amazonaws.com
    endpoint: str = ""
    index: str = "aquifer-context"
    region: str = "us-east-1"
    # AOSS uses the "aoss" SigV4 service name (not "es").
    service: str = "aoss"
    # k-NN / HNSW index parameters.
    engine: str = "faiss"
    space_type: str = "cosinesimil"
    # Default number of results returned by a search.
    default_k: int = 10


class GitHubSettings(_Base):
    """GitHub connector configuration."""

    api_url: str = "https://api.github.com"
    # ARN/name of the Secrets Manager secret holding the GitHub token.
    token_secret: str = ""
    # Comma-separated org/repo allowlist, e.g. "myorg/*,otherorg/repo".
    repo_allowlist: list[str] = Field(default_factory=list)
    # Page size for REST list calls (GitHub max is 100).
    page_size: int = 100


class IngestionSettings(_Base):
    """Ingestion pipeline (Lambda + SQS + S3) configuration."""

    # SQS queue URL that fans out per-page fetch jobs to the worker Lambda.
    queue_url: str = ""
    # S3 bucket where per-source "since" watermarks and raw payloads are stored.
    state_bucket: str = ""
    state_prefix: str = "watermarks/"
    # Chunking: token-ish target sizes for the splitter (see core.chunking).
    chunk_size: int = 800
    chunk_overlap: int = 100


class SemanticIndexSettings(_Base):
    """Ingestion-time semantic indexing configuration.

    Uses a generative model on the org's own Bedrock to extract neutral, objective metadata
    (entities, relationships, topics) that improves retrieval. Reached over the same Bedrock VPC
    endpoint as embeddings — no external AI processing.
    """

    enabled: bool = True
    # Bedrock model id for extraction (a Claude model). Override per deployment/region.
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    region: str = "us-east-1"
    max_tokens: int = 1024
    temperature: float = 0.0
    # Truncate very large bodies before extraction to bound token cost/latency.
    max_input_chars: int = 12000


class McpSettings(_Base):
    """MCP server (Fargate) configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    # Default number of results a search returns when the caller does not specify k.
    default_k: int = 10


class Settings(_Base):
    """Top-level settings, composed from the grouped sections above."""

    environment: str = "dev"
    log_level: str = "INFO"

    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    semantic_index: SemanticIndexSettings = Field(default_factory=SemanticIndexSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)

    # Dotted import paths of Interceptor implementations to load, in order.
    # OSS default is empty (pass-through). Enterprise sets e.g.
    # ["aquifer_enterprise.rbac:RbacInterceptor", "aquifer_enterprise.audit:AuditInterceptor"].
    interceptors: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once from the environment."""
    return Settings()
