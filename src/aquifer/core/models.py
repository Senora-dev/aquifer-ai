"""Canonical data model for Aquifer.

Every connector normalizes its source into :class:`Document` objects. The ingestion pipeline
splits those into :class:`Chunk` objects, embeds them, and upserts them into the vector store.
The MCP server queries the store and returns :class:`SearchResult` objects.

Keeping this model small and source-agnostic is what lets new connectors plug in without
touching the pipeline, the vector store, or the MCP tools.
"""

from __future__ import annotations

import hashlib
import json
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
    # A file read from a repository at a specific commit (Terraform, YAML descriptors, ADRs).
    FILE = "file"


class RelationshipOrigin(StrEnum):
    """How a relationship (or entity) was obtained. Never upgraded without validation.

    - ``explicit_reference``: parsed deterministically from structured source (a Terraform
      reference, a docker-compose ``depends_on`` entry, a Backstage ``dependsOn``).
    - ``llm_extracted``: produced by the generative extractor. Not verified; see ``grounded``.
    """

    EXPLICIT_REFERENCE = "explicit_reference"
    LLM_EXTRACTED = "llm_extracted"


class ExtractionStatus(StrEnum):
    """Outcome of semantic extraction for a document (recorded so failures can be retried)."""

    OK = "ok"
    PARTIAL = "partial"  # structured extraction succeeded, LLM extraction failed
    FAILED = "failed"
    DISABLED = "disabled"
    NOT_RUN = "not_run"


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
    # How many times this job has been re-enqueued because the source was rate limited.
    # Bounded by the worker so a permanently throttled job eventually reaches the DLQ.
    attempt: int = 0


class Evidence(BaseModel):
    """Where a fact came from. Every field is optional and is only set when the source
    actually provides it — line numbers, revisions and timestamps are never invented.
    """

    document_id: str | None = None
    source_type: str | None = None
    repository: str | None = None
    path: str | None = None
    # Commit SHA the content was read at (files). ``None`` for issue/PR bodies, which are not
    # versioned by git.
    revision: str | None = None
    url: str | None = None
    # 1-based, inclusive line range within the source body.
    line_start: int | None = None
    line_end: int | None = None
    # How the location was determined: "parsed" (structured parser position) or
    # "text_match" (first line where the referenced name occurs in the text).
    locator: str | None = None
    snippet: str | None = None


class Entity(BaseModel):
    """An objective, named thing an artifact refers to — no interpretation, no judgment.

    Examples: ``{"type": "service", "name": "billing-api"}``,
    ``{"type": "jira_key", "name": "PROJ-400"}``. Entities are facts that improve retrieval;
    they are not conclusions about the artifact.
    """

    type: str  # service | component | datastore | repo | jira_key | system | person | other
    name: str  # canonical identifier, e.g. "billing-api" or "PROJ-400"
    # Scope in which ``name`` is unique, when names are not globally unique (e.g. a Terraform
    # resource address is only unique within one module directory of one repository).
    namespace: str | None = None
    origin: str = RelationshipOrigin.LLM_EXTRACTED.value
    evidence: list[Evidence] = Field(default_factory=list)


class Relationship(BaseModel):
    """An objective, factual edge from this artifact to another entity.

    A neutral graph edge that lets agents *retrieve* related context (e.g. "what references
    service-x?"). It is not a verdict — the agent decides what an edge means for its task.
    """

    type: str  # depends_on | references | part_of | modifies | mentions | ...
    target: str  # entity name this points to, e.g. "service-x" or "PROJ-123"
    description: str = ""  # short factual note, optional
    # The entity the edge starts from. ``None`` means the extractor did not say; the edge is
    # then only known to be stated *in the document*, not by which entity.
    source: str | None = None
    source_type: str | None = None
    target_type: str | None = None
    # Relationships indexed before provenance existed were all produced by the LLM extractor,
    # so that is the accurate default.
    origin: str = RelationshipOrigin.LLM_EXTRACTED.value
    # For LLM relationships: whether the target name was found in the source text. ``None`` =
    # not checked. Explicit references are grounded by construction.
    grounded: bool | None = None
    evidence: list[Evidence] = Field(default_factory=list)


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

    # --- provenance (all optional; set only when the source provides them) ---
    # Repository-relative path for file-backed documents (files, READMEs).
    path: str | None = None
    # Commit SHA the content was read at. ``None`` for issue/PR bodies.
    revision: str | None = None
    # Committer date of ``revision`` — when that snapshot was committed, which is *not* the
    # time this particular file last changed.
    revision_committed_at: datetime | None = None
    # When the connector retrieved this content from the source.
    fetched_at: datetime | None = None
    # Source-side state where meaningful (issue/PR: "open" | "closed" | "merged").
    state: str | None = None
    # Outcome of semantic extraction (see ``ExtractionStatus``).
    extraction_status: str | None = None

    def content_hash(self) -> str:
        """A stable fingerprint of everything that affects what gets indexed.

        Re-ingesting a document with an unchanged hash is skipped (no Bedrock calls), and the
        hash tags each indexed generation so stale chunks can be found and removed. Bump
        ``INDEX_FORMAT_VERSION`` when extraction or chunking changes so documents re-index.
        """
        if self.metadata.get("blob_sha"):
            # A git blob SHA already fingerprints the content exactly.
            return file_content_hash(self.metadata["blob_sha"], self.path)
        else:
            basis = {
                "title": self.title,
                "body": self.body,
                "labels": sorted(self.labels),
                "state": self.state,
                "url": self.url,
            }
        return content_hash_for(basis)

    @staticmethod
    def make_id(source_type: SourceType, repo: str, kind: DocumentKind, external_id: str) -> str:
        """Build a deterministic, collision-resistant document id.

        Deterministic ids let re-ingestion of an edited item overwrite its prior chunks
        instead of duplicating them.
        """
        return f"{source_type.value}:{repo}:{kind.value}:{external_id}"


# Bump when chunking, extraction or the indexed schema change in a way that should cause every
# document to be re-indexed on its next ingestion.
INDEX_FORMAT_VERSION = "2"


def content_hash_for(basis: dict) -> str:
    payload = json.dumps({"v": INDEX_FORMAT_VERSION, **basis}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def file_content_hash(blob_sha: str, path: str | None) -> str:
    """Content hash of a git-backed file, computable from a tree listing without the content."""
    return content_hash_for({"blob_sha": blob_sha, "path": path})


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
    # How many leading words of ``text`` repeat the tail of the previous chunk. Reassembling a
    # document drops them so the shared context is not emitted twice.
    overlap_words: int = 0

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

    # --- provenance & freshness (optional) ---
    path: str | None = None
    revision: str | None = None
    revision_committed_at: datetime | None = None
    fetched_at: datetime | None = None
    indexed_at: datetime | None = None
    state: str | None = None
    extraction_status: str | None = None
    # Fingerprint of the parent document's content at ingestion time (skip-unchanged).
    content_hash: str | None = None
    # Unique id of the ingestion that wrote this chunk. Replacing a document writes a new
    # generation first and then deletes older generations, so the document is never missing.
    generation: str | None = None
    # Character offsets of this chunk within ``title + "\n\n" + body`` and the 1-based line
    # range it covers within ``body`` (``None`` when the chunk is title-only).
    char_start: int | None = None
    char_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None

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

    # Provenance & freshness (optional; absent for chunks indexed before they existed).
    chunk_index: int | None = None
    path: str | None = None
    revision: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    revision_committed_at: datetime | None = None
    indexed_at: datetime | None = None
    state: str | None = None
    extraction_status: str | None = None
    content_hash: str | None = None


class Reconciliation(BaseModel):
    """A connector's statement of which documents currently exist within a scope.

    After a fetch, the pipeline deletes indexed documents that match ``scope`` but whose ids are
    not in ``live_document_ids``. Connectors only emit this when the listing is complete (e.g. a
    non-truncated git tree, or a README 404), so a partial listing never deletes data.
    """

    scope: dict
    live_document_ids: set[str] = Field(default_factory=set)
    reason: str = ""


class IndexedState(BaseModel):
    """What the index currently holds for one document (for skip-unchanged decisions)."""

    document_id: str
    content_hash: str | None = None
    updated_at: datetime | None = None
    extraction_status: str | None = None
