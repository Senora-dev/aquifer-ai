"""Modular semantic-indexing prompts.

The indexer asks a generative model (the org's own Bedrock) to read an artifact and return
**neutral, objective** structured metadata — named entities, factual relationships, topics —
that improves search and retrieval. *What* to extract is universal and lives in
:data:`BASE_SYSTEM_PROMPT` + the JSON contract; *how to read this kind of artifact* is
type-specific and lives in a :class:`PromptTemplate`'s instructions.

Strict neutrality is the whole point: the prompts extract facts, never conclusions, judgments,
recommendations, or guardrail verdicts. The agent does the reasoning; Aquifer organizes context.

A :class:`PromptRegistry` maps ``(source_type, kind)`` to a template with graceful fallback:
exact match → per-source default → generic. New connectors register their own templates without
touching the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from aquifer.core.models import DocumentKind, SemanticMetadata, SourceType

# The JSON contract, generated from the model so it cannot drift from core.models.SemanticMetadata.
SEMANTIC_SCHEMA: dict = SemanticMetadata.model_json_schema()

# A compact, human-readable example of the required output, embedded in the system prompt.
SEMANTIC_OUTPUT_CONTRACT = """{
  "summary": "1-2 sentence neutral, factual description of what this artifact is (no opinions)",
  "entities": [
    {"type": "service|component|datastore|repo|jira_key|system|person|other",
     "name": "canonical identifier, e.g. 'billing-api' or 'PROJ-400'"}
  ],
  "topics": ["short objective topical keywords, e.g. 'caching', 'authentication'"],
  "relationships": [
    {"type": "depends_on|references|part_of|modifies|mentions",
     "target": "the entity name this points to (e.g. 'redis' or 'PROJ-400')",
     "description": "short factual note (optional)"}
  ]
}"""

BASE_SYSTEM_PROMPT = (
    "You are a precise information extractor building a neutral Context Lake for AI agents. Read "
    "the provided artifact and extract ONLY objective, factual, structured metadata that improves "
    "search and retrieval.\n\n"
    "Strict neutrality (this is critical):\n"
    "- Extract facts only: named entities (services, components, datastores, repos, Jira keys, "
    "systems, people) and the factual relationships between them.\n"
    "- DO NOT interpret, judge, rate, prioritize, recommend, or assess risk. No conclusions, no "
    "guardrails, no 'should'/'must', no severity, no verdicts. The agent reasons; you only "
    "organize context.\n"
    "- Use canonical, lowercase, hyphenated names for services/components; use exact keys for "
    "tickets (e.g. 'PROJ-123').\n"
    "- Represent a stated dependency or 'blocked by X' link factually as a relationship (e.g. "
    "depends_on); represent epic/parent membership as part_of. Do not infer importance.\n"
    "- Assert only what the text supports; prefer empty lists over guesses.\n"
    "- The summary must be a neutral factual description, not an evaluation.\n"
    "- Respond with ONLY a single valid JSON object (no markdown, no prose) matching exactly "
    "this shape:\n" + SEMANTIC_OUTPUT_CONTRACT
)


@dataclass(frozen=True)
class PromptTemplate:
    """A named, type-specific extraction prompt: shared system prompt + focused instructions."""

    name: str
    instructions: str
    system: str = BASE_SYSTEM_PROMPT

    def render_user(self, document, max_chars: int) -> str:  # noqa: ANN001 - Document
        """Build the user message: type-specific guidance + the artifact's metadata and body."""
        body = (document.body or "")[:max_chars]
        labels = ", ".join(document.labels) if document.labels else "none"
        return (
            f"{self.instructions}\n\n"
            "--- ARTIFACT METADATA ---\n"
            f"source: {document.source_type}\n"
            f"kind: {document.kind}\n"
            f"repo/project: {document.repo or 'n/a'}\n"
            f"title: {document.title}\n"
            f"url: {document.url}\n"
            f"labels: {labels}\n\n"
            f"--- ARTIFACT CONTENT ---\n{body}\n"
        )


# --- Type-specific extraction guidance (neutral, retrieval-focused) ------------------------

_GITHUB_CODE = (
    "This is a pull request or source change from GitHub. Extract the services/components/modules "
    "it touches and any entities it references. Capture relationships: depends_on (dependencies "
    "added or removed), modifies (APIs, schemas, or services changed), references (tickets, "
    "issues, or other services mentioned, including Jira keys). Names only — no impact or risk."
)

_GITHUB_ISSUE = (
    "This is a GitHub issue. Extract the services/components named and any referenced items "
    "(related issues/PRs by number, Jira keys, other services). Capture relationships as "
    "references/mentions and depends_on where the text states a dependency. Do not judge severity."
)

_GITHUB_README = (
    "This is a repository README. Extract the services/components the repository provides, its "
    "datastores and dependencies (depends_on), and any referenced systems. Capture ownership only "
    "as a factual entity (e.g. a team) if explicitly stated. Do not extract policies as verdicts."
)

_JIRA_TASK = (
    "This is a Jira work item. Extract all Jira keys (e.g. 'PROJ-400') as entities and their "
    "factual links: part_of for an epic/parent, depends_on for a 'blocked by' link, references for "
    "related tickets. Also extract the services/features named. Do not infer priority or status "
    "judgments."
)

_GENERIC = (
    "Extract the named entities (services, components, datastores, tickets, systems) and the "
    "factual relationships between them. Facts only — no judgments, conclusions, or verdicts."
)


class PromptRegistry:
    """Selects an extraction prompt for a document, with graceful fallback."""

    def __init__(self) -> None:
        self._exact: dict[tuple[SourceType, DocumentKind], PromptTemplate] = {}
        self._by_source: dict[SourceType, PromptTemplate] = {}
        self._default = PromptTemplate(name="generic", instructions=_GENERIC)

    def register(
        self,
        template: PromptTemplate,
        *,
        source_type: SourceType | None = None,
        kind: DocumentKind | None = None,
    ) -> PromptRegistry:
        """Register a template for an exact (source, kind), a whole source, or the default."""
        if source_type is not None and kind is not None:
            self._exact[(source_type, kind)] = template
        elif source_type is not None:
            self._by_source[source_type] = template
        else:
            self._default = template
        return self

    def select(self, source_type: SourceType, kind: DocumentKind) -> PromptTemplate:
        return (
            self._exact.get((source_type, kind))
            or self._by_source.get(source_type)
            or self._default
        )


def default_registry() -> PromptRegistry:
    """The built-in registry covering GitHub kinds and Jira (when its connector lands)."""
    registry = PromptRegistry()

    registry.register(
        PromptTemplate("github-code", _GITHUB_CODE),
        source_type=SourceType.GITHUB,
        kind=DocumentKind.PR,
    )
    registry.register(
        PromptTemplate("github-issue", _GITHUB_ISSUE),
        source_type=SourceType.GITHUB,
        kind=DocumentKind.ISSUE,
    )
    registry.register(
        PromptTemplate("github-readme", _GITHUB_README),
        source_type=SourceType.GITHUB,
        kind=DocumentKind.README,
    )
    registry.register(
        PromptTemplate("github-discussion", _GITHUB_ISSUE),
        source_type=SourceType.GITHUB,
        kind=DocumentKind.DISCUSSION,
    )
    # Per-source default for any other GitHub kind (e.g. raw code files): treat as code.
    registry.register(PromptTemplate("github", _GITHUB_CODE), source_type=SourceType.GITHUB)

    # Jira: registered at the source level so every Jira kind uses task-oriented extraction.
    registry.register(PromptTemplate("jira-task", _JIRA_TASK), source_type=SourceType.JIRA)

    return registry
