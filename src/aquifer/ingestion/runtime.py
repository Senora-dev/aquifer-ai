"""Where ingestion jobs, watermarks and credentials live.

Discovery and the worker are the same code in both deployment profiles; only the runtime
differs:

- :class:`AwsRuntime` (scale profile): SQS queue, S3 watermarks, Secrets Manager.
- ``aquifer.node.queue.NodeRuntime`` (single-server profile): a SQLite job queue and state on
  local disk, secrets from SSM Parameter Store, a file or the environment.

The active runtime is process-wide. Lambda handlers use the AWS default; ``aquifer serve``
installs the node runtime at startup.
"""

from __future__ import annotations

from datetime import datetime

from aquifer.core.models import FetchJob
from aquifer.core.secrets import resolve_secret


class Runtime:
    def enqueue(self, job: FetchJob, *, delay_seconds: int = 0) -> None:
        raise NotImplementedError

    def read_watermark(self, key: str) -> datetime | None:
        raise NotImplementedError

    def write_watermark(self, key: str, since: datetime) -> None:
        raise NotImplementedError

    def get_secret(self, ref: str) -> str:
        return resolve_secret(ref)


class AwsRuntime(Runtime):
    """SQS + S3 + Secrets Manager. Looks the helpers up at call time (tests patch them)."""

    def enqueue(self, job: FetchJob, *, delay_seconds: int = 0) -> None:
        from aquifer.core.config import get_settings
        from aquifer.ingestion import aws

        if delay_seconds:
            aws.enqueue_job(get_settings().ingestion.queue_url, job, delay_seconds=delay_seconds)
        else:
            aws.enqueue_job(get_settings().ingestion.queue_url, job)

    def read_watermark(self, key: str) -> datetime | None:
        from aquifer.core.config import get_settings
        from aquifer.ingestion import aws

        return aws.read_watermark(get_settings().ingestion.state_bucket, key)

    def write_watermark(self, key: str, since: datetime) -> None:
        from aquifer.core.config import get_settings
        from aquifer.ingestion import aws

        aws.write_watermark(get_settings().ingestion.state_bucket, key, since)

    def get_secret(self, ref: str) -> str:
        scheme = ref.partition(":")[0]
        if scheme in {"ssm", "file", "env"}:
            return resolve_secret(ref)
        from aquifer.ingestion import aws

        return aws.get_secret(ref)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    return _runtime or AwsRuntime()


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime
