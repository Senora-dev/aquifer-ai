"""Amazon Bedrock implementation of :class:`Inferencer`.

Uses the Bedrock Converse API with a Claude model, reached over the same VPC endpoint as
embeddings. The model is instructed (by the prompts) to return a single JSON object; this client
parses it, tolerating the occasional fenced or prose-wrapped response.
"""

from __future__ import annotations

import json
from typing import Any

from aquifer.core.config import SemanticIndexSettings, get_settings
from aquifer.core.interfaces import Inferencer


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating surrounding prose or code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON object found in model output")


class BedrockInferencer(Inferencer):
    def __init__(
        self,
        settings: SemanticIndexSettings | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings().semantic_index
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._settings.region)
        return self._client

    def infer_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        response = self.client.converse(
            modelId=self._settings.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={
                "maxTokens": self._settings.max_tokens,
                "temperature": self._settings.temperature,
            },
        )
        text = response["output"]["message"]["content"][0]["text"]
        return extract_json(text)
