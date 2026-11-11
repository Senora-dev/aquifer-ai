"""Central configuration for Aquifer.

All settings are environment-driven so the exact same package runs unchanged in the
ingestion Lambdas and the Fargate MCP server — the CDK stack injects these as env vars.
Settings are grouped by concern and composed into a single ``Settings`` object via
``get_settings()`` (cached).

Nothing here reaches out to AWS; this module only declares *what* is configurable.
Concrete adapters (Bedrock, OpenSearch, GitHub) read these values when constructed.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
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
    # "bedrock" (production) or "hashing": a deterministic, local, *lexical* embedder for
    # offline tests, the benchmark and the no-AWS demo. It is not a semantic model.
    provider: Literal["bedrock", "hashing"] = "bedrock"
    # botocore retry/timeouts for Bedrock calls (standard mode = exponential backoff + jitter).
    max_attempts: int = 6
    read_timeout_s: int = 60
    connect_timeout_s: int = 10


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
    # "opensearch" (scale profile), "sqlite" (single-server profile: SQLite on local disk,
    # vectors searched in memory) or "memory" (tests and the local demo, optionally persisted to
    # ``memory_path`` as JSON).
    backend: Literal["opensearch", "sqlite", "memory"] = "opensearch"
    memory_path: str = ""
    sqlite_path: str = "/var/lib/aquifer/index.db"
    # opensearch-py client retries for 429/5xx/timeouts.
    max_retries: int = 3
    timeout_s: int = 30


class GitHubSettings(_Base):
    """GitHub connector configuration."""

    api_url: str = "https://api.github.com"
    # Where the GitHub token is stored: a Secrets Manager id/ARN, or a reference such as
    # "ssm:/aquifer/github-token", "file:/run/secrets/github-token" or "env:GITHUB_TOKEN"
    # (see aquifer.core.secrets).
    token_secret: str = ""
    # JSON array of exact "owner/repo" names, e.g. ["acme/billing", "acme/infra"].
    # Wildcards are not supported. Every listed repository is one trust domain: any
    # authenticated MCP client can read all of them.
    repo_allowlist: list[str] = Field(default_factory=list)
    # Page size for REST list calls (GitHub max is 100).
    page_size: int = 100
    # Repository files to ingest at the default branch head (glob, "**" = any directories).
    # Defaults cover the formats with deterministic extractors plus architecture decision
    # records. Set to [] to ingest only READMEs, issues and PRs.
    file_globs: list[str] = Field(
        default_factory=lambda: [
            "**/*.tf",
            "**/docker-compose*.yml",
            "**/docker-compose*.yaml",
            "**/compose.yml",
            "**/compose.yaml",
            "**/catalog-info.yaml",
            "**/catalog-info.yml",
            "**/adr/**/*.md",
            "**/adrs/**/*.md",
            "**/decisions/**/*.md",
        ]
    )
    max_files_per_repo: int = 1000
    max_file_bytes: int = 262_144
    files_per_page: int = 25
    # In-process retries for 5xx / network errors (bounded, exponential backoff).
    request_retries: int = 3
    # Rate-limit waits up to this many seconds are slept in-process; longer waits re-enqueue
    # the job with an SQS delay instead.
    max_inline_rate_limit_wait_s: int = 30

    @field_validator("repo_allowlist")
    @classmethod
    def _validate_repos(cls, repos: list[str]) -> list[str]:
        bad = [r for r in repos if not _REPO_RE.fullmatch(r or "")]
        if bad:
            raise ValueError(
                f"repo_allowlist entries must be exact 'owner/repo' names (no wildcards); "
                f"invalid: {bad}"
            )
        return repos


_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")


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
    # Each run re-reads this many seconds before the previous watermark, so items updated
    # around a run boundary (or with clock skew) are not missed. Re-reads are cheap because
    # unchanged documents are skipped by content hash.
    watermark_overlap_s: int = 300
    # Remove indexed documents of repositories no longer in the allowlist (each discovery run).
    purge_unlisted_repos: bool = True
    # Rate-limited jobs are re-enqueued with a delay at most this many times before failing
    # into the DLQ.
    max_rate_limit_requeues: int = 12
    # Documents whose extraction is due for a retry are re-queued by each discovery run (at
    # most this many per run, in jobs of retry_job_size).
    retry_sweep_limit: int = 200
    retry_job_size: int = 10


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
    # Deterministic parsers (Terraform, docker-compose, Backstage). Free, local, always safe.
    structured_extraction_enabled: bool = True
    # Also run the LLM on files a deterministic parser handled. Off by default: the parser's
    # explicit references are more precise and the LLM call costs money.
    llm_for_structured_files: bool = False
    # Retries of failed LLM extraction (independent of content changes). Delay doubles per
    # attempt: retry_base_s, 2x, 4x ... capped at retry_max_s.
    retry_base_s: int = 300
    retry_max_s: int = 21_600
    # Throttling / configuration failures are retried up to max_attempts; failures caused by
    # this document's content (rejected input, unparseable output) up to max_content_attempts.
    # After that the document is marked "failed" until its content changes.
    max_attempts: int = 8
    max_content_attempts: int = 3


class McpSettings(_Base):
    """MCP server (Fargate) configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    # Default number of results a search returns when the caller does not specify k.
    default_k: int = 10
    # Upper bound on k for every tool (protects the store and the agent's context window).
    max_k: int = 50
    # "token": every HTTP request must carry ``Authorization: Bearer <token>``.
    # "none": no authentication — only permitted when ``host`` is a loopback address.
    auth_mode: Literal["token", "none"] = "token"
    # {"principal-name": "token"} — injected from Secrets Manager in the deployed stack.
    auth_tokens: dict[str, str] = Field(default_factory=dict, repr=False)
    # Alternatively, a secret reference (e.g. "ssm:/aquifer/mcp-tokens") to the same JSON
    # object, read at startup. Used by the single-server profile.
    auth_tokens_ref: str = ""


class NodeSettings(_Base):
    """The single-server runtime (``aquifer serve``): scheduler, job queue and workers."""

    # Directory for the job queue and ingestion state (SQLite). The index lives at
    # ``vector_store.sqlite_path``.
    data_dir: str = "/var/lib/aquifer"
    # Run GitHub discovery this often (seconds). Unchanged content is skipped by hash, so
    # frequent runs cost GitHub API calls but no model calls.
    sync_interval_s: int = 900
    # Ingestion worker threads. Bedrock latency, not CPU, bounds throughput.
    workers: int = 2
    # A failed job is retried with exponential backoff (job_retry_base_s, doubling, capped at
    # job_retry_max_s) and moved to the dead-letter list after job_max_attempts.
    job_max_attempts: int = 5
    job_retry_base_s: int = 30
    job_retry_max_s: int = 3600
    # A job claimed by a worker that died (crash, restart) is reclaimed after this lease.
    job_lease_s: int = 1800


class IntegratorSettings(_Base):
    """The built-in MCP Integrator (single-server profile): imports content from external MCP
    servers into the index. Sources are configured at run time with `aquifer sources`."""

    enabled: bool = True
    # Base URL a browser reaches this server on, for OAuth redirects
    # (`<public_url>/oauth/callback`). Default: the SSM tunnel / local port.
    public_url: str = "http://127.0.0.1:18080"
    # Refuse source URLs (and OAuth endpoints) on private, loopback or link-local addresses
    # unless explicitly allowed (internal MCP servers).
    allow_private_networks: bool = False
    # Where source credentials are stored: a secret-reference prefix. "" = a 0600 file under
    # node.data_dir/credentials. The AWS profile uses "ssm:/aquifer/<name>/sources".
    credentials_prefix: str = ""
    default_schedule_hours: int = 24
    # How long an OAuth authorization waits for the browser redirect.
    authorize_timeout_s: int = 900
    # Incremental streams (ordered by update time) re-list everything every Nth run so items
    # deleted at the source can still be detected.
    full_listing_every_runs: int = 7
    # Items updated up to this long before the previous high-water mark are re-checked.
    overlap_s: int = 300
    # Pause between page calls to one source (seconds): a simple, polite rate guard.
    min_call_interval_s: float = 0.2


class WebSettings(_Base):
    """The product UI served by ``aquifer serve`` at ``/`` (see docs/ui.md)."""

    enabled: bool = True
    # Principals (MCP token names) that may change sources and access tokens in the UI. Every
    # other token can sign in and read, exactly like it can over MCP.
    admin_principals: list[str] = Field(default_factory=lambda: ["default"])
    session_hours: int = 12
    # Grounded answers: generated by this Bedrock model from the retrieved excerpts, only when
    # Bedrock is configured (embedding provider "bedrock") and the model answers a call.
    # "" = use semantic_index.model_id. Search with cited excerpts works without it.
    answers_enabled: bool = True
    answer_model_id: str = ""
    answer_max_tokens: int = 700
    # The labeled demo workspace (synthetic acme/* repositories bundled with the package).
    demo_enabled: bool = True
    # How MCP clients reach this server, shown on the Agents page (set by the AWS module):
    # "local" (this machine), "ssm-tunnel" (private: no inbound ports; clients go through an
    # SSM port forward) or "https" (public HTTPS on a domain; bearer tokens still required).
    network_access: str = "local"
    # The address remote clients use when network_access is "https" (e.g. https://host/mcp).
    remote_mcp_url: str = ""


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
    node: NodeSettings = Field(default_factory=NodeSettings)
    integrator: IntegratorSettings = Field(default_factory=IntegratorSettings)
    web: WebSettings = Field(default_factory=WebSettings)

    # Dotted import paths of Interceptor implementations to load, in order.
    # OSS default is empty (pass-through). Enterprise sets e.g.
    # ["aquifer_enterprise.rbac:RbacInterceptor", "aquifer_enterprise.audit:AuditInterceptor"].
    interceptors: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once from the environment."""
    return Settings()
