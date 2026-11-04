"""Single-server runtime: durable job queue, scheduler, workers and the `aquifer` CLI."""

import json
from datetime import UTC, datetime

import pytest

from aquifer.core.config import (
    EmbeddingSettings,
    GitHubSettings,
    IngestionSettings,
    NodeSettings,
    SemanticIndexSettings,
    Settings,
    VectorStoreSettings,
)
from aquifer.core.errors import RateLimitedError
from aquifer.core.interfaces import Connector
from aquifer.core.models import INDEX_FORMAT_VERSION, Document, DocumentKind, FetchJob, SourceType
from aquifer.ingestion import discovery_handler, factory, worker_handler
from aquifer.ingestion.runtime import set_runtime
from aquifer.node import cli
from aquifer.node.queue import JobQueue, NodeRuntime
from aquifer.node.service import NodeService


@pytest.fixture(autouse=True)
def _reset():
    factory._memory_stores.clear()
    worker_handler._invalidate_credentials()
    yield
    set_runtime(None)
    factory._memory_stores.clear()
    worker_handler._invalidate_credentials()


# --- queue ------------------------------------------------------------------------------

def test_claim_complete_and_dedupe(tmp_path):
    q = JobQueue(tmp_path / "q.db")
    assert q.put("discover", {}, dedupe_key="discover")
    assert not q.put("discover", {}, dedupe_key="discover")  # already queued
    job = q.claim()
    assert job.kind == "discover" and job.attempts == 1
    assert q.claim() is None
    assert not q.put("discover", {}, dedupe_key="discover")  # still running
    q.complete(job)
    assert q.put("discover", {}, dedupe_key="discover")


def test_failures_back_off_then_dead_letter(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("aquifer.node.queue.time.time", lambda: clock[0])
    q = JobQueue(tmp_path / "q.db", max_attempts=3, retry_base_s=10, retry_max_s=100)
    q.put("fetch", {"repo": "o/r"})
    delays = []
    for _ in range(3):
        job = q.claim()
        assert job is not None
        action = q.fail(job, "TransientError: 502")
        row = q._conn.execute("SELECT available_at FROM jobs").fetchone()[0]
        delays.append(row - clock[0])
        clock[0] = row  # jump to when it is due
    assert action == "dead_letter"
    assert delays[:2] == [10, 20]
    assert q.claim() is None
    dead = q.dead_letters()
    assert dead[0]["attempts"] == 3 and "502" in dead[0]["error"]
    assert q.requeue_dead() == 1
    assert q.claim().attempts == 1  # fresh budget


def test_interrupted_job_is_redelivered(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("aquifer.node.queue.time.time", lambda: clock[0])
    q = JobQueue(tmp_path / "q.db", lease_s=60)
    q.put("fetch", {})
    assert q.claim() is not None  # the worker dies here
    assert q.claim() is None
    clock[0] += 61  # lease expired
    assert q.claim().attempts == 2
    # After a restart, running jobs are claimable immediately.
    assert q.recover_interrupted() == 1
    assert q.claim() is not None


def test_postpone_does_not_count_as_a_failure(tmp_path):
    q = JobQueue(tmp_path / "q.db", max_attempts=1)
    q.put("fetch", {"attempt": 0})
    job = q.claim()
    q.postpone(job, 0, {"attempt": 1})
    again = q.claim()
    assert again.attempts == 1 and again.payload == {"attempt": 1}


def test_node_watermarks_follow_the_index_format(tmp_path):
    rt = NodeRuntime(JobQueue(tmp_path / "q.db"))
    t = datetime(2024, 5, 1, tzinfo=UTC)
    rt.write_watermark("github.json", t)
    assert rt.read_watermark("github.json") == t
    rt.queue.set_state("watermark:github.json", {"since": t.isoformat(), "index_format": "0"})
    assert INDEX_FORMAT_VERSION != "0" and rt.read_watermark("github.json") is None


# --- service: discovery → fetch → index, end to end -------------------------------------

class FakeGitHub(Connector):
    source_type = "github"

    def __init__(self):
        self.pages = {None: ([self._doc(1), self._doc(2)], "p2"), "p2": ([self._doc(3)], None)}
        self.rate_limit_once = False
        self.broken = False

    @staticmethod
    def _doc(n):
        return Document(
            id=Document.make_id(SourceType.GITHUB, "o/r", DocumentKind.ISSUE, str(n)),
            source_type=SourceType.GITHUB, external_id=str(n), kind=DocumentKind.ISSUE,
            repo="o/r", title=f"issue {n}", body=f"billing-api depends on service-{n}",
            updated_at=datetime(2024, 1, n, tzinfo=UTC))

    def validate_access(self):
        return None

    def discover(self, source):
        yield FetchJob(source_id="github", source_type=SourceType.GITHUB, repo="o/r",
                       kind=DocumentKind.ISSUE)

    def fetch(self, job):
        if self.broken:
            raise RuntimeError("upstream exploded")
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise RateLimitedError("slow down", retry_after=0)
        docs, nxt = self.pages[job.cursor]
        return docs, job.model_copy(update={"cursor": nxt}) if nxt else None


@pytest.fixture
def node(tmp_path, monkeypatch):
    settings = Settings(
        github=GitHubSettings(repo_allowlist=["o/r"]),
        vector_store=VectorStoreSettings(backend="sqlite", sqlite_path=str(tmp_path / "i.db")),
        embedding=EmbeddingSettings(provider="hashing", dimensions=64),
        semantic_index=SemanticIndexSettings(enabled=False),
        ingestion=IngestionSettings(),
        node=NodeSettings(data_dir=str(tmp_path), job_max_attempts=2, job_retry_base_s=0),
    )
    fake = FakeGitHub()
    monkeypatch.setattr(discovery_handler, "get_settings", lambda: settings)
    monkeypatch.setattr(discovery_handler, "build_connector", lambda *a, **k: fake)
    monkeypatch.setattr(worker_handler, "build_connector", lambda *a, **k: fake)
    service = NodeService(settings)
    return service, fake, settings


def test_scheduled_sync_ingests_every_page(node):
    service, _, settings = node
    assert service.schedule_due() == ["discover"]
    assert service.schedule_due() == []  # not due again yet
    service.drain()
    store = factory.build_vector_store(settings)
    assert store.stats()["documents"] == 3
    assert service.queue.get_state("discovery:last_success")["enqueued"] == 1
    # A second run indexes nothing new (idempotent) and leaves no jobs behind.
    service.queue.put("discover", {"full": True}, dedupe_key="discover")
    service.drain()
    assert service.queue.counts() == {}
    assert store.stats()["documents"] == 3


def test_rate_limited_page_is_postponed_not_failed(node):
    service, fake, settings = node
    fake.rate_limit_once = True
    service.schedule_due()
    service.drain()
    assert service.queue.counts() == {"pending": {"fetch": 1}}  # waiting for the source
    import time
    time.sleep(1.1)
    service.drain()
    assert factory.build_vector_store(settings).stats()["documents"] == 3
    assert service.queue.dead_letters() == []


def test_persistent_failure_ends_in_dead_letters_and_status(node, capsys, monkeypatch):
    service, fake, settings = node
    fake.broken = True
    service.schedule_due()
    service.drain()
    dead = service.queue.dead_letters()
    assert len(dead) == 1 and "upstream exploded" in dead[0]["error"]
    status = cli.collect_status(settings)
    assert status["queue"]["dead"] == {"fetch": 1}
    # Recovery after the cause is fixed.
    fake.broken = False
    service.queue.requeue_dead()
    service.drain()
    assert factory.build_vector_store(settings).stats()["documents"] == 3


def test_failed_discovery_is_reported(node, monkeypatch):
    service, fake, settings = node

    def bad_access():
        from aquifer.core.errors import SourceAuthError
        raise SourceAuthError("GitHub token rejected (401)")

    fake.validate_access = bad_access
    service.schedule_due()
    service.drain()
    err = service.queue.get_state("discovery:last_error")
    assert "401" in err["error"]


def test_background_threads_process_jobs(node):
    service, _, settings = node
    service.start()
    try:
        import time
        deadline = time.time() + 20
        while time.time() < deadline:
            if factory.build_vector_store(settings).stats()["documents"] == 3:
                break
            time.sleep(0.1)
    finally:
        service.stop()
    assert factory.build_vector_store(settings).stats()["documents"] == 3


# --- CLI ---------------------------------------------------------------------------------

def test_cli_env_file_and_commands(tmp_path, monkeypatch, capsys):
    env = tmp_path / "aquifer.env"
    env.write_text(f"AQUIFER_NODE__DATA_DIR={tmp_path}\n"
                   f"AQUIFER_VECTOR_STORE__BACKEND=sqlite\n"
                   f"AQUIFER_VECTOR_STORE__SQLITE_PATH={tmp_path / 'i.db'}\n"
                   f"AQUIFER_MCP__AUTH_TOKENS_REF=file:{tmp_path / 'tokens.json'}\n")
    for k in list(__import__("os").environ):
        if k.startswith("AQUIFER_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("AQUIFER_ENV_FILE", str(env))
    for argv in (["init"], ["init"], ["sync", "--full"], ["sync"], ["status", "--json"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 0
    out = capsys.readouterr()
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert len(tokens["default"]) >= 32
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    assert tokens["default"] not in out.out + out.err  # never printed
    assert "already queued" in out.err
    assert json.loads(out.out)["queue"] == {"pending": {"discover": 1}}


def test_health_endpoint_needs_no_token():
    import asyncio

    sent = []

    async def app(scope, receive, send):  # pragma: no cover - must not be reached
        raise AssertionError

    async def send(msg):
        sent.append(msg)

    asyncio.run(cli.health_app(app)({"type": "http", "path": "/healthz"}, None, send))
    assert sent[0]["status"] == 200 and b"ok" in sent[1]["body"]
