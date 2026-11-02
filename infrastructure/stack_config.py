"""Deploy-time configuration for the Aquifer stack, validated before anything is synthesized.

Every value comes from CDK context (``cdk deploy -c key=value``). Invalid input fails ``cdk synth``
with a message that says what to change, instead of producing a stack that deploys and then
fails at runtime.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field

DEFAULT_INDEX = "aquifer-context"
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
# Override per region/account with -c extraction_model_id=... (run scripts/preflight.py to check
# availability). Inference-profile ids (e.g. "us.anthropic...") are supported.
DEFAULT_EXTRACTION_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_INDEX_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,254}")
_INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "us-gov.", "jp.", "au.", "ca.", "global.")


class ConfigError(ValueError):
    """Invalid deploy-time configuration."""


@dataclass(frozen=True)
class StackConfig:
    repos: list[str]
    index: str = DEFAULT_INDEX
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID
    extraction_model_id: str = DEFAULT_EXTRACTION_MODEL_ID
    semantic_index_enabled: bool = True
    # CIDRs allowed to reach the MCP listener. Empty = the VPC CIDR only.
    mcp_allowed_cidrs: list[str] = field(default_factory=list)
    # ACM certificate for a TLS listener (443). Without it the listener is plain TCP/80 inside
    # the VPC and bearer tokens cross the VPC network unencrypted.
    mcp_certificate_arn: str | None = None
    # ENABLED (production, 2 OCU minimum) or DISABLED (dev/test, 1 OCU minimum).
    aoss_standby_replicas: str = "ENABLED"
    worker_max_concurrency: int = 5
    schedule_minutes: int = 15
    log_retention_days: int = 30


def _as_list(value, name: str) -> list[str]:  # noqa: ANN001
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"-c {name} must be a JSON array: {exc}") from exc
        else:
            value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"-c {name} must be a list of strings")
    return value


def _as_bool(value, default: bool) -> bool:  # noqa: ANN001
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value, name: str, default: int, lo: int, hi: int) -> int:  # noqa: ANN001
    if value is None:
        return default
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"-c {name} must be an integer") from exc
    if not lo <= out <= hi:
        raise ConfigError(f"-c {name} must be between {lo} and {hi} (got {out})")
    return out


def load_config(get) -> StackConfig:  # noqa: ANN001 - get(key) -> context value or None
    repos = _as_list(get("repos"), "repos")
    if not repos:
        raise ConfigError(
            "No repositories configured. Pass the GitHub repositories to ingest, e.g. "
            "cdk deploy -c repos='[\"your-org/your-repo\"]'. Every listed repository forms one "
            "trust domain: any authenticated MCP client can read all of them."
        )
    bad = [r for r in repos if not _REPO_RE.fullmatch(r)]
    if bad:
        raise ConfigError(f"-c repos entries must be exact 'owner/repo' names (no wildcards): "
                          f"{bad}")

    index = get("index") or DEFAULT_INDEX
    if not _INDEX_RE.fullmatch(index):
        raise ConfigError(f"-c index must be a lowercase OpenSearch index name (got {index!r})")

    cidrs = _as_list(get("mcp_allowed_cidrs"), "mcp_allowed_cidrs")
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ConfigError(f"-c mcp_allowed_cidrs: invalid CIDR {cidr!r}") from exc
        if net.prefixlen == 0:
            raise ConfigError("-c mcp_allowed_cidrs must not include 0.0.0.0/0 or ::/0; the "
                              "MCP endpoint is private by design")

    standby = str(get("aoss_standby_replicas") or "ENABLED").upper()
    if standby not in {"ENABLED", "DISABLED"}:
        raise ConfigError("-c aoss_standby_replicas must be ENABLED or DISABLED")

    cert = get("mcp_certificate_arn") or None
    if cert and not str(cert).startswith("arn:"):
        raise ConfigError("-c mcp_certificate_arn must be an ACM certificate ARN")

    return StackConfig(
        repos=repos,
        index=index,
        embedding_model_id=get("model_id") or DEFAULT_EMBEDDING_MODEL_ID,
        extraction_model_id=get("extraction_model_id") or DEFAULT_EXTRACTION_MODEL_ID,
        semantic_index_enabled=_as_bool(get("semantic_index_enabled"), True),
        mcp_allowed_cidrs=cidrs,
        mcp_certificate_arn=cert,
        aoss_standby_replicas=standby,
        worker_max_concurrency=_as_int(get("worker_max_concurrency"), "worker_max_concurrency",
                                       5, 2, 100),
        schedule_minutes=_as_int(get("schedule_minutes"), "schedule_minutes", 15, 5, 1440),
        log_retention_days=_as_int(get("log_retention_days"), "log_retention_days", 30, 1, 3653),
    )


def bedrock_model_arns(model_id: str, region: str, account: str) -> list[str]:
    """IAM resources needed to invoke ``model_id`` (foundation model or inference profile)."""
    if model_id.startswith(_INFERENCE_PROFILE_PREFIXES):
        base = model_id.split(".", 1)[1]
        # A cross-region inference profile routes to the model in several regions.
        return [
            f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
            f"arn:aws:bedrock:*::foundation-model/{base}",
        ]
    return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]
