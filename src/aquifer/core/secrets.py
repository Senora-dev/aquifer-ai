"""Secret references: where a credential is stored, without the credential itself.

Settings hold a *reference*, never the value:

- ``ssm:/aquifer/github-token``: an SSM Parameter Store SecureString. This is the default on the
  single-server AWS profile; standard parameters cost nothing.
- ``file:/run/secrets/github-token``: a file, for Docker or Kubernetes secrets and other hosts.
- ``env:GITHUB_TOKEN``: an environment variable.
- anything else: a Secrets Manager secret id or ARN (the scale profile).

Values are stripped of surrounding whitespace: a trailing newline in a token makes HTTP headers
invalid, and secrets written with ``echo`` or shell pipes often carry one. Values are never
logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class SecretNotFoundError(LookupError):
    """The reference points at nothing (unset variable, missing file or parameter)."""


def _client(service: str) -> Any:
    import boto3

    return boto3.client(service)


def resolve_secret(ref: str, *, client: Any | None = None) -> str:
    """Return the secret value that ``ref`` points to."""
    if not ref:
        raise SecretNotFoundError("empty secret reference")
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        value = os.environ.get(rest)
        if value is None:
            raise SecretNotFoundError(f"environment variable {rest} is not set")
        return value.strip()
    if scheme == "file":
        path = Path(rest)
        if not path.is_file():
            raise SecretNotFoundError(f"secret file {rest} does not exist")
        return path.read_text().strip()
    if scheme == "ssm":
        ssm = client or _client("ssm")
        try:
            resp = ssm.get_parameter(Name=rest, WithDecryption=True)
        except Exception as exc:
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            if code == "ParameterNotFound":
                raise SecretNotFoundError(f"SSM parameter {rest} does not exist") from None
            raise
        return resp["Parameter"]["Value"].strip()
    from aquifer.ingestion import aws

    return aws.get_secret(ref, client=client)


def store_secret(ref: str, value: str, *, client: Any | None = None) -> None:
    """Write a secret to a writable reference (``ssm:`` or ``file:``).

    Used for credentials Aquifer itself renews (OAuth refresh tokens of MCP sources).
    """
    scheme, _, rest = ref.partition(":")
    if scheme == "file":
        path = Path(rest)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        tmp.replace(path)
        return
    if scheme == "ssm":
        ssm = client or _client("ssm")
        ssm.put_parameter(Name=rest, Value=value, Type="SecureString", Overwrite=True)
        return
    raise ValueError(f"secret reference {ref.split(':', 1)[0]!r}: is not writable")


def delete_secret(ref: str, *, client: Any | None = None) -> None:
    scheme, _, rest = ref.partition(":")
    if scheme == "file":
        Path(rest).unlink(missing_ok=True)
    elif scheme == "ssm":
        ssm = client or _client("ssm")
        try:
            ssm.delete_parameter(Name=rest)
        except Exception as exc:
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            if code != "ParameterNotFound":
                raise
