"""Classify extraction failures into stable codes, retry classes and actionable messages.

``kind`` drives the retry policy:

- ``transient`` — throttling, timeouts, 5xx: retried with exponential backoff.
- ``configuration`` — model access, Marketplace subscription, IAM, unknown model: retried with
  the same backoff (an operator fix is picked up automatically) and reported with what to do.
- ``content`` — the input or the model output for *this* document is the problem: retried a
  few times, then the document is marked ``failed`` and not retried until its content changes.

Messages are written for operators and never include credentials (Bedrock error messages do
not contain any; we still truncate them).
"""

from __future__ import annotations

from dataclasses import dataclass

from aquifer.core.errors import is_transient


@dataclass(frozen=True)
class ExtractionFailure:
    code: str
    kind: str  # transient | configuration | content
    message: str


def _aws(exc: BaseException) -> tuple[str | None, str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error") or {}
        return err.get("Code"), str(err.get("Message") or "")
    return None, str(exc)


_CREDENTIAL_ERRORS = frozenset({
    "TokenRetrievalError", "NoCredentialsError", "UnauthorizedSSOTokenError",
    "SSOTokenLoadError", "CredentialRetrievalError", "PartialCredentialsError",
})


def classify_extraction_error(exc: BaseException) -> ExtractionFailure:
    code, message = _aws(exc)
    text = f"{message} {exc}".lower()
    if type(exc).__name__ in _CREDENTIAL_ERRORS or code in {
        "ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException",
        "InvalidClientTokenId",
    }:
        return ExtractionFailure(
            "aws_credentials_unavailable", "configuration",
            "AWS credentials are missing or expired (e.g. an SSO session ended). Renew them "
            "(aws sso login, or the role's credentials); pending documents retry automatically.")
    if code == "AccessDeniedException" and "marketplace" in text:
        return ExtractionFailure(
            "bedrock_marketplace_subscription_required", "configuration",
            "Bedrock: this model needs a one-time AWS Marketplace subscription for the account. "
            "Invoke it once (e.g. Bedrock console > Playground) with a principal allowed "
            "aws-marketplace:Subscribe / ViewSubscriptions; retries then succeed automatically.")
    if code == "ResourceNotFoundException" and "use case" in text:
        return ExtractionFailure(
            "bedrock_use_case_form_required", "configuration",
            "Bedrock: submit the Anthropic use case details form (Bedrock console > Model "
            "catalog > an Anthropic model). Activation can take ~15 minutes.")
    if code == "AccessDeniedException" and "not available for this account" in text:
        return ExtractionFailure(
            "bedrock_model_not_available", "configuration",
            "Bedrock: the configured extraction model is not available to this account. Choose "
            "another model (AQUIFER_SEMANTIC_INDEX__MODEL_ID / extraction_model_id).")
    if code == "AccessDeniedException":
        return ExtractionFailure(
            "bedrock_access_denied", "configuration",
            "Bedrock denied the call: check model access in the Bedrock console and that the "
            "role may bedrock:InvokeModel this model or inference profile.")
    if code == "ResourceNotFoundException":
        return ExtractionFailure(
            "bedrock_model_not_found", "configuration",
            "Bedrock could not find the extraction model in this region (retired or wrong id). "
            "Run ./aquifer-test check or scripts/preflight.py and set extraction_model_id.")
    if code == "ValidationException" and ("model identifier" in text or "model id" in text):
        return ExtractionFailure(
            "bedrock_invalid_model", "configuration",
            "Bedrock does not recognize the extraction model id in this region.")
    if is_transient(exc):
        return ExtractionFailure("transient_error", "transient",
                                 f"Transient error ({code or type(exc).__name__}); will retry.")
    if code == "ValidationException":
        return ExtractionFailure("bedrock_rejected_input", "content",
                                 f"Bedrock rejected this document's input: {message[:200]}")
    name = type(exc).__name__
    if name in {"ValueError", "ValidationError", "JSONDecodeError"}:
        return ExtractionFailure("unparseable_model_output", "content",
                                 "The model output could not be parsed as the expected JSON.")
    return ExtractionFailure("unexpected_error", "content", f"Unexpected error: {name}")
