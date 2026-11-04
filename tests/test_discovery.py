"""Discovery Lambda: credential check, watermark overlap/safety, out-of-scope purge."""

from datetime import UTC, datetime, timedelta

import pytest

from aquifer.core.config import GitHubSettings, IngestionSettings, Settings, VectorStoreSettings
from aquifer.core.errors import SourceAuthError
from aquifer.core.models import Chunk, DocumentKind, SourceType
from aquifer.ingestion import aws, discovery_handler
from aquifer.vectorstore.memory import InMemoryVectorStore


class FakeConnector:
    def __init__(self, fail_auth=False):
        self.fail_auth = fail_auth
        self.sources = []

    def validate_access(self):
        if self.fail_auth:
            raise SourceAuthError("GitHub rejected the token (401)")

    def discover(self, source):
        self.sources.append(source)
        return ["job1", "job2"]


@pytest.fixture
def env(monkeypatch):
    settings = Settings(
        github=GitHubSettings(repo_allowlist=["o/r"]),
        ingestion=IngestionSettings(queue_url="q", state_bucket="b", watermark_overlap_s=300),
    )
    monkeypatch.setattr(discovery_handler, "get_settings", lambda: settings)
    state = {"enqueued": [], "watermark": None, "connector": FakeConnector()}
    monkeypatch.setattr(discovery_handler, "build_connector",
                        lambda *a, **k: state["connector"])
    monkeypatch.setattr(aws, "enqueue_job", lambda url, job, **k: state["enqueued"].append(job))
    monkeypatch.setattr(aws, "read_watermark", lambda b, k: state.get("previous"))
    monkeypatch.setattr(aws, "write_watermark",
                        lambda b, k, since: state.__setitem__("watermark", since))
    return state


def test_watermark_is_rewound_by_the_overlap(env):
    previous = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    env["previous"] = previous
    discovery_handler.handler()
    since = env["connector"].sources[0].config["since"]
    assert since == previous - timedelta(seconds=300)
    assert env["enqueued"] == ["job1", "job2"]
    assert env["watermark"] is not None


def test_bad_credentials_fail_the_run_without_advancing_the_watermark(env):
    env["connector"] = FakeConnector(fail_auth=True)
    with pytest.raises(SourceAuthError):
        discovery_handler.handler()
    assert env["enqueued"] == [] and env["watermark"] is None


def test_read_watermark_only_treats_missing_object_as_never_ran():
    class NoKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    class Client:
        def __init__(self, exc):
            self.exc = exc

        def get_object(self, **kw):
            raise self.exc

    assert aws.read_watermark("b", "k", client=Client(NoKey())) is None
    with pytest.raises(Denied):  # must not silently trigger a full backfill
        aws.read_watermark("b", "k", client=Client(Denied()))


def _chunk(repo):
    return Chunk(chunk_id=f"{repo}#0", document_id=f"github:{repo}:issue:1", index=0, text="t",
                 embedding=[1.0], source_type=SourceType.GITHUB, kind=DocumentKind.ISSUE,
                 repo=repo)


def test_purge_removes_repos_no_longer_allowlisted():
    store = InMemoryVectorStore()
    store.upsert([_chunk("o/r"), _chunk("o/removed")])
    settings = Settings(github=GitHubSettings(repo_allowlist=["o/r"]),
                        vector_store=VectorStoreSettings(endpoint="x"))
    assert discovery_handler.purge_unlisted_repos(settings, store) == 1
    assert store.facet("repo") == [("o/r", 1)]


def test_purge_never_runs_with_an_empty_allowlist():
    store = InMemoryVectorStore()
    store.upsert([_chunk("o/r")])
    assert discovery_handler.purge_unlisted_repos(Settings(), store) == 0
    assert len(store) == 1


def test_wildcard_allowlist_entries_are_rejected():
    with pytest.raises(ValueError, match="no wildcards"):
        GitHubSettings(repo_allowlist=["myorg/*"])


def test_enqueue_delay_is_capped_at_sqs_maximum():
    sent = {}

    class Sqs:
        def send_message(self, **kw):
            sent.update(kw)

    from aquifer.core.models import FetchJob

    job = FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r",
                   kind=DocumentKind.ISSUE)
    aws.enqueue_job("q", job, delay_seconds=5000, client=Sqs())
    assert sent["DelaySeconds"] == 900


def test_get_secret_strips_trailing_newline():
    class Sm:
        def get_secret_value(self, SecretId):
            return {"SecretString": "token-value\n"}

    assert aws.get_secret("arn", client=Sm()) == "token-value"


def test_watermark_from_an_older_index_format_forces_one_full_sync():
    import io
    import json as _json

    from aquifer.core.models import INDEX_FORMAT_VERSION

    class S3:
        def __init__(self):
            self.body = None

        def put_object(self, **kw):
            self.body = kw["Body"]

        def get_object(self, **kw):
            return {"Body": io.BytesIO(self.body)}

    s3 = S3()
    since = datetime(2024, 1, 1, tzinfo=UTC)
    aws.write_watermark("b", "k", since, client=s3)
    assert _json.loads(s3.body)["index_format"] == INDEX_FORMAT_VERSION
    assert aws.read_watermark("b", "k", client=s3) == since
    s3.body = _json.dumps({"since": since.isoformat()}).encode()  # written by an older version
    assert aws.read_watermark("b", "k", client=s3) is None
