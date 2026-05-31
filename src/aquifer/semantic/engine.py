"""The LLM-backed semantic indexer.

Given a document, select the right prompt for its (source, kind), ask the inferencer for the
neutral structured metadata, and validate it into a :class:`SemanticMetadata`. The indexer
depends only on the :class:`Inferencer` interface and the prompt registry, so both the model
provider and the prompts are swappable.
"""

from __future__ import annotations

from aquifer.core.config import SemanticIndexSettings, get_settings
from aquifer.core.interfaces import Inferencer, SemanticIndexer
from aquifer.core.models import Document, SemanticMetadata
from aquifer.semantic.prompts import SEMANTIC_SCHEMA, PromptRegistry, default_registry


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
        return SemanticMetadata.model_validate(data)
