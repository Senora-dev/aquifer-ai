"""The product UI's API: sign-in, access model, GitHub connect, asking, evidence and agents.

Everything runs offline: GitHub is an httpx MockTransport, the index is in memory, answers use a
fake inferencer.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from aquifer.core.config import Settings
from aquifer.core.hooks import InterceptorPipeline
from aquifer.embedding.hashing import HashingEmbedder
from aquifer.mcp.tools import ContextTools
from aquifer.node.service import open_queue
from aquifer.vectorstore.memory import InMemoryVectorStore
from aquifer.web import github_source
from aquifer.web.answer import AnswerService, validate
from aquifer.web.app import WebApp
from aquifer.web.demo import CORPUS_PATH, build_corpus
from aquifer.web.evidence import best_window, permalink, query_terms
from aquifer.web.usage import UsageLog

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "a" * 40
READER = "r" * 40
CSRF = {"x-aquifer-csrf": "1"}


class FakeGitHub:
    def __init__(self) -> None:
        self.reject = False
        self.scopes: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.reject:
            return httpx.Response(401, json={"message": "Bad credentials"})
        headers = {"x-oauth-scopes": self.scopes} if self.scopes is not None else {}
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "acme-dev"}, headers=headers)
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[
                {"full_name": "acme/payments", "private": True, "permissions": {"push": True}},
                {"full_name": "acme/platform", "private": False},
            ])
        self.preview_auth = request.headers.get("authorization")
        path = request.url.path
        if path == "/repos/acme/payments":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/acme/payments/readme":
            return httpx.Response(200, json={})
        if path == "/repos/acme/payments/git/trees/main":
            return httpx.Response(200, json={"tree": [
                {"path": "docs/adr/0007-ledger-on-postgres.md", "type": "blob"},
                {"path": "infra/main.tf", "type": "blob"},
                {"path": "src/app.py", "type": "blob"},
                {"path": "docs/adr", "type": "tree"}]})
        if path == "/repos/acme/payments/issues":
            return httpx.Response(200, json=[
                {"number": 12, "title": "Checkout timeouts", "state": "open"},
                {"number": 14, "title": "a PR", "state": "open", "pull_request": {}}])
        if path == "/repos/acme/payments/pulls":
            return httpx.Response(200, json=[{"number": 14, "title": "Retry billing",
                                              "state": "closed"}])
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("AQUIFER_GITHUB__REPO_ALLOWLIST", "AQUIFER_GITHUB__TOKEN_SECRET"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    settings.node.data_dir = str(tmp_path)
    settings.embedding.provider = "hashing"
    settings.mcp.auth_mode = "token"
    settings.mcp.auth_tokens = {"default": ADMIN, "reader": READER}
    settings.mcp.auth_tokens_ref = f"file:{tmp_path / 'tokens.json'}"
    settings.integrator.enabled = False
    tools = ContextTools(embedder=HashingEmbedder(), vector_store=InMemoryVectorStore(None),
                         interceptors=InterceptorPipeline(), settings=settings)
    fake = FakeGitHub()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler),
                        base_url="https://api.github.test")
    queue = open_queue(settings)
    web = WebApp(settings, tools, queue=queue, github_http=http)
    client = TestClient(web.mount(_not_found))
    return {"settings": settings, "web": web, "client": client, "fake": fake, "queue": queue,
            "tools": tools}


async def _not_found(scope, receive, send) -> None:  # noqa: ANN001 - stands in for the MCP app
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def sign_in(client: TestClient, token: str) -> None:
    r = client.post("/api/session", json={"token": token}, headers=CSRF)
    assert r.status_code == 200, r.text


# --- sign-in and access model ---------------------------------------------------------

def test_api_requires_sign_in_and_rejects_bad_tokens(env):
    c = env["client"]
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/session", json={"token": "x" * 40}, headers=CSRF).status_code == 401
    sign_in(c, ADMIN)
    me = c.get("/api/session").json()
    assert me == {"auth_required": True, "signed_in": True, "principal": "default",
                  "admin": True}
    assert c.get("/api/overview").json()["phase"] == "empty"


def test_session_cookie_is_httponly_strict_and_csrf_is_required(env):
    c = env["client"]
    r = c.post("/api/session", json={"token": ADMIN}, headers=CSRF)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.post("/api/github/sync", json={}).status_code == 403  # no CSRF header
    assert c.post("/api/github/sync", json={}, headers=CSRF).status_code == 200


def test_revoking_a_token_ends_its_ui_sessions(env):
    c = env["client"]
    sign_in(c, READER)
    assert c.get("/api/overview").status_code == 200
    env["settings"].mcp.auth_tokens.pop("reader")
    assert c.get("/api/overview").status_code == 401


def test_readers_can_ask_but_not_change_sources_or_tokens(env):
    c = env["client"]
    sign_in(c, READER)
    assert c.get("/api/session").json()["admin"] is False
    for method, path, body in [("post", "/api/github/check", {"token": "ghp_x"}),
                               ("post", "/api/github/connect", {"repos": ["acme/x"]}),
                               ("post", "/api/agents", {"name": "bot"}),
                               ("delete", "/api/agents/default", None)]:
        r = getattr(c, method)(path, headers=CSRF, **({"json": body} if body else {}))
        assert r.status_code == 403, path
        assert r.json()["error"] == "forbidden"
    r = c.post("/api/ask", json={"question": "What depends on billing-api?",
                                 "workspace": "demo"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["evidence"]


def test_security_headers_on_ui_responses(env):
    r = env["client"].get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


# --- GitHub ---------------------------------------------------------------------------

def test_github_connect_stores_a_secret_reference_and_queues_a_full_sync(env):
    c, s = env["client"], env["settings"]
    sign_in(c, ADMIN)
    token = "github_pat_" + "x" * 40
    r = c.post("/api/github/check", json={"token": token}, headers=CSRF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["login"] == "acme-dev" and body["token_type"] == "fine-grained"
    assert [x["full_name"] for x in body["repos"]] == ["acme/payments", "acme/platform"]
    assert token not in r.text
    assert "readable by every Aquifer access token" in body["review"]["visibility"]

    # A repository the token cannot see is refused server-side.
    r = c.post("/api/github/connect", json={"repos": ["acme/secret"]}, headers=CSRF)
    assert r.status_code == 400 and r.json()["error"] == "repo_not_visible"

    c.post("/api/github/check", json={"token": token}, headers=CSRF)
    r = c.post("/api/github/connect", json={"repos": ["acme/payments"]}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert s.github.repo_allowlist == ["acme/payments"]
    assert s.github.token_secret.startswith("file:")
    stored = Path(s.github.token_secret[5:]).read_text()
    assert token in stored
    assert token not in github_source.config_path(s).read_text()
    assert any(j["kind"] == "discover" and j["payload"]["full"] for j in env["queue"].active())
    repos = c.get("/api/sources").json()["github"]["repos"]
    assert repos[0]["repo"] == "acme/payments" and repos[0]["state"] == "queued"

    # The CLI's settings see the same repositories.
    fresh = Settings()
    fresh.node.data_dir = s.node.data_dir
    github_source.apply(fresh)
    assert fresh.github.repo_allowlist == ["acme/payments"]


def test_deployment_allowlist_survives_the_ui_merge(tmp_path, monkeypatch):
    # Regression: the deployment's repositories come from AQUIFER_GITHUB__* and must not be
    # replaced by an empty list when the UI's choices are merged in.
    monkeypatch.setenv("AQUIFER_GITHUB__REPO_ALLOWLIST", '["acme/platform"]')
    monkeypatch.setenv("AQUIFER_GITHUB__TOKEN_SECRET", "ssm:/aquifer/github-token")
    s = Settings()
    s.node.data_dir = str(tmp_path)
    github_source.apply(s)
    assert s.github.repo_allowlist == ["acme/platform"]
    assert s.github.token_secret == "ssm:/aquifer/github-token"
    assert github_source.describe(s)["deployment_repos"] == ["acme/platform"]


def test_github_preview_shows_what_indexing_would_read(env):
    c = env["client"]
    sign_in(c, ADMIN)
    token = "ghp_" + "p" * 36
    r = c.post("/api/github/preview", json={"repos": ["acme/payments"]}, headers=CSRF)
    assert r.status_code == 409  # a token check comes first
    c.post("/api/github/check", json={"token": token}, headers=CSRF)
    r = c.post("/api/github/preview", json={"repos": ["acme/secret"]}, headers=CSRF)
    assert r.status_code == 400 and r.json()["error"] == "repo_not_visible"

    r = c.post("/api/github/preview", json={"repos": ["acme/payments"]}, headers=CSRF)
    assert r.status_code == 200, r.text
    repo = r.json()["repos"][0]
    assert repo["readme"] is True and repo["branch"] == "main"
    # The same file patterns as indexing: the ADR and the Terraform file, not src/app.py.
    assert repo["files"]["sample"] == ["docs/adr/0007-ledger-on-postgres.md", "infra/main.tf"]
    assert repo["issues"]["count"] == 1 and repo["issues"]["sample"][0]["number"] == 12
    assert repo["pulls"]["sample"][0]["title"] == "Retry billing"
    assert token not in r.text
    assert env["fake"].preview_auth == f"Bearer {token}"
    # Previewing stores and indexes nothing.
    assert env["settings"].github.repo_allowlist == []
    assert not github_source.config_path(env["settings"]).exists()
    assert not any(j["kind"] == "discover" for j in env["queue"].active())


def test_github_preview_needs_an_admin(env):
    c = env["client"]
    sign_in(c, READER)
    r = c.post("/api/github/preview", json={"repos": ["acme/payments"]}, headers=CSRF)
    assert r.status_code == 403


def test_rejected_github_token_is_an_actionable_error(env):
    c = env["client"]
    sign_in(c, ADMIN)
    env["fake"].reject = True
    r = c.post("/api/github/check", json={"token": "ghp_" + "y" * 36}, headers=CSRF)
    assert r.status_code == 400
    assert r.json()["error"] == "token_rejected"
    assert "401" in r.json()["message"]


def test_classic_token_write_scopes_are_shown_before_connecting(env):
    c = env["client"]
    sign_in(c, ADMIN)
    env["fake"].scopes = "repo, read:org"
    body = c.post("/api/github/check", json={"token": "ghp_" + "z" * 36}, headers=CSRF).json()
    assert body["write_scopes"] == ["repo"]
    assert body["review"]["notes"][0]["level"] == "warning"


def test_failed_discovery_marks_repositories_failed_with_an_action(env):
    c, s = env["client"], env["settings"]
    sign_in(c, ADMIN)
    s.github.repo_allowlist = ["acme/payments"]
    env["queue"].set_state("discovery:last_error", {"at": "2026-11-10T10:00:00+00:00",
                                                    "error": "SourceAuthError: bad token"})
    ov = c.get("/api/overview").json()
    assert ov["phase"] == "failed"
    assert ov["github"]["repos"][0]["error"]["code"] == "token_rejected"


# --- asking ---------------------------------------------------------------------------

def ask(c, question, workspace="demo"):
    r = c.post("/api/ask", json={"question": question, "workspace": workspace}, headers=CSRF)
    assert r.status_code == 200, r.text
    return r.json()


def test_demo_answer_cites_the_decision_with_exact_lines(env):
    c = env["client"]
    sign_in(c, ADMIN)
    res = ask(c, "Did we decide to move invoice events to Kafka?")
    assert res["status"] == "evidence" and res["demo"] is True
    top = res["evidence"][0]
    assert top["document_id"] == "github:acme/payments:pr:42"
    assert top["link"] == "https://github.com/acme/payments/pull/42"
    focus = [ln["text"] for ln in top["excerpt"]["lines"] if ln["focus"]]
    assert "Closed: we decided to keep SQS." in focus
    assert set(top["matched_terms"]) >= {"invoice", "kafka"}


def test_file_evidence_links_to_the_pinned_revision_and_lines(env):
    c = env["client"]
    sign_in(c, ADMIN)
    top = ask(c, "Why does the ledger use Postgres?")["evidence"][0]
    assert top["path"] == "docs/adr/0007-ledger-on-postgres.md"
    assert (top["line_start"], top["line_end"]) == (12, 13)
    assert top["link"].endswith("/blob/3f9c2a7e5b1d4c8a9e0f1a2b3c4d5e6f70819203/docs/adr/"
                                "0007-ledger-on-postgres.md#L12-L13")
    doc = c.get("/api/documents", params={"id": top["document_id"], "workspace": "demo",
                                          "start": 12, "end": 13}).json()
    disk = (ROOT / "examples/demo/repos/payments/docs/adr/0007-ledger-on-postgres.md")
    assert doc["lines"][11:13] == disk.read_text().split("\n")[11:13]
    assert "PostgreSQL" in doc["lines"][11]


def test_dependency_question_leads_with_the_compose_block(env):
    c = env["client"]
    sign_in(c, ADMIN)
    res = ask(c, "What depends on billing-api, and where is that defined?")
    assert res["terms"] == ["depends", "billing-api"]  # one identifier, not three terms
    top = res["evidence"][0]
    assert top["document_id"] == "github:acme/platform:file:docker-compose.yml"
    assert (top["line_start"], top["line_end"]) == (2, 6)  # orders-service: ... - billing-api
    disk = (ROOT / "examples/demo/repos/platform/docker-compose.yml").read_text().split("\n")
    focus = [ln["text"] for ln in top["excerpt"]["lines"] if ln["focus"]]
    assert focus == disk[1:6]
    assert focus[0].strip() == "orders-service:" and focus[-1].strip() == "- billing-api"
    # billing-api-v2 is a different service: it is not evidence for billing-api.
    ids = [e["document_id"] for e in res["evidence"]]
    assert "github:acme/legacy-billing:file:docker-compose.yml" not in ids


def test_demo_sources_list_the_indexed_repositories(env):
    c = env["client"]
    sign_in(c, ADMIN)
    data = c.get("/api/sources", params={"workspace": "demo"}).json()
    assert data["demo"] is True and "fictional" in data["note"]
    by_repo = {r["repo"]: r for r in data["repos"]}
    assert set(by_repo) == {"acme/legacy-billing", "acme/payments", "acme/platform"}
    assert by_repo["acme/payments"]["kinds"]["pr"] == 3
    assert "repos" not in c.get("/api/sources").json()  # the live view is unchanged


def test_questions_without_evidence_say_so(env):
    c = env["client"]
    sign_in(c, ADMIN)
    res = ask(c, "How do we rotate the HSM signing keys?")
    assert res["status"] == "insufficient" and res["evidence"] == []


def test_empty_live_index_reports_no_sources(env):
    c = env["client"]
    sign_in(c, ADMIN)
    assert ask(c, "anything", workspace=None)["status"] == "no_sources"


def test_documents_outside_the_scope_are_not_found(env):
    c = env["client"]
    sign_in(c, ADMIN)
    r = c.get("/api/documents", params={"id": "github:acme/payments:pr:42"})  # live, not demo
    assert r.status_code == 404


def test_grounded_answer_is_used_only_with_valid_citations(env):
    class Fake:
        def __init__(self, out):
            self.out = out

        def infer_json(self, system, user, schema=None):  # noqa: ANN001, ANN201
            assert "untrusted" in system
            return self.out

    c, web = env["client"], env["web"]
    sign_in(c, ADMIN)
    web.answers = AnswerService(env["settings"], inferencer=Fake(
        {"answer": "No: the PR was closed and SQS was kept [1].", "insufficient": False}))
    res = ask(c, "Did we decide to move invoice events to Kafka?")
    assert res["status"] == "answered" and res["answer"]["citations"] == [1]

    web.answers = AnswerService(env["settings"], inferencer=Fake(
        {"answer": "Yes, Kafka [9].", "insufficient": False}))
    res = ask(c, "Did we decide to move invoice events to Kafka?")
    assert res["status"] == "evidence" and "generation_note" in res

    web.answers = AnswerService(env["settings"], inferencer=Fake(
        {"answer": None, "insufficient": True}))
    assert ask(c, "Did we decide to move invoice events to Kafka?")["status"] == "not_answered"


def test_answers_are_unavailable_without_bedrock(env):
    status = AnswerService(env["settings"]).status()
    assert status["available"] is False and "Bedrock" in status["reason"]


def test_validate_rejects_uncited_answers():
    assert validate({"answer": "No citation."}, {1})["rejected"] == "citations"
    assert validate({"answer": "Yes [1][2]."}, {1, 2})["citations"] == [1, 2]


# --- agents ---------------------------------------------------------------------------

def test_issued_token_works_immediately_and_is_observed_after_a_query(env, tmp_path):
    c, s, web = env["client"], env["settings"], env["web"]
    sign_in(c, ADMIN)
    r = c.post("/api/agents", json={"name": "claude-code-alex"}, headers=CSRF)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert s.mcp.auth_tokens["claude-code-alex"] == token
    assert json.loads((tmp_path / "tokens.json").read_text())["claude-code-alex"] == token
    # Setup snippets read the token from the environment; the value is never inlined.
    setup = r.json()["setup"]
    assert all(token not in v for v in [setup["claude-code"], setup["json"], setup["verify"],
                                        setup["python-sdk"]])
    assert "${AQUIFER_TOKEN}" in setup["claude-code"] and "${AQUIFER_TOKEN}" in setup["json"]

    rows = {a["name"]: a for a in c.get("/api/agents").json()["agents"]}
    assert rows["claude-code-alex"]["configured"] and rows["claude-code-alex"]["observed"] is None

    web.usage.record("claude-code-alex", "search_context")
    rows = {a["name"]: a for a in c.get("/api/agents").json()["agents"]}
    assert rows["claude-code-alex"]["observed"]["calls_total"] == 1

    assert rows["claude-code-alex"]["role"] == "agent"
    assert rows["default"]["role"] == "admin"

    assert c.delete("/api/agents/default", headers=CSRF).status_code == 400  # admin token
    assert c.delete("/api/agents/claude-code-alex", headers=CSRF).status_code == 200
    assert "claude-code-alex" not in s.mcp.auth_tokens
    rows = {a["name"]: a for a in c.get("/api/agents").json()["agents"]}
    revoked = rows["claude-code-alex"]
    assert revoked["status"] == "revoked" and revoked["revoked_at"]


def test_reissued_name_does_not_inherit_the_revoked_credentials_calls(env):
    c, web = env["client"], env["web"]
    sign_in(c, ADMIN)
    c.post("/api/agents", json={"name": "claude-code"}, headers=CSRF)
    web.usage.record("claude-code", "search_context")
    assert c.delete("/api/agents/claude-code", headers=CSRF).status_code == 200
    assert c.post("/api/agents", json={"name": "claude-code"}, headers=CSRF).status_code == 201
    rows = {a["name"]: a for a in c.get("/api/agents").json()["agents"]}
    assert rows["claude-code"]["status"] == "active" and rows["claude-code"]["observed"] is None


def test_agent_credentials_are_mcp_only_and_never_admin(env):
    c, s = env["client"], env["settings"]
    sign_in(c, ADMIN)
    token = c.post("/api/agents", json={"name": "ci-bot"}, headers=CSRF).json()["token"]
    # Admin and built-in names cannot be issued to an agent.
    s.web.admin_principals.append("ops")
    for name in ("ops", "local-stdio"):
        r = c.post("/api/agents", json={"name": name}, headers=CSRF)
        assert r.status_code == 400 and r.json()["error"] == "reserved"
    # An agent credential cannot open a UI session (and so no admin action either).
    other = TestClient(env["web"].mount(_not_found))
    r = other.post("/api/session", json={"token": token}, headers=CSRF)
    assert r.status_code == 403 and r.json()["error"] == "agent_credential"
    assert token not in r.text
    # The home page counts agent credentials and the ones seen in the last 7 days.
    ov = c.get("/api/overview").json()["agents"]
    assert ov == {"credentials": 2, "active_7d": 0}  # ci-bot and the reader token
    env["web"].usage.record("ci-bot", "search_context")
    assert c.get("/api/overview").json()["agents"]["active_7d"] == 1


def test_mcp_endpoint_accepts_agent_tokens_and_rejects_missing_invalid_and_revoked(env):
    from aquifer.mcp.server import create_http_app

    c, s, web = env["client"], env["settings"], env["web"]
    sign_in(c, ADMIN)
    token = c.post("/api/agents", json={"name": "probe"}, headers=CSRF).json()["token"]
    mcp_app = create_http_app(s, env["tools"], on_call=web.usage.record)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {}}}
    accept = {"Content-Type": "application/json",
              "Accept": "application/json, text/event-stream"}
    with TestClient(web.mount(mcp_app)) as mc:
        ok = mc.post("/mcp", json=call, headers={**accept, "Authorization": f"Bearer {token}"})
        assert ok.status_code == 200 and '"result"' in ok.text
        assert mc.post("/mcp", json=call, headers=accept).status_code == 401
        bad = {**accept, "Authorization": "Bearer " + "x" * 48}
        assert mc.post("/mcp", json=call, headers=bad).status_code == 401
        rows = {a["name"]: a for a in c.get("/api/agents").json()["agents"]}
        assert rows["probe"]["observed"]["tools"] == {"list_sources": 1}
        assert c.delete("/api/agents/probe", headers=CSRF).status_code == 200
        revoked = mc.post("/mcp", json=call,
                          headers={**accept, "Authorization": f"Bearer {token}"})
        assert revoked.status_code == 401


def test_settings_page_reports_models_without_secrets(env):
    c = env["client"]
    sign_in(c, READER)
    r = c.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "token" and "embedding" in body and "answers" in body
    assert ADMIN not in r.text and READER not in r.text


def test_suggestions_are_short_and_come_from_indexed_content():
    from aquifer.web.app import _compose_dependencies

    compose = ("services:\n  orders-service:\n    depends_on:\n      - orders-db\n"
               "      - billing-api\n  checkout:\n    depends_on:\n      - billing-api\n"
               "    image: x\n")
    deps = _compose_dependencies(compose)
    assert deps.count("billing-api") == 2 and "orders-db" in deps and "image: x" not in deps


def test_mcp_server_records_tool_calls_per_principal(tmp_path):
    from aquifer.mcp.server import create_server

    calls = []
    settings = Settings()
    tools = ContextTools(embedder=HashingEmbedder(), vector_store=InMemoryVectorStore(None),
                         interceptors=InterceptorPipeline(), settings=settings)
    server = create_server(tools=tools, settings=settings, default_principal="local-stdio",
                           on_call=lambda p, t: calls.append((p, t)))
    import asyncio

    asyncio.run(server.call_tool("list_sources", {}))
    assert calls == [("local-stdio", "list_sources")]


def test_usage_log_summary(tmp_path):
    log = UsageLog(tmp_path / "usage.db")
    log.record("bot", "search_context", now=1_000_000)
    log.record("bot", "get_document", now=1_000_100)
    s = log.summary(now=1_000_200)["bot"]
    assert s["calls_recent"] == 2 and s["tools"] == {"search_context": 1, "get_document": 1}


# --- pieces ---------------------------------------------------------------------------

def test_query_terms_and_yaml_windows():
    from aquifer.web.evidence import best_window, query_terms

    assert query_terms("Where is checkout_service defined?") == ["checkout_service"]
    lines = ["services:", "  web:", "    depends_on:", "      - db", "  db:", "    image: x"]
    w = best_window(lines, 1, len(lines), ["depends"])
    assert (w["start"], w["end"]) == (2, 3)  # the parent key is named


def test_demo_corpus_matches_examples():
    built = build_corpus(ROOT / "examples" / "demo" / "repos")
    assert json.loads(CORPUS_PATH.read_text()) == json.loads(json.dumps(built)), \
        "run: python scripts/build_demo_corpus.py"


def test_best_window_prefers_prose_and_finishes_sentences():
    lines = ["# Store ledger in PostgreSQL", "", "ledger-service stores data in PostgreSQL, in",
             "the shared instance.", "", "Other."]
    w = best_window(lines, 1, len(lines), query_terms("Why does the ledger use Postgres?"))
    assert (w["start"], w["end"]) == (3, 4)


def test_permalink_anchors_only_pinned_files():
    assert permalink("https://github.com/a/b/blob/abc/x.tf", revision="abc", path="x.tf",
                     start=3, end=5) == "https://github.com/a/b/blob/abc/x.tf#L3-L5"
    assert permalink("https://github.com/a/b/pull/4", revision=None, path=None, start=1,
                     end=2) == "https://github.com/a/b/pull/4"


# --- MCP Integrator through the UI ------------------------------------------------------

def test_integrator_source_needs_preview_and_explicit_enable(env):
    from aquifer.integrator import jobs
    from tests.integrator_fakes import Data, FakeServer
    from tests.test_integrator import _source

    c, s = env["client"], env["settings"]
    s.integrator.enabled = True
    s.integrator.allow_private_networks = True
    s.integrator.min_call_interval_s = 0
    jobs._registry.cache_clear()
    data = Data()
    data.add(1, "Runbook: failover payments-db", "2026-01-01T10:00:00Z", title="Failover")
    with FakeServer(data) as srv:
        source = _source(srv.url, state="ready")
        source.enabled = False
        jobs.registry_for(s).save(source)
        sign_in(c, READER)
        assert c.post("/api/mcp/tracker/preview", json={}, headers=CSRF).status_code == 403
        sign_in(c, ADMIN)
        r = c.post("/api/mcp/tracker/preview", json={}, headers=CSRF)
        assert r.status_code == 200, r.text
        pv = r.json()
        assert pv["nothing_was_indexed"] is True
        assert pv["streams"][0]["samples"][0]["title"] == "Failover"
        assert "every Aquifer MCP token" in pv["access"]
        assert not any(j["kind"] == "mcp_sync" for j in env["queue"].active())

        r = c.post("/api/mcp/tracker/enable", json={}, headers=CSRF)
        assert r.status_code == 200, r.text
        assert any(j["kind"] == "mcp_sync" for j in env["queue"].active())
        listed = c.get("/api/sources").json()["mcp_sources"]
        assert listed[0]["name"] == "tracker" and listed[0]["state"] == "active"

        assert c.post("/api/mcp/tracker/pause", json={}, headers=CSRF).status_code == 200
        assert jobs.registry_for(s).get("tracker").enabled is False
    jobs._registry.cache_clear()
