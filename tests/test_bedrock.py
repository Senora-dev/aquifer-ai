import io
import json

from aquifer.core.config import EmbeddingSettings
from aquifer.embedding.bedrock import BedrockEmbedder


class FakeBedrockClient:
    def __init__(self, vector):
        self._vector = vector
        self.calls = []

    def invoke_model(self, *, modelId, accept, contentType, body):  # noqa: N803 - boto3 kwarg
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        payload = json.dumps({"embedding": self._vector}).encode()
        return {"body": io.BytesIO(payload)}


def _embedder(vector):
    settings = EmbeddingSettings(model_id="amazon.titan-embed-text-v2:0", dimensions=len(vector))
    return BedrockEmbedder(settings=settings, client=FakeBedrockClient(vector)), settings


def test_embed_query_returns_vector_and_sends_params():
    emb, settings = _embedder([0.1, 0.2, 0.3])
    out = emb.embed_query("hello")
    assert out == [0.1, 0.2, 0.3]
    call = emb.client.calls[0]
    assert call["modelId"] == settings.model_id
    assert call["body"]["inputText"] == "hello"
    assert call["body"]["dimensions"] == 3
    assert call["body"]["normalize"] is True


def test_embed_documents_preserves_order_and_count():
    emb, _ = _embedder([1.0, 0.0])
    out = emb.embed_documents(["a", "b", "c"])
    assert len(out) == 3
    assert [c["body"]["inputText"] for c in emb.client.calls] == ["a", "b", "c"]


def test_dimensions_exposed():
    emb, _ = _embedder([0.0] * 8)
    assert emb.dimensions == 8
