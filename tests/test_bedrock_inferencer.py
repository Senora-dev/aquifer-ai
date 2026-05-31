import pytest

from aquifer.core.config import SemanticIndexSettings
from aquifer.semantic.bedrock_inferencer import BedrockInferencer, extract_json


class FakeConverseClient:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def converse(self, *, modelId, system, messages, inferenceConfig):  # noqa: N803
        self.calls.append(
            {"modelId": modelId, "system": system, "messages": messages, "cfg": inferenceConfig}
        )
        return {"output": {"message": {"content": [{"text": self._text}]}}}


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced_or_prose():
    text = "Here you go:\n```json\n{\"a\": 1, \"b\": [2,3]}\n```\nDone."
    assert extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_infer_json_calls_converse_and_parses():
    client = FakeConverseClient('{"summary": "ok", "entities": ["svc"]}')
    settings = SemanticIndexSettings(model_id="anthropic.claude-x", max_tokens=256, temperature=0.0)
    inf = BedrockInferencer(settings=settings, client=client)

    out = inf.infer_json("system text", "user text")

    assert out == {"summary": "ok", "entities": ["svc"]}
    call = client.calls[0]
    assert call["modelId"] == "anthropic.claude-x"
    assert call["system"] == [{"text": "system text"}]
    assert call["messages"][0]["content"][0]["text"] == "user text"
    assert call["cfg"]["maxTokens"] == 256
