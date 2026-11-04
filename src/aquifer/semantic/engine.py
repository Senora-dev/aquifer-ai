"""The LLM-backed semantic indexer.

Given a document, select the right prompt for its (source, kind), ask the inferencer for the
neutral structured metadata, and validate it into a :class:`SemanticMetadata`. The indexer
depends only on the :class:`Inferencer` interface and the prompt registry, so both the model
provider and the prompts are swappable.
"""

from __future__ import annotations

import logging

from aquifer.core.config import SemanticIndexSettings, get_settings
from aquifer.core.errors import log_event
from aquifer.core.interfaces import Inferencer, SemanticIndexer
from aquifer.core.models import (
    Document,
    Entity,
    Relationship,
    RelationshipOrigin,
    SemanticMetadata,
)
from aquifer.semantic.prompts import (
    SEMANTIC_SCHEMA,
    LlmSemanticOutput,
    PromptRegistry,
    default_registry,
)

_LLM = RelationshipOrigin.LLM_EXTRACTED.value


logger = logging.getLogger(__name__)


class LlmSemanticIndexer(SemanticIndexer):
    def __init__(
        self,
        inferencer: Inferencer,
        registry: PromptRegistry | None = None,
        settings: SemanticIndexSettings | None = None,
    ) -> None:
        self.inferencer = inferencer
        self.registry = registry or default_registry()
        self.settings = settings or get_settings().semantic_index

    def extract(self, document: Document) -> SemanticMetadata:
        template = self.registry.select(document.source_type, document.kind)
        user = template.render_user(document, self.settings.max_input_chars)
        data = self.inferencer.infer_json(template.system, user, schema=SEMANTIC_SCHEMA)
        out = LlmSemanticOutput.parse(data)
        if out.dropped:
            log_event(logger, "llm_output_items_dropped", document_id=document.id,
                      dropped=out.dropped)
        # Everything from the model is labelled llm_extracted, whatever it claimed.
        return SemanticMetadata(
            summary=out.summary,
            entities=[Entity(type=e.type, name=e.name, origin=_LLM) for e in out.entities],
            topics=out.topics,
            relationships=[
                Relationship(type=r.type, target=r.target, source=r.source or None,
                             description=r.description, origin=_LLM,
                             assertion=(r.assertion or "present").strip().lower(),
                             quote=r.quote or None)
                for r in out.relationships
            ],
        )
