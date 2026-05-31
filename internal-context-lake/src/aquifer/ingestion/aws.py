"""Thin AWS helpers for the ingestion Lambdas: SQS, S3 watermarks, Secrets Manager.

Isolated here (and lazily importing boto3) so the pipeline core and connectors stay free of
AWS dependencies and remain unit-testable. Each helper takes its client as an optional argument
to make the rare unit test that touches them trivial to fake.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from aquifer.core.models import FetchJob


def _client(service: str, region: str | None = None) -> Any:
    import boto3

    return boto3.client(service, region_name=region) if region else boto3.client(service)


def get_secret(secret_id: str, *, region: str | None = None, client: Any | None = None) -> str:
    """Fetch a plaintext secret string (e.g. the GitHub token) from Secrets Manager."""
    client = client or _client("secretsmanager", region)
    response = client.get_secret_value(SecretId=secret_id)
    return response["SecretString"]


def enqueue_job(queue_url: str, job: FetchJob, *, client: Any | None = None) -> None:
    """Send a single fetch job onto the ingestion queue."""
    client = client or _client("sqs")
    client.send_message(QueueUrl=queue_url, MessageBody=job.model_dump_json())


def read_watermark(
    bucket: str, key: str, *, client: Any | None = None
) -> datetime | None:
    """Read a source's last-run watermark from S3, or ``None`` if it has never run."""
    client = client or _client("s3")
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    payload = json.loads(obj["Body"].read())
    value = payload.get("since")
    return datetime.fromisoformat(value) if value else None


def write_watermark(
    bucket: str, key: str, since: datetime, *, client: Any | None = None
) -> None:
    """Persist a source's new watermark to S3."""
    client = client or _client("s3")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({"since": since.isoformat()}).encode(),
        ContentType="application/json",
    )
