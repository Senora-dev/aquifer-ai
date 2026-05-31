"""Amazon Bedrock embedding adapter.

Implements :class:`aquifer.core.interfaces.Embedder` using Titan Text Embeddings v2 over a
Bedrock runtime client. In the deployed stack the client reaches Bedrock through a VPC
(PrivateLink) endpoint, so embeddings never leave the VPC.

Titan embeds a single text per ``invoke_model`` call, so batching is done client-side. The
boto3 client is created lazily and can be injected for testing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from aquifer.core.config import EmbeddingSettings, get_settings
from aquifer.core.interfaces import Embedder


class BedrockEmbedder(Embedder):
    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings().embedding
        self.dimensions = self._settings.dimensions
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily construct the ``bedrock-runtime`` client (kept out of import time)."""
        if self._client is None:
            import boto3  # imported lazily so core stays free of AWS deps

            self._client = boto3.client("bedrock-runtime", region_name=self._settings.region)
        return self._client

    def _embed_one(self, text: str) -> list[float]:
        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self._settings.dimensions,
                "normalize": True,
            }
        )
        response = self.client.invoke_model(
            modelId=self._settings.model_id,
            accept="application/json",
            contentType="application/json",
            body=body,
        )
        payload = json.loads(response["body"].read())
        return payload["embedding"]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        # Titan is single-input; loop client-side. Order is preserved by construction.
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)
