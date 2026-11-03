"""Worker Lambda routing test: parse SQS records, process, re-enqueue successors."""

from aquifer.core.models import DocumentKind, FetchJob, SourceType
from aquifer.ingestion import worker_handler


class FakePipeline:
    def __init__(self, successor=None):
        self._successor = successor
        self.processed = []

    def process_job(self, job):
        self.processed.append(job)
        return self._successor


def test_handler_processes_records_and_enqueues_successor(monkeypatch):
    successor = FetchJob(
        source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
        kind=DocumentKind.ISSUE, cursor="2",
    )
    fake = FakePipeline(successor=successor)
    enqueued = []

    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, settings: fake)
    monkeypatch.setattr(
        worker_handler.aws, "enqueue_job",
        lambda queue_url, job, **kw: enqueued.append(job),
    )

    job = FetchJob(
        source_id="gh", source_type=SourceType.GITHUB, repo="o/r", kind=DocumentKind.ISSUE,
    )
    event = {"Records": [{"body": job.model_dump_json()}]}

    result = worker_handler.handler(event)

    assert result == {"processed": 1, "requeued": 0}
    assert fake.processed[0].repo == "o/r"
    assert enqueued == [successor]


def test_handler_no_successor_does_not_enqueue(monkeypatch):
    fake = FakePipeline(successor=None)
    enqueued = []
    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, settings: fake)
    monkeypatch.setattr(
        worker_handler.aws, "enqueue_job",
        lambda queue_url, job, **kw: enqueued.append(job),
    )

    job = FetchJob(
        source_id="gh", source_type=SourceType.GITHUB, repo="o/r", kind=DocumentKind.README,
    )
    worker_handler.handler({"Records": [{"body": job.model_dump_json()}]})
    assert enqueued == []


def test_rate_limited_job_is_requeued_with_delay(monkeypatch):
    from aquifer.core.errors import RateLimitedError

    class Limited:
        def process_job(self, job):
            raise RateLimitedError("limit", retry_after=3600)

    sent = []
    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, settings: Limited())
    monkeypatch.setattr(
        worker_handler.aws, "enqueue_job",
        lambda queue_url, job, **kw: sent.append((job, kw)),
    )
    job = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
                   kind=DocumentKind.ISSUE, cursor="4")
    result = worker_handler.handler({"Records": [{"body": job.model_dump_json()}]})

    assert result == {"processed": 0, "requeued": 1}
    requeued, kw = sent[0]
    assert requeued.cursor == "4" and requeued.attempt == 1
    assert kw["delay_seconds"] == 900  # SQS maximum


def test_rate_limit_requeues_are_bounded(monkeypatch):
    import pytest

    from aquifer.core.errors import RateLimitedError

    class Limited:
        def process_job(self, job):
            raise RateLimitedError("limit", retry_after=60)

    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, settings: Limited())
    monkeypatch.setattr(worker_handler.aws, "enqueue_job", lambda *a, **k: None)
    job = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
                   kind=DocumentKind.ISSUE, attempt=99)
    with pytest.raises(RateLimitedError):  # → SQS retry → DLQ
        worker_handler.handler({"Records": [{"body": job.model_dump_json()}]})


def test_other_errors_propagate_for_sqs_retry(monkeypatch):
    import pytest

    class Broken:
        def process_job(self, job):
            raise RuntimeError("opensearch down")

    monkeypatch.setattr(worker_handler, "_pipeline_for", lambda st, settings: Broken())
    job = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
                   kind=DocumentKind.ISSUE)
    with pytest.raises(RuntimeError):
        worker_handler.handler({"Records": [{"body": job.model_dump_json()}]})
