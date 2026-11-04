"""MCP Integrator: safety gates, deterministic sync, updates, deletions with evidence, retries,
interrupted runs, scope, bearer and OAuth (including unattended refresh and re-authorization)."""

import asyncio
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from aquifer.core.config import (
    EmbeddingSettings,
    IntegratorSettings,
    NodeSettings,
    SemanticIndexSettings,
    Settings,
    VectorStoreSettings,
)
from aquifer.core.models import DocumentKind
from aquifer.ingestion import factory
from aquifer.ingestion.runtime import set_runtime
from aquifer.integrator import jobs
from aquifer.integrator.client import (
    UnsafeDestination,
    check_destination,
    connect,
    sanitize,
)
from aquifer.integrator.models import (
    AuthConfig,
    DetailSpec,
    FieldMap,
    Pagination,
    Source,
    StreamSpec,
    SyncPlan,
)
from aquifer.integrator.plan import approvable, classify_tool, infer_stream, validate_plan
from aquifer.integrator.status import sources_status
from aquifer.integrator.web import oauth_callback_app
from aquifer.mcp.tools import ContextTools
from aquifer.node.service import NodeService
from tests.integrator_fakes import Data, FakeOAuth, FakeServer

T1, T2, T3 = "2026-01-01T10:00:00Z", "2026-01-02T10:00:00Z", "2026-01-03T10:00:00Z"


@pytest.fixture(autouse=True)
def _isolation():
    def reset():
        factory._memory_stores.clear()
        jobs._pipelines.clear()
        jobs._registry.cache_clear()
    reset()
    yield
    set_runtime(None)
    reset()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        vector_store=VectorStoreSettings(backend="sqlite", sqlite_path=str(tmp_path / "i.db")),
        embedding=EmbeddingSettings(provider="hashing", dimensions=64),
        semantic_index=SemanticIndexSettings(enabled=False),
        node=NodeSettings(data_dir=str(tmp_path), job_max_attempts=3, job_retry_base_s=0),
        integrator=IntegratorSettings(allow_private_networks=True, min_call_interval_s=0,
                                      authorize_timeout_s=20,
                                      overlap_s=0, public_url="http://127.0.0.1:9"),
    )


def _plan(*, enumeration="complete", detail=True, ordered=True, size=2) -> SyncPlan:
    return SyncPlan(streams=[StreamSpec(
        name="items", kind=DocumentKind.TICKET, tool="list_items",
        args={"orderBy": "UPDATED", "direction": "DESC"}, items="items",
        pagination=Pagination(type="cursor", param="after", next="pageInfo.endCursor",
                              has_more="pageInfo.hasNextPage", size_param="perPage", size=size),
        fields=FieldMap(id="id", title="title", url="url", updated_at="updated_at"),
        detail=DetailSpec(tool="get_item", args={"id": "{id}"}, body="body") if detail else None,
        enumeration=enumeration, ordered_by_updated_desc=ordered)])


def _source(url, **kw) -> Source:
    plan = kw.pop("plan", None) or _plan()
    approved = sorted(plan.tools())
    kw.setdefault("state", "active")
    return Source(name=kw.pop("name", "tracker"), url=url, plan=plan, approved_tools=approved,
                  enabled=True, **kw)


def _run(settings, source) -> NodeService:
    reg = jobs.registry_for(settings)
    reg.save(source)
    jobs.register()
    service = NodeService(settings)
    service.queue.put("mcp_sync", {"source": source.name, "full": True})
    service.drain(60)
    return service


def _store(settings):
    return factory.build_vector_store(settings)


def _texts(settings) -> dict[str, str]:
    store = _store(settings)
    out = {}
    for doc_id in store.list_document_ids({"repo": "mcp/tracker"}) or []:
        rows = store.get_by_document(doc_id)
        out[doc_id.rsplit("/", 1)[1]] = rows[0]["text"]
    return out


def _last_run(settings, name="tracker") -> dict:
    return jobs.registry_for(settings).runs(name, 1)[0]


# --- safety gates ---------------------------------------------------------------------------

def test_tool_safety_classes(settings):
    data = Data()
    with FakeServer(data) as srv:
        async def tools():
            async with connect(_source(srv.url), allow_private=True) as conn:
                return {t.name: t for t in await conn.list_tools()}
        by_name = asyncio.run(tools())
    assert classify_tool(by_name["list_items"]) == "read_only"
    assert classify_tool(by_name["delete_item"]) == "write"
    # A write-like name is refused even when the server claims it is read-only.
    assert classify_tool(by_name["remove_everything"]) == "write"
    assert classify_tool(by_name["fetch_raw"]) == "unannotated_read"
    assert not approvable(by_name["fetch_raw"], allow_unannotated=False)
    assert approvable(by_name["fetch_raw"], allow_unannotated=True)
    assert not approvable(by_name["remove_everything"], allow_unannotated=True)


def test_unapproved_and_write_tools_are_never_called(settings):
    data = Data()
    data.add(1, "x", T1)
    with FakeServer(data) as srv:
        source = _source(srv.url)
        source.approved_tools.append("remove_everything")  # tampered config

        async def attempt():
            async with connect(source, allow_private=True) as conn:
                tools = await conn.list_tools()
                problems = validate_plan(source.model_copy(update={"plan": SyncPlan(
                    streams=[_plan().streams[0].model_copy(update={"tool": "remove_everything"})]
                )}), tools)
                with pytest.raises(Exception, match="not approved"):
                    await conn.call("delete_item", {"id": 1})
                return problems
        problems = asyncio.run(attempt())
    assert any("only read-only tools" in p for p in problems)
    assert "delete_item" not in data.calls and "remove_everything" not in data.calls
    assert data.items  # nothing was deleted at the source


@pytest.mark.parametrize("url", ["http://example.com/mcp", "https://127.0.0.1/mcp",
                                 "https://169.254.169.254/latest", "https://10.0.0.5/mcp",
                                 "https://localhost/mcp"])
def test_private_and_plain_http_destinations_are_refused(url):
    with pytest.raises(UnsafeDestination):
        check_destination(url)


def test_remote_descriptions_are_sanitized():
    assert sanitize("ok\x07\x1b[31m\nnext", 100) == "ok  [31m\nnext"
    assert len(sanitize("x" * 1000, 50)) == 50


# --- deterministic sync ---------------------------------------------------------------------

def test_first_import_pages_through_everything_with_provenance(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2), (3, T3)):
        data.add(n, f"billing-api depends on service-{n}", t)
    with FakeServer(data) as srv:
        _run(settings, _source(srv.url))
    texts = _texts(settings)
    assert set(texts) == {"1", "2", "3"} and "service-2" in texts["2"]
    run = _last_run(settings)
    assert run["status"] == "succeeded" and run["stats"]["pages"] == 2
    row = _store(settings).get_by_document(
        "mcp:mcp/tracker:ticket:items/2")[0]
    assert row["url"] == "https://example.test/items/2" and row["updated_at"].startswith(
        "2026-01-02")
    assert row["source_metadata"]["mcp_tool"] == "list_items"
    assert row["source_metadata"]["mcp_server"] == srv.url


def test_imported_content_is_readable_only_while_the_source_is_enabled(settings):
    data = Data()
    data.add(1, "ledger-service depends on payments-db", T1)
    with FakeServer(data) as srv:
        _run(settings, _source(srv.url))
    tools = ContextTools(factory.build_embedder(settings), _store(settings), settings=settings)
    tools.extra_sources = jobs.sources_provider(settings)
    hits = tools.search_context("payments-db")
    assert hits and hits[0]["repo"] == "mcp/tracker"
    doc = tools.get_document(hits[0]["document_id"])
    assert doc["imported_via"]["tool"] == "list_items"
    listed = [s for s in tools.list_sources() if s["source_type"] == "mcp"]
    assert listed[0]["sync_coverage"][0]["enumeration"] == "complete"

    reg = jobs.registry_for(settings)
    src = reg.get("tracker")
    src.enabled = False
    reg.save(src)
    tools.extra_sources = jobs.sources_provider(settings)  # fresh cache
    assert tools.search_context("payments-db") == []
    assert tools.get_document(hits[0]["document_id"]) == {
        "document_id": hits[0]["document_id"], "found": False}


def test_incremental_run_fetches_only_what_changed(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2), (3, T3)):
        data.add(n, f"body {n}", t)
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url))
        assert data.calls["get_item"] == 3
        data.items[1].update(body="body 1 edited", updated_at="2026-01-04T10:00:00Z")
        service.queue.put("mcp_sync", {"source": "tracker"})
        service.drain(60)
    assert data.calls["get_item"] == 4  # one detail call: the changed item only
    assert _texts(settings)["1"].endswith("body 1 edited")
    run = _last_run(settings)
    assert run["stats"]["indexed"] == 1


def test_duplicate_delivery_is_idempotent(settings):
    data = Data()
    data.add(1, "same", T1)
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url))
        before = _store(settings).stats()["chunks"]
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    assert _store(settings).stats()["chunks"] == before
    assert _last_run(settings)["stats"].get("indexed", 0) == 0


def test_rate_limited_page_is_postponed_and_completes(settings, monkeypatch):
    data = Data()
    data.add(1, "a", T1)
    with FakeServer(data) as srv:
        jobs.registry_for(settings).save(_source(srv.url))
        jobs.register()
        service = NodeService(settings)
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        data.rate_limit_next = 1
        service.drain(30)
        assert service.queue.counts() == {"pending": {"mcp_page": 1}}  # waiting, not failed
        service.queue._conn.execute("UPDATE jobs SET available_at = 0")
        service.drain(30)
    assert set(_texts(settings)) == {"1"}
    assert _last_run(settings)["status"] == "succeeded"
    assert service.queue.dead_letters() == []


def test_interrupted_page_job_resumes_after_restart(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2), (3, T3)):
        data.add(n, f"b{n}", t)
    with FakeServer(data) as srv:
        jobs.registry_for(settings).save(_source(srv.url))
        jobs.register()
        service = NodeService(settings)
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        assert service.run_once()          # sync job: enqueues page 1
        assert service.run_once()          # page 1: enqueues page 2
        job = service.queue.claim()        # page 2 claimed... and the process dies
        assert job.kind == "mcp_page"
        restarted = NodeService(settings)  # new process
        assert restarted.queue.recover_interrupted() == 1
        restarted.drain(60)
    assert set(_texts(settings)) == {"1", "2", "3"}
    assert _last_run(settings)["status"] == "succeeded"


def test_server_dropping_a_planned_tool_stops_the_run_with_an_action(settings):
    data = Data(drop_tool="get_item")
    data.add(1, "a", T1)
    with FakeServer(data) as srv:
        _run(settings, _source(srv.url))
    run = _last_run(settings)
    assert run["status"] == "failed" and "plan_invalid" in run["error"]
    status = sources_status(settings)[0]
    assert status["state"] == "error" and "aquifer sources" in status["action"]


# --- deletions ------------------------------------------------------------------------------

def test_deletion_requires_a_complete_listing_and_confirmation(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2), (3, T3)):
        data.add(n, f"b{n}", t)
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url))
        del data.items[2]
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    assert set(_texts(settings)) == {"1", "3"}
    assert _last_run(settings)["stats"]["deleted"] == 1


def test_item_missing_from_listing_but_still_existing_is_kept(settings, monkeypatch):
    data = Data()
    for n, t in ((1, T1), (2, T2)):
        data.add(n, f"b{n}", t)
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url))
        hidden = data.items.pop(2)

        def get_item_still_works(original):
            async def wrapper(*a, **k):
                data.items[2] = hidden  # visible to get_item, not to this listing
                try:
                    return await original(*a, **k)
                finally:
                    data.items.pop(2, None)
            return wrapper
        from aquifer.integrator import sync as sync_mod
        monkeypatch.setattr(sync_mod, "reconcile_stream",
                            get_item_still_works(sync_mod.reconcile_stream))
        monkeypatch.setattr(jobs, "reconcile_stream", sync_mod.reconcile_stream)
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    assert set(_texts(settings)) == {"1", "2"}  # the detail lookup proved it exists


def test_partial_listing_never_deletes(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2)):
        data.add(n, f"b{n}", t)
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url, plan=_plan(enumeration="partial")))
        data.items.clear()
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    assert set(_texts(settings)) == {"1", "2"}


def test_empty_listing_without_a_verifier_deletes_nothing(settings):
    data = Data()
    for n, t in ((1, T1), (2, T2)):
        data.add(n, f"b{n}", t)
    plan = _plan(detail=False)
    plan.streams[0].fields.body = "title"
    with FakeServer(data) as srv:
        service = _run(settings, _source(srv.url, plan=plan))
        data.items.clear()  # e.g. the token lost access
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    assert set(_texts(settings)) == {"1", "2"}
    state = jobs.registry_for(settings).stream_state("tracker", "items")
    assert "no items" in state["deletion_suspended"]


# --- bearer and OAuth -----------------------------------------------------------------------

def test_bearer_token_auth_and_rejection(settings, tmp_path):
    token_file = tmp_path / "tok"
    token_file.write_text("s3cret-token-value\n")
    data = Data()
    data.add(1, "a", T1)
    with FakeServer(data, auth="bearer", token="s3cret-token-value") as srv:
        good = _source(srv.url, auth=AuthConfig(type="bearer", secret_ref=f"file:{token_file}"))
        _run(settings, good)
        assert set(_texts(settings)) == {"1"}
        token_file.write_text("wrong")
        service = NodeService(settings)
        service.queue.put("mcp_sync", {"source": "tracker"})
        service.drain(30)
    status = sources_status(settings)[0]
    assert status["state"] == "needs_reauth"
    assert "aquifer sources authorize tracker" in status["action"]


def _authorize(settings, source_name: str) -> dict:
    """Run the mcp_authorize job; act as the browser (follow the URL, deliver the callback)."""
    reg = jobs.registry_for(settings)
    request_id = reg.oauth_begin(source_name)
    jobs.register()
    service = NodeService(settings)
    service.queue.put("mcp_authorize", {"source": source_name, "request_id": request_id})
    worker = threading.Thread(target=service.run_once)
    worker.start()
    deadline = time.time() + 20
    while time.time() < deadline and not (reg.oauth_get(request_id) or {}).get("auth_url"):
        time.sleep(0.05)
    auth_url = reg.oauth_get(request_id)["auth_url"]
    redirect = httpx.get(auth_url, follow_redirects=False).headers["location"]
    q = parse_qs(urlparse(redirect).query)
    assert reg.oauth_deliver(q["state"][0], code=q["code"][0], error=None)
    worker.join(30)
    return reg.oauth_get(request_id)


def test_oauth_authorize_then_unattended_refresh_and_reauth(settings, tmp_path):
    oauth = FakeOAuth()
    data = Data()
    data.add(1, "a", T1)
    with FakeServer(data, auth="oauth", oauth=oauth) as srv:
        reg = jobs.registry_for(settings)
        ref = jobs.credentials_ref(settings, "tracker", "oauth")
        reg.save(_source(srv.url, auth=AuthConfig(type="oauth", secret_ref=ref),
                         state="needs_authorization"))
        result = _authorize(settings, "tracker")
        assert result["status"] == "succeeded", result
        assert reg.get("tracker").state == "active"
        cred = (tmp_path / "credentials" / "tracker-oauth.json")
        assert cred.stat().st_mode & 0o777 == 0o600
        assert "oauth_metadata" in cred.read_text()  # remembered for unattended refresh

        jobs.register()
        service = NodeService(settings)
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
        assert set(_texts(settings)) == {"1"}

        # The access token expires; a later (restarted) run refreshes it unattended.
        oauth.expire_all_access()
        data.add(2, "b", T2)
        jobs._registry.cache_clear()
        service = NodeService(settings)
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
        assert oauth.refreshes >= 1
        assert set(_texts(settings)) == {"1", "2"}

        # Refresh token revoked too: the source asks for re-authorization; nothing hangs.
        oauth.expire_all_access()
        oauth.refresh.clear()
        service.queue.put("mcp_sync", {"source": "tracker", "full": True})
        service.drain(60)
    status = sources_status(settings)[0]
    assert status["state"] == "needs_reauth"
    assert "authorize tracker" in status["action"]


def test_oauth_callback_route_accepts_only_pending_single_use_states(settings):
    reg = jobs.registry_for(settings)
    rid = reg.oauth_begin("tracker")
    reg.oauth_update(rid, status="waiting", oauth_state="abc123")
    sent = []

    async def app(scope, receive, send):  # pragma: no cover - other paths
        raise AssertionError

    async def send(msg):
        sent.append(msg)

    def call(qs):
        sent.clear()
        asyncio.run(oauth_callback_app(app, lambda: reg)(
            {"type": "http", "path": "/oauth/callback", "query_string": qs}, None, send))
        return sent[0]["status"], sent[1]["body"]

    status, body = call(b"state=nope&code=x")
    assert status == 400 and b"Unknown or expired" in body
    status, body = call(b"state=abc123&code=the-code<script>")
    assert status == 200 and b"<script>" not in body
    assert reg.oauth_get(rid)["code"] == "the-code<script>"
    assert call(b"state=abc123&code=again")[0] == 400  # single use


# --- proposal -------------------------------------------------------------------------------

def test_stream_proposal_from_a_sample_call(settings):
    data = Data()
    data.add(1, "a", T1)
    data.add(2, "b", T2)
    with FakeServer(data) as srv:
        async def sample():
            src = _source(srv.url)
            async with connect(src, allow_private=True) as conn:
                tools = {t.name: t for t in await conn.list_tools()}
                res = await conn.call("list_items", {"orderBy": "UPDATED", "direction": "DESC"})
                return tools["list_items"], res.data
        tool, sample_data = asyncio.run(sample())
    stream = infer_stream("items", tool, {"orderBy": "UPDATED", "direction": "DESC"},
                          sample_data, kind=DocumentKind.TICKET)
    assert stream.items == "items" and stream.fields.id == "id"
    assert stream.fields.updated_at == "updated_at" and stream.fields.url == "url"
    assert (stream.pagination.type, stream.pagination.param, stream.pagination.next) == (
        "cursor", "after", "pageInfo.endCursor")
    assert stream.ordered_by_updated_desc is True
    assert stream.enumeration == "partial"  # completeness is the operator's call


def test_configured_oauth_scope_is_not_widened_by_the_server(settings):
    """Linear advertises "read write"; a source configured with "read" must request only read."""
    oauth = FakeOAuth()
    seen_scopes = []
    original = oauth.authorize

    async def recording(client, params):
        seen_scopes.append(params.scopes)
        return await original(client, params)

    oauth.authorize = recording
    data = Data()
    data.add(1, "a", T1)
    with FakeServer(data, auth="oauth", oauth=oauth) as srv:
        reg = jobs.registry_for(settings)
        ref = jobs.credentials_ref(settings, "tracker", "oauth")
        reg.save(_source(srv.url, auth=AuthConfig(type="oauth", secret_ref=ref, scope="read"),
                         state="needs_authorization"))
        assert _authorize(settings, "tracker")["status"] == "succeeded"
    assert seen_scopes == [["read"]]
