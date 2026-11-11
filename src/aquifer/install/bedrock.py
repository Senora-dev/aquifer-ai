"""Bedrock model checks shared by ``aquifer install aws`` and ``deploy/aws/aquifer-aws``.

    python -m aquifer.install.bedrock detect      # prints the extraction model to use (or "")

Embeddings (Titan Text Embeddings v2) are required: the server embeds every chunk with it.
A Claude model is optional: it powers LLM extraction and the UI's grounded answers; without
one, deterministic extraction and source-backed search still work.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


@dataclass(frozen=True)
class ModelCheck:
    model: str
    ok: bool
    code: str = ""
    message: str = ""


def _explain(model: str, exc: Exception) -> ModelCheck:
    from aquifer.semantic.failures import classify_extraction_error

    f = classify_extraction_error(exc)
    return ModelCheck(model, False, f.code, f.message)


def check_embedding(runtime, model: str = EMBEDDING_MODEL) -> ModelCheck:  # noqa: ANN001
    try:
        runtime.invoke_model(modelId=model, body=json.dumps({"inputText": "aquifer check",
                                                             "dimensions": 256}))
    except Exception as exc:  # noqa: BLE001 - classified into an actionable message
        return _explain(model, exc)
    return ModelCheck(model, True)


def check_chat(runtime, model: str) -> ModelCheck:  # noqa: ANN001
    try:
        runtime.converse(modelId=model,
                         messages=[{"role": "user", "content": [{"text": "Reply OK"}]}],
                         inferenceConfig={"maxTokens": 5})
    except Exception as exc:  # noqa: BLE001
        return _explain(model, exc)
    return ModelCheck(model, True)


def _version(model_id: str) -> list[int]:
    name = re.sub(r"-\d{8}.*$|-v\d+(:\d+)?$", "", model_id.split("anthropic.", 1)[1])
    return [int(x) for x in re.findall(r"\d+", name)]


def _rank(model_id: str):  # noqa: ANN202
    family = 0 if "haiku" in model_id else 1 if "sonnet" in model_id else 2
    v = _version(model_id)
    return (model_id.startswith("global."), family, not v, [-x for x in (v + [0, 0, 0])[:3]])


def candidates(control) -> list[str]:  # noqa: ANN001
    """Active regional Claude inference profiles, best first: Haiku (a third of Sonnet's price
    per document, see docs/cost.md), newest first; global profiles last."""
    out = []
    for p in control.list_inference_profiles(typeEquals="SYSTEM_DEFINED")[
            "inferenceProfileSummaries"]:
        pid = p["inferenceProfileId"]
        if p["status"] != "ACTIVE" or "anthropic.claude" not in pid:
            continue
        v = _version(pid)
        if "haiku" in pid and v[:1] and v[0] < 4:
            continue  # older Haiku models extract relationships noticeably worse
        out.append(pid)
    return sorted(out, key=_rank)


def detect_extraction_model(control, runtime,  # noqa: ANN001
                            log: Callable[[str], None] = lambda _m: None) -> str:
    """The first Claude model this account can invoke, or "" if none."""
    for model in candidates(control):
        result = check_chat(runtime, model)
        if result.ok:
            return model
        log(f"skip {model}: {result.code}")
    return ""


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = argv if argv is not None else sys.argv[1:]
    if args[:1] != ["detect"]:
        print("usage: python -m aquifer.install.bedrock detect", file=sys.stderr)
        return 2
    model = detect_extraction_model(boto3.client("bedrock"), boto3.client("bedrock-runtime"),
                                    log=lambda m: print(f"  {m}", file=sys.stderr))
    print(model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
