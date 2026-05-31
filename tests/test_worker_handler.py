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

    assert result == {"processed": 1}
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
