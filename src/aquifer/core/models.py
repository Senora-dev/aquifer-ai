"""Canonical data model for Aquifer.

Every connector normalizes its source into :class:`Document` objects. The ingestion pipeline
splits those into :class:`Chunk` objects, embeds them, and upserts them into the vector store.
The MCP server queries the store and returns :class:`SearchResult` objects.

Keeping this model small and source-agnostic is what lets new connectors plug in without
touching the pipeline, the vector store, or the MCP tools.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    """The kind of upstream system a document came from."""

    GITHUB = "github"
    # Jira has no connector yet, but the type exists so the semantic-indexing prompt registry
    # can define Jira-specific extraction (demonstrating modularity across input types).
    JIRA = "jira"
    # Future: CONFLUENCE = "confluence", SLACK = "slack", ...


class DocumentKind(StrEnum):
    """The kind of object within a source."""

    ISSUE = "issue"
    PR = "pr"
    README = "readme"
    DISCUSSION = "discussion"
    COMMENT = "comment"


class Source(BaseModel):
    """A configured origin to ingest from (e.g. a GitHub org + repo filter)."""

    id: str
    type: SourceType
    # Free-form connector config (org, repo allowlist, etc.). Validated by the connector.
    config: dict = Field(default_factory=dict)


class FetchJob(BaseModel):
    """A bounded unit of fetch work — the payload of an SQS ingestion message.

    The worker processes exactly one job (one page of one kind from one repo) and, if more
    pages remain, enqueues a successor job with an advanced ``cursor``. This keeps every
    Lambda invocation short and makes a full backfill "just many small messages".
    """

    source_id: str
    source_type: SourceType
    # Repository / project identifier within the source, e.g. "myorg/myrepo".
    repo: str
    kind: DocumentKind
    # Opaque pagination cursor (page number, GraphQL endCursor, etc.). None = first page.
    cursor: str | None = None
    # Incremental watermark: only fetch items updated at/after this time.
    since: datetime | None = None


class Entity(BaseModel):
    """An objective, named thing an artifact refers to — no interpretation, no judgment.

    Examples: ``{"type": "service", "name": "billing-api"}``,
    ``{"type": "jira_key", "name": "PROJ-400"}``. Entities are facts that improve retrieval;
    they are not conclusions about the artifact.
    """

    type: str  # service | component | datastore | repo | jira_key | system | person | other
    name: str  # canonical identifier, e.g. "billing-api" or "PROJ-400"


class Relationship(BaseModel):
    """An objective, factual edge from this artifact to another entity.

    A neutral graph edge that lets agents *retrieve* related context (e.g. "what references
    service-x?"). It is not a verdict — the agent decides what an edge means for its task.
    """

    type: str  # depends_on | references | part_of | modifies | mentions
    target: str  # entity name this points to, e.g. "service-x" or "PROJ-123"
    description: str = ""  # short factual note, optional


class SemanticMetadata(BaseModel):
    """Neutral, structured metadata extracted at ingestion to facilitate accurate retrieval.

    Objective facts only — entities, relationships, topics, and a factual summary. Never
    conclusions, value judgments, or guardrail verdicts: Aquifer organizes context, the agent
    reasons over it. Produced via the ``before_ingest`` hook and denormalized onto every chunk
    as queryable index fields.
    """

    summary: str = ""  # neutral, factual description (aids semantic retrieval)
    entities: list[Entity] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class Document(BaseModel):
    """A normalized unit of engineering context, source-agnostic."""

    model_config = ConfigDict(use_enum_values=True)

    id: str
    source_type: SourceType
    # Stable identifier within the source, e.g. "myorg/myrepo#123".
    external_id: str
    kind: DocumentKind
    repo: str | None = None
    title: str = ""
    body: str = ""
    url: str = ""
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    # Source-specific extras that don't fit the canonical fields.
    metadata: dict = Field(default_factory=dict)
    # Neutral semantic metadata attached by the indexer at ingestion time (before_ingest).
    semantic: SemanticMetadata | None = None

    @staticmethod
    def make_id(source_type: SourceType, repo: str, kind: DocumentKind, external_id: str) -> str:
        """Build a deterministic, collision-resistant document id.

        Deterministic ids let re-ingestion of an edited item overwrite its prior chunks
        instead of duplicating them.
        """
        return f"{source_type.value}:{repo}:{kind.value}:{external_id}"


class Chunk(BaseModel):
    """A slice of a document's text, with its embedding and denormalized filter fields.

    Filterable document fields are copied onto the chunk so the vector store can apply
    metadata filters at query time without a join.
    """

    chunk_id: str
    document_id: str
    index: int
    text: str
    embedding: list[float] | None = None

    # Denormalized from the parent document for filtering / display.
    source_type: SourceType
    kind: DocumentKind
    repo: str | None = None
    title: str = ""
    url: str = ""
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)

    # Denormalized neutral semantic metadata, indexed per chunk so agents can filter/retrieve
    # on objective entities, topics, and relationships.
    summary: str = ""
    entities: list[Entity] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    @staticmethod
    def make_id(document_id: str, index: int) -> str:
        """Deterministic chunk id: ``{document_id}#{index}``."""
        return f"{document_id}#{index}"


class QueryContext(BaseModel):
    """Inputs to a search, carried through the interceptor pipeline.

    ``principal`` is unused by the OSS core (``authorize`` always returns True) but is the
    hook enterprise RBAC reads.
    """

    query: str
    k: int = 10
    filters: dict = Field(default_factory=dict)
    principal: str | None = None


class SearchResult(BaseModel):
    """A hit returned to an agent: the chunk, its score, and its neutral semantic metadata.

    The metadata fields are objective context (entities, relationships, topics) the agent uses
    for retrieval and its own reasoning — Aquifer draws no conclusions.
    """

    chunk_id: str
    document_id: str
    score: float
    text: str
    title: str = ""
    url: str = ""
    source_type: SourceType | None = None
    kind: DocumentKind | None = None
    repo: str | None = None

    # Denormalized neutral semantic metadata carried back from the index.
    summary: str = ""
    entities: list[Entity] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
