"""Error taxonomy, bounded retries, and structured operational logging.

Ingestion distinguishes three failure classes so nothing is silently swallowed and nothing is
retried forever:

- :class:`TransientError` — worth retrying (throttling, timeouts, 5xx). Retried in-process with
  bounded exponential backoff; if still failing, it propagates so SQS redelivers the job and,
  after ``maxReceiveCount``, moves it to the DLQ.
- :class:`RateLimitedError` — the source asked us to wait longer than an in-process sleep is
  sensible (GitHub's hourly reset). The worker re-enqueues the job with a delay instead of
  burning SQS receives.
- Everything else is permanent for that unit of work: logged with its document id and counted,
  and processing of the rest of the page continues.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """A failure that is expected to succeed on retry."""


class RateLimitedError(TransientError):
    """The source is rate limiting us; ``retry_after`` is the suggested wait in seconds."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceAuthError(Exception):
    """The source rejected our credentials (permanent until an operator fixes them)."""


class SourceNotFoundError(Exception):
    """The requested source object does not exist or is not visible to our credentials."""


_TRANSIENT_AWS_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "InternalFailure",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
    }
)


def is_transient(exc: BaseException) -> bool:
    """Classify an exception from any adapter as retryable or not.

    Uses duck typing so the core never imports boto3 / opensearch-py / httpx.
    """
    if isinstance(exc, TransientError):
        return True
    # botocore ClientError
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code in _TRANSIENT_AWS_CODES:
            return True
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
    name = type(exc).__name__
    # botocore network errors, opensearch-py ConnectionError/ConnectionTimeout, httpx transport
    # errors, and Python's own timeouts.
    if name in {
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ConnectionError",
        "ConnectionTimeout",
        "TimeoutException",
        "ReadTimeout",
        "ConnectTimeout",
        "RemoteProtocolError",
        "TimeoutError",
    }:
        return True
    # opensearch-py TransportError carries a numeric status_code.
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and (status_code == 429 or status_code >= 500)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retryable: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] = time.sleep,
    operation: str = "call",
    logger: logging.Logger | None = None,
) -> T:
    """Call ``fn`` with bounded exponential backoff (full jitter) on retryable errors."""
    log = logger or logging.getLogger(__name__)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                delay = min(max_delay, float(retry_after))
            else:
                delay = random.uniform(0, delay)
            log_event(
                log,
                "retry",
                level=logging.WARNING,
                operation=operation,
                attempt=attempt,
                delay_s=round(delay, 3),
                error=type(exc).__name__,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields) -> None:
    """Emit one structured (JSON) operational log line.

    Callers pass identifiers and counts only — never document bodies, queries, or credentials.
    """
    payload = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    logger.log(level, json.dumps(payload, default=str, sort_keys=True))


def configure_lambda_logging(level: str) -> None:
    """Make INFO-level operational events visible in Lambda.

    The Lambda Python runtime installs its own handler but leaves the root logger at WARNING,
    which would silently drop ``job_processed`` and the other INFO events.
    """
    logging.getLogger().setLevel(level.upper())
    # Request-level chatter from HTTP clients is not operational signal.
    for noisy in ("httpx", "httpcore", "urllib3", "botocore", "opensearch"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
