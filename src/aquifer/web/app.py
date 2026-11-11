"""The UI's JSON API and static files, mounted by ``aquifer serve`` in front of the MCP app.

Routes (everything else goes to the MCP app unchanged):

- ``/`` and ``/assets/*``: the single-page UI (no build step; plain HTML, CSS and JS).
- ``/api/session``: sign in / out with an Aquifer access token.
- ``/api/overview``, ``/api/sources``, ``/api/catalog``: what is connected and its state.
- ``/api/github/*``: connect the native GitHub source (admin).
- ``/api/mcp/*``: preview, enable, sync and pause MCP Integrator sources (admin).
- ``/api/ask``, ``/api/documents``: questions and source views, through :class:`ContextTools`.
- ``/api/agents``: access tokens, configured vs observed (admin to issue or revoke).

Reads go through the same protected path as MCP (authorization, repository scope, audit).
Mutating requests need the session cookie (SameSite=Strict) *and* an ``X-Aquifer-CSRF`` header.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections import Counter
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from aquifer.core.config import Settings
from aquifer.core.errors import log_event
from aquifer.web import agents as web_agents
from aquifer.web import github_source
from aquifer.web.answer import AnswerService
from aquifer.web.demo import NOTE as DEMO_NOTE
from aquifer.web.demo import SUGGESTIONS as DEMO_SUGGESTIONS
from aquifer.web.demo import DemoWorkspace
from aquifer.web.evidence import (
    best_window,
    doc_body,
    excerpt_lines,
    permalink,
    query_terms,
)
from aquifer.web.session import COOKIE, Principal, Sessions
from aquifer.web.usage import usage_log

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("aquifer.audit")

STATIC = Path(__file__).with_name("static")
TOKEN_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
INTEGRATOR_GITHUB_SOURCE = "github-mcp"
_MAX_QUESTION = 500
_EVIDENCE_DOCS = 5
_CLOSEST_DOCS = 3

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

CATALOG = [
    {"id": "github", "name": "GitHub", "logo": "github", "status": "available",
     "via": "Native connector",
     "what": "READMEs, issues, pull requests and decision files (ADRs, Terraform, Compose, "
             "Backstage)."},
    {"id": "github-mcp", "name": "GitHub Discussions & Releases", "logo": "github",
     "status": "available", "via": "MCP Integrator · GitHub's MCP server",
     "what": "Discussions and release notes. Uses the GitHub token you connected.",
     "requires": "github"},
    {"id": "atlassian", "name": "Jira & Confluence", "logo": "atlassian", "status": "early",
     "via": "MCP Integrator · Atlassian Rovo", "what": "Tickets and pages."},
    {"id": "linear", "name": "Linear", "logo": "linear", "status": "early",
     "via": "MCP Integrator", "what": "Issues and projects."},
    {"id": "notion", "name": "Notion", "logo": "notion", "status": "early",
     "via": "MCP Integrator", "what": "Pages and databases."},
    {"id": "sentry", "name": "Sentry", "logo": "sentry", "status": "early",
     "via": "MCP Integrator", "what": "Issues and releases."},
    {"id": "slack", "name": "Slack", "logo": "slack", "status": "early",
     "via": "MCP Integrator · your own Slack app", "what": "Channel messages."},
    {"id": "custom", "name": "Any MCP server", "logo": "mcp", "status": "early",
     "via": "MCP Integrator", "what": "Anything a remote MCP server lists with read-only "
                                     "tools."},
]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra) -> None:  # noqa: ANN003
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _json(data, status: int = 200, headers: dict | None = None) -> JSONResponse:  # noqa: ANN001
    return JSONResponse(json.loads(json.dumps(data, default=str)), status_code=status,
                        headers={"cache-control": "no-store", **(headers or {})})


class WebApp:
    def __init__(self, settings: Settings, tools, *, queue=None, service=None,  # noqa: ANN001
                 demo: DemoWorkspace | None = None, answers: AnswerService | None = None,
                 github_http=None) -> None:  # noqa: ANN001
        self.settings = settings
        self.tools = tools
        self.queue = queue
        self.service = service
        self.demo = demo or DemoWorkspace(settings)
        self.answers = answers or AnswerService(settings)
        self.sessions = Sessions(settings)
        self.usage = usage_log(settings.node.data_dir)
        self._github_http = github_http  # tests inject a fake GitHub
        # Tokens checked in this session and not yet connected: {sid: (token, probe, at)}.
        self._pending: dict[str, tuple[str, dict, float]] = {}
        self._lock = threading.Lock()
        github_source.apply(settings)

    # --- plumbing ------------------------------------------------------------------

    def asgi(self) -> Starlette:
        r = self._route
        routes = [
            Route("/", self.index),
            Mount("/assets", app=StaticFiles(directory=STATIC), name="assets"),
            Route("/api/session", self.session_get, methods=["GET"]),
            Route("/api/session", self.session_post, methods=["POST"]),
            Route("/api/session", self.session_delete, methods=["DELETE"]),
            r("/api/overview", self.overview),
            r("/api/settings", self.server_settings),
            r("/api/sources", self.sources),
            r("/api/catalog", self.catalog),
            r("/api/github/check", self.github_check, "POST", admin=True),
            r("/api/github/preview", self.github_preview, "POST", admin=True),
            r("/api/github/connect", self.github_connect, "POST", admin=True),
            r("/api/github/sync", self.github_sync, "POST", admin=True),
            r("/api/github/repos/{owner}/{name}", self.github_remove, "DELETE", admin=True),
            r("/api/mcp/github", self.mcp_github, "POST", admin=True),
            r("/api/mcp/{name}/{action}", self.mcp_action, "POST", admin=True),
            r("/api/ask", self.ask, "POST"),
            r("/api/documents", self.document),
            r("/api/agents", self.agents),
            r("/api/agents", self.agent_create, "POST", admin=True),
            r("/api/agents/{name}", self.agent_revoke, "DELETE", admin=True),
        ]
        return Starlette(routes=routes)

    def mount(self, mcp_app):  # noqa: ANN001, ANN201
        """An ASGI app: the UI's paths here, everything else to ``mcp_app``."""
        web = self.asgi()

        async def app(scope, receive, send) -> None:  # noqa: ANN001
            path = scope.get("path", "") if scope["type"] == "http" else ""
            if path == "/" or path.startswith(("/api/", "/assets/")):
                async def send_secure(message) -> None:  # noqa: ANN001
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        headers += [(b"content-security-policy", CSP.encode()),
                                    (b"x-content-type-options", b"nosniff"),
                                    (b"referrer-policy", b"no-referrer"),
                                    (b"x-frame-options", b"DENY")]
                        message = {**message, "headers": headers}
                    await send(message)

                await web(scope, receive, send_secure)
                return
            await mcp_app(scope, receive, send)

        return app

    def _route(self, path: str, fn, method: str = "GET", *, admin: bool = False):  # noqa: ANN001, ANN202
        async def endpoint(request: Request) -> Response:
            try:
                principal = self._principal(request)
                if method != "GET" and request.headers.get("x-aquifer-csrf") != "1":
                    raise ApiError(403, "csrf", "Missing request header.")
                if admin and not principal.admin:
                    raise ApiError(403, "forbidden",
                                   f"Signed in as '{principal.name}', which can read but not "
                                   "change sources or access tokens. Ask an Aquifer admin "
                                   f"(tokens: {', '.join(self.settings.web.admin_principals)}).")
                body = {}
                if method in {"POST", "PUT"}:
                    try:
                        body = await request.json()
                    except ValueError:
                        body = {}
                    if not isinstance(body, dict):
                        raise ApiError(400, "bad_request", "Expected a JSON object.")
                return await run_in_threadpool(fn, request, principal, body)
            except ApiError as exc:
                return _json({"error": exc.code, "message": exc.message, **exc.extra},
                             exc.status)
            except PermissionError as exc:
                return _json({"error": "forbidden", "message": str(exc)}, 403)
            except Exception:  # noqa: BLE001
                logger.exception("web api error on %s", path)
                return _json({"error": "internal", "message": "Something went wrong on the "
                              "server. Details are in the server log (aquifer status)."}, 500)

        return Route(path, endpoint, methods=[method])

    def _principal(self, request: Request) -> Principal:
        p = self.sessions.principal(request.cookies.get(COOKIE))
        if p is None:
            raise ApiError(401, "signed_out", "Sign in with an Aquifer access token.")
        return p

    def _tools_principal(self, p: Principal) -> str | None:
        return p.name if self.sessions.auth_required else None

    def _audit(self, action: str, principal: Principal, **fields) -> None:  # noqa: ANN003
        log_event(audit_logger, "ui_action", action=action, principal=principal.name, **fields)

    # --- page and session ----------------------------------------------------------

    async def index(self, request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"cache-control": "no-cache"})

    async def session_get(self, request: Request) -> Response:
        p = self.sessions.principal(request.cookies.get(COOKIE))
        return _json({"auth_required": self.sessions.auth_required, "signed_in": p is not None,
                      "principal": p.name if p else None, "admin": bool(p and p.admin)})

    async def session_post(self, request: Request) -> Response:
        if not self.sessions.auth_required:
            return _json({"signed_in": True, "principal": "local", "admin": True})
        if request.headers.get("x-aquifer-csrf") != "1":
            return _json({"error": "csrf", "message": "Missing request header."}, 403)
        client = (request.client.host if request.client else "unknown")
        if self.sessions.throttled(client):
            return _json({"error": "throttled",
                          "message": "Too many attempts. Wait a minute and try again."}, 429)
        try:
            token = str((await request.json()).get("token") or "").strip()
        except (ValueError, AttributeError):
            token = ""
        result = self.sessions.sign_in(token) if token else None
        if result is None:
            log_event(audit_logger, "ui_sign_in_failed", level=logging.WARNING, client=client)
            return _json({"error": "invalid_token",
                          "message": "That token is not valid for this Aquifer server."}, 401)
        principal, cookie = result
        if web_agents.is_agent(self.settings, principal):
            log_event(audit_logger, "ui_sign_in_refused", level=logging.WARNING,
                      principal=principal, reason="agent_credential")
            return _json({"error": "agent_credential",
                          "message": "This is an agent credential: it works only for MCP "
                                     "clients. Sign in with your own access token."}, 403)
        log_event(audit_logger, "ui_sign_in", principal=principal)
        resp = _json({"signed_in": True, "principal": principal,
                      "admin": self.sessions.is_admin(principal)})
        resp.set_cookie(COOKIE, cookie, max_age=self.settings.web.session_hours * 3600,
                        httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https")
        return resp

    async def session_delete(self, request: Request) -> Response:
        resp = _json({"signed_in": False})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # --- state of the deployment ---------------------------------------------------

    def _store(self):  # noqa: ANN202
        return self.tools.vector_store

    def _github_status(self) -> dict:
        s = self.settings
        store = self._store()
        counts = dict(store.facet("repo", None, size=10_000) or [])
        active = self.queue.active() if self.queue else []
        jobs = Counter(j["payload"].get("repo") for j in active if j["kind"] == "fetch")
        running = Counter(j["payload"].get("repo") for j in active
                          if j["kind"] == "fetch" and j["status"] == "running")
        discovering = any(j["kind"] == "discover" for j in active)
        get = self.queue.get_state if self.queue else (lambda k, d=None: d)
        last_success = get("discovery:last_success")
        last_error = get("discovery:last_error")
        dead = [d for d in (self.queue.dead_letters(limit=200) if self.queue else [])
                if d["kind"] == "fetch"]
        desc = github_source.describe(s)
        sync_error = _discovery_error(last_error) if last_error else None
        repos = []
        for repo in s.github.repo_allowlist:
            docs = counts.get(repo, 0)
            entry = {"repo": repo, "documents": docs,
                     "origin": "deployment" if repo in desc["deployment_repos"] else "ui",
                     "jobs": jobs.get(repo, 0), "running": running.get(repo, 0)}
            if docs:
                entry["last_indexed_at"] = store.max_value("indexed_at", {"repo": repo})
            repo_dead = [d for d in dead if d.get("repo") == repo]
            if entry["jobs"]:
                entry["state"] = "indexing"
            elif sync_error and not docs:
                entry.update(state="failed", error=sync_error)
            elif repo_dead:
                entry.update(state="failed", error={
                    "message": repo_dead[0]["error"],
                    "action": "After fixing the cause, requeue it: aquifer dead-letters "
                              "--requeue all"})
            elif docs:
                entry["state"] = "ready"
            elif discovering:
                entry["state"] = "queued"
            elif not last_success:
                entry["state"] = "waiting"
            else:
                entry["state"] = "empty"
            repos.append(entry)
        return {
            **desc,
            "repos": repos,
            "token_configured": bool(s.github.token_secret),
            "sync": {"interval_s": s.node.sync_interval_s, "running": discovering,
                     "last_success": (last_success or {}).get("at"),
                     "last_started": get("discovery:last_started"),
                     "error": sync_error},
        }

    def _mcp_status(self) -> list[dict]:
        if not self.settings.integrator.enabled:
            return []
        from aquifer.integrator.status import sources_status

        counts = dict(self._store().facet("repo", None, size=10_000) or [])
        out = sources_status(self.settings, self.queue)
        for s in out:
            s["documents"] = counts.get(s["scope"], 0)
        return out

    def _counts(self) -> tuple[int, int]:
        counts = dict(self._store().facet("repo", None, size=10_000) or [])
        scopes = list(self.settings.github.repo_allowlist)
        if self.tools.extra_sources is not None:
            scopes += [x["scope"] for x in self.tools.extra_sources()]
        docs = sum(counts.get(s, 0) for s in scopes)
        return docs, len(scopes)

    def overview(self, request: Request, p: Principal, body: dict) -> Response:
        github = self._github_status()
        mcp = self._mcp_status()
        docs, scopes = self._counts()
        indexing = github["sync"]["running"] or any(r["jobs"] for r in github["repos"]) or any(
            (m.get("last_run") or {}).get("status") == "running" for m in mcp)
        failed = [r for r in github["repos"] if r.get("state") == "failed"] + [
            m for m in mcp if m.get("action") and m["state"] in {"error", "needs_reauth"}]
        if not github["repos"] and not mcp:
            phase = "empty"
        elif docs:
            phase = "ready"
        elif indexing or any(r["state"] in {"queued", "waiting"} for r in github["repos"]):
            phase = "indexing"
        elif failed:
            phase = "failed"
        else:
            phase = "nothing_indexed"
        agents = self._agent_rows(p)
        return _json({
            "phase": phase,
            "indexing": bool(indexing),
            "documents": docs,
            "scopes": scopes,
            "github": github,
            "mcp_sources": mcp,
            "failed": len(failed),
            "answers": self.answers.status(),
            "demo": {"available": self.demo.available, "note": DEMO_NOTE,
                     "suggestions": DEMO_SUGGESTIONS},
            "suggestions": self._suggestions() if docs else [],
            # Agent credentials (not admin or UI tokens): how many exist, and how many made an
            # MCP tool call in the last 7 days. Neither is a live connection count.
            "agents": {
                "credentials": sum(1 for a in agents
                                   if a["status"] == "active" and a["role"] in {"agent",
                                                                                 "reader"}),
                "active_7d": sum(1 for a in agents if a["status"] == "active" and a["observed"]
                                 and a["observed"].get("calls_recent")),
            },
            "principal": p.name, "admin": p.admin,
        })

    def server_settings(self, request: Request, p: Principal, body: dict) -> Response:
        """Technical facts about this server (no secrets), for the Settings page."""
        s = self.settings
        ans = self.answers.status()
        return _json({
            "embedding": {"provider": s.embedding.provider, "model": s.embedding.model_id},
            "extraction": {"enabled": s.semantic_index.enabled,
                           "model": s.semantic_index.model_id or None},
            "answers": {"available": ans.get("available", False), "model": ans.get("model"),
                        "reason": ans.get("reason")},
            "sync_interval_s": s.node.sync_interval_s,
            "auth_mode": s.mcp.auth_mode,
            "network_access": s.web.network_access,
            "admin_principals": list(s.web.admin_principals),
            "environment": s.environment,
        })

    def _suggestions(self) -> list[str]:
        """Short questions about what is indexed (no model involved): the most depended-on
        Compose service, changes described as "from X to Y" in pull requests, decision records."""
        repos = list(self.settings.github.repo_allowlist)
        if not repos:
            return []
        depends: Counter = Counter()
        moves: list[str] = []
        decisions = False
        seen: set[str] = set()
        try:
            for r in self._store().search_by_metadata({"repo": repos}, 300):
                if r.document_id in seen:
                    continue
                seen.add(r.document_id)
                path = r.path or ""
                if r.kind == "file" and re.search(r"(^|/)(adrs?|decisions)/", path):
                    decisions = True
                elif r.kind == "file" and re.search(r"(compose[^/]*\.ya?ml)$", path):
                    depends.update(_compose_dependencies(r.text))
                elif r.kind == "pr" and r.title:
                    m = re.search(r"\bfrom ([\w.-]{2,24}) to ([\w.-]{2,24})\b", r.title, re.I)
                    if m:
                        moves.append(f"Why did we move from {m.group(1)} to {m.group(2)}?")
        except Exception:  # noqa: BLE001 - suggestions are optional
            logger.debug("suggestions failed", exc_info=True)
        out = [f"What depends on {name}?" for name, _ in depends.most_common(1)]
        out += list(dict.fromkeys(moves))[:2]
        if decisions:
            out.append("Where are our architecture decisions documented?")
        return out[:4]

    def sources(self, request: Request, p: Principal, body: dict) -> Response:
        if request.query_params.get("workspace") == "demo":
            self._workspace("demo")
            return _json({"demo": True, "note": DEMO_NOTE, "repos": self.demo.summary()})
        return _json({"github": self._github_status(), "mcp_sources": self._mcp_status(),
                      "admin": p.admin, "access": github_source.ACCESS_NOTE})

    def catalog(self, request: Request, p: Principal, body: dict) -> Response:
        from aquifer.integrator.providers import PROVIDERS

        items = []
        for c in CATALOG:
            prov = PROVIDERS.get(c["id"] if c["id"] != "github-mcp" else "github")
            entry = {**c}
            if prov is not None and c["status"] == "early":
                entry["verified"] = prov.verified
                entry["notes"] = prov.notes
            items.append(entry)
        return _json({"items": items, "admin": p.admin,
                      "integrator": self.settings.integrator.enabled})

    # --- GitHub --------------------------------------------------------------------

    def _probe(self, token: str) -> dict:
        try:
            return github_source.probe(token, self.settings, http=self._github_http)
        except github_source.GitHubAccessError as exc:
            raise ApiError(400, exc.code, exc.message) from None

    def github_check(self, request: Request, p: Principal, body: dict) -> Response:
        env = github_source.env_github()
        if env.token_secret:
            from aquifer.core.secrets import resolve_secret

            token = resolve_secret(env.token_secret).strip()
            source = "deployment"
        elif body.get("use_stored") and github_source.load(self.settings).get("token_ref"):
            from aquifer.core.secrets import resolve_secret

            token = resolve_secret(github_source.load(self.settings)["token_ref"]).strip()
            source = "stored"
        else:
            raw = body.get("token")
            if not raw:
                raise ApiError(400, "token_required", "Paste a GitHub token to continue.")
            try:
                token = github_source.clean_token(str(raw))
            except github_source.GitHubAccessError as exc:
                raise ApiError(400, exc.code, exc.message) from None
            source = "ui"
        result = self._probe(token)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if now - v[2] < 900}
            self._pending[p.sid] = (token if source == "ui" else "", result, now)
        self._audit("github_token_checked", p, token_type=result["token_type"],
                    repos_visible=len(result["repos"]))
        connected = set(self.settings.github.repo_allowlist)
        return _json({**result, "token_source": source,
                      "connected": sorted(connected),
                      "review": github_source.access_review(result, self.settings)})

    def _checked(self, p: Principal, repos: list[str]) -> tuple[str, dict]:
        """The token and probe of this session's last check, limited to what it can read."""
        if not repos:
            raise ApiError(400, "no_repos", "Choose at least one repository.")
        with self._lock:
            pending = self._pending.get(p.sid)
        if pending is None:
            raise ApiError(409, "check_expired", "Check the token again before connecting "
                                                 "(checks expire after 15 minutes).")
        token, probe, _ = pending
        visible = {r["full_name"] for r in probe["repos"]}
        unknown = [r for r in repos if r not in visible]
        if unknown:
            raise ApiError(400, "repo_not_visible",
                           "This token cannot read: " + ", ".join(unknown))
        return token, probe

    def github_preview(self, request: Request, p: Principal, body: dict) -> Response:
        """Sample what indexing would read, before anything is stored or indexed."""
        repos = [str(r) for r in (body.get("repos") or [])][:200]
        token, _ = self._checked(p, repos)
        if not token:  # the deployment's or the previously stored token
            from aquifer.core.secrets import resolve_secret

            ref = github_source.env_github().token_secret or \
                github_source.load(self.settings).get("token_ref")
            token = resolve_secret(ref).strip() if ref else ""
        try:
            result = github_source.preview_content(token, self.settings, repos,
                                                   http=self._github_http)
        except github_source.GitHubAccessError as exc:
            raise ApiError(400, exc.code, exc.message) from None
        self._audit("github_previewed", p, repos=len(repos))
        return _json(result)

    def github_connect(self, request: Request, p: Principal, body: dict) -> Response:
        repos = [str(r) for r in (body.get("repos") or [])][:200]
        if not repos:
            raise ApiError(400, "no_repos", "Choose at least one repository.")
        with self._lock:
            pending = self._pending.pop(p.sid, None)
        if pending is None:
            raise ApiError(409, "check_expired", "Check the token again before connecting "
                                                 "(checks expire after 15 minutes).")
        token, probe, _ = pending
        visible = {r["full_name"] for r in probe["repos"]}
        unknown = [r for r in repos if r not in visible]
        if unknown:
            raise ApiError(400, "repo_not_visible",
                           "This token cannot read: " + ", ".join(unknown))
        token_ref = None
        if token:
            from aquifer.core.secrets import store_secret
            from aquifer.integrator.jobs import credentials_ref

            token_ref = credentials_ref(self.settings, "native-github", "token")
            store_secret(token_ref, token)
        added = github_source.connect(self.settings, repos, token_ref=token_ref,
                                      principal=p.name, probe=probe)
        self._queue_discovery(full=True)
        self._audit("github_connected", p, repos=len(repos), added=len(added))
        return _json({"added": added, "repos": self.settings.github.repo_allowlist})

    def _queue_discovery(self, full: bool) -> bool:
        if self.queue is None:
            return False
        added = self.queue.put("discover", {"full": full},
                               dedupe_key="discover:full" if full else "discover")
        if self.service is not None:
            self.service.wake()
        return added

    def github_sync(self, request: Request, p: Principal, body: dict) -> Response:
        added = self._queue_discovery(full=bool(body.get("full")))
        self._audit("github_sync", p)
        return _json({"queued": added})

    def github_remove(self, request: Request, p: Principal, body: dict) -> Response:
        repo = f"{request.path_params['owner']}/{request.path_params['name']}"
        if repo in github_source.env_github().repo_allowlist:
            raise ApiError(400, "deployment_repo",
                           f"{repo} is configured by the deployment "
                           "(AQUIFER_GITHUB__REPO_ALLOWLIST); remove it there.")
        if not github_source.remove_repo(self.settings, repo):
            raise ApiError(404, "not_found", f"{repo} is not connected.")
        removed = self._store().delete_by_filter({"repo": repo})
        self._audit("github_repo_removed", p, repo=repo)
        return _json({"removed": repo, "chunks_deleted": removed})

    # --- MCP Integrator ------------------------------------------------------------

    def _registry(self):  # noqa: ANN202
        if not self.settings.integrator.enabled:
            raise ApiError(400, "integrator_disabled", "The MCP Integrator is turned off "
                                                       "(AQUIFER_INTEGRATOR__ENABLED=false).")
        from aquifer.integrator.jobs import registry_for

        return registry_for(self.settings)

    def mcp_github(self, request: Request, p: Principal, body: dict) -> Response:
        """Create (or extend) the GitHub MCP source with discussion / release streams."""
        from aquifer.integrator.actions import add_template_streams
        from aquifer.integrator.models import AuthConfig, Source
        from aquifer.integrator.providers import get_provider

        reg = self._registry()
        targets = [str(t) for t in (body.get("targets") or [])][:50]
        streams = [s for s in (body.get("streams") or []) if s in {"discussions", "releases"}]
        if not targets or not streams:
            raise ApiError(400, "bad_request", "Choose repositories and what to import.")
        token_ref = self.settings.github.token_secret
        if not token_ref:
            raise ApiError(400, "github_not_connected", "Connect GitHub first: this uses the "
                                                        "same token.")
        provider = get_provider("github")
        source = reg.get(INTEGRATOR_GITHUB_SOURCE) or Source(
            name=INTEGRATOR_GITHUB_SOURCE, provider="github", url=provider.url,
            auth=AuthConfig(type="bearer", secret_ref=token_ref), headers=dict(provider.headers),
            schedule_hours=self.settings.integrator.default_schedule_hours,
            connected_by=p.name, state="ready")
        try:
            added = []
            for template in streams:
                added += add_template_streams(self.settings, source, template, targets)
        except ValueError as exc:
            raise ApiError(400, "plan_invalid", str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise _integrator_error(exc) from None
        self._audit("mcp_streams_added", p, source=source.name, streams=len(added))
        return _json({"source": source.name,
                      "streams": [s.coverage() for s in source.plan.streams]})

    def mcp_action(self, request: Request, p: Principal, body: dict) -> Response:
        from aquifer.integrator import actions

        name, action = request.path_params["name"], request.path_params["action"]
        reg = self._registry()
        source = reg.get(name)
        if source is None:
            raise ApiError(404, "not_found", f"No source named {name}.")
        try:
            if action == "preview":
                result = actions.preview(self.settings, source, limit=3)
                self._audit("mcp_previewed", p, source=name)
                return _json(result)
            if action == "enable":
                actions.enable(self.settings, source, queue=self.queue)
                if self.service is not None:
                    self.service.wake()
                self._audit("mcp_enabled", p, source=name)
                return _json({"enabled": name})
        except ValueError as exc:
            raise ApiError(400, "plan_invalid", str(exc)) from None
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _integrator_error(exc) from None
        if action == "sync":
            if not source.enabled:
                raise ApiError(400, "not_enabled", f"Enable {name} first.")
            added = bool(self.queue and self.queue.put(
                "mcp_sync", {"source": name, "full": False}, dedupe_key=f"mcp_sync:{name}"))
            if self.service is not None:
                self.service.wake()
            return _json({"queued": added})
        if action == "pause":
            source.enabled = False
            source.state = "paused"
            reg.save(source)
            self._audit("mcp_paused", p, source=name)
            return _json({"paused": name})
        raise ApiError(404, "not_found", "Unknown action.")

    # --- asking --------------------------------------------------------------------

    def _workspace(self, value: str | None):  # noqa: ANN202
        if value == "demo":
            if not self.demo.available:
                raise ApiError(404, "demo_unavailable", "The demo workspace is turned off.")
            return self.demo.tools, True
        return self.tools, False

    def ask(self, request: Request, p: Principal, body: dict) -> Response:
        question = str(body.get("question") or "").strip()[:_MAX_QUESTION]
        if not question:
            raise ApiError(400, "empty_question", "Type a question first.")
        tools, demo = self._workspace(body.get("workspace"))
        who = self._tools_principal(p)
        started = time.monotonic()
        terms = query_terms(question)
        searchable = sum(s.get("indexed_documents", 0) for s in tools.list_sources(principal=who))
        if not searchable:
            return _json({"question": question, "status": "no_sources", "terms": terms,
                          "evidence": [], "closest": [], "demo": demo})

        hits = tools.search_context(question, k=15, principal=who)
        best: dict[str, dict] = {}
        for hit in hits:
            best.setdefault(hit["document_id"], hit)
        evidence: list[dict] = []
        closest: list[dict] = []
        for hit in list(best.values())[: _EVIDENCE_DOCS * 2]:
            item = self._evidence_item(tools, who, hit, terms, demo)
            if item is None:
                continue
            if len(item["matched_terms"]) >= _needed(terms):
                evidence.append(item)
            elif len(closest) < _CLOSEST_DOCS:
                closest.append(item)
        # Sources that mention more of the question's terms first; ties keep search order.
        evidence.sort(key=lambda e: -len(e["matched_terms"]))
        evidence = evidence[:_EVIDENCE_DOCS]
        for i, item in enumerate(evidence, 1):
            item["rank"] = i
        out = {"question": question, "terms": terms, "demo": demo,
               "status": "evidence" if evidence else "insufficient",
               "evidence": evidence, "closest": [] if evidence else closest,
               "answer": None, "generation": self.answers.status()}
        if evidence and out["generation"]["available"]:
            result = self.answers.answer(question, evidence)
            if result is None:
                out["generation"] = self.answers.status()
            elif result.get("text"):
                out.update(status="answered", answer=result)
            elif result.get("insufficient"):
                out["status"] = "not_answered"
            else:
                out["generation_note"] = ("The generated answer cited no excerpt, so it is not "
                                          "shown. The sources are below.")
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        log_event(audit_logger, "ui_ask", principal=p.name, workspace="demo" if demo else "live",
                  status=out["status"], evidence=len(evidence))
        return _json(out)

    def _evidence_item(self, tools, who, hit: dict, terms: list[str], demo: bool) -> dict | None:  # noqa: ANN001
        doc = tools.get_document(hit["document_id"], principal=who)
        if not doc.get("found"):
            return None
        body = doc_body(doc.get("text") or "", doc.get("title") or "")
        lines = body.split("\n")
        lo = hit.get("line_start") or 1
        hi = hit.get("line_end") or len(lines)
        window = best_window(lines, lo, hi, terms)
        if not window["terms"] and terms:
            whole = best_window(lines, 1, len(lines), terms)
            if whole["terms"]:
                window = whole
        url = doc.get("url") or None
        return {
            "document_id": doc["document_id"],
            "title": doc.get("title") or "",
            "kind": doc.get("kind"),
            "source_type": doc.get("source_type"),
            "repo": doc.get("repo"),
            "path": doc.get("path"),
            "revision": doc.get("revision"),
            "state": doc.get("state"),
            "author": doc.get("author"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "indexed_at": doc.get("indexed_at"),
            "labels": doc.get("labels") or [],
            "imported_via": doc.get("imported_via"),
            "line_start": window["start"],
            "line_end": window["end"],
            "excerpt": {"lines": excerpt_lines(lines, window["start"], window["end"])},
            "matched_terms": window["terms"],
            "url": url,
            "link": permalink(url, revision=doc.get("revision"), path=doc.get("path"),
                              start=window["start"], end=window["end"]),
            "link_is_fictional": demo,
        }

    def document(self, request: Request, p: Principal, body: dict) -> Response:
        doc_id = request.query_params.get("id") or ""
        tools, demo = self._workspace(request.query_params.get("workspace"))
        doc = tools.get_document(doc_id, principal=self._tools_principal(p)) if doc_id else {}
        if not doc.get("found"):
            raise ApiError(404, "not_found", "This source is not in the index, or your access "
                                             "does not include it.")
        body_text = doc_body(doc.get("text") or "", doc.get("title") or "")
        lines = body_text.split("\n")
        start = _int(request.query_params.get("start"))
        end = _int(request.query_params.get("end")) or start
        keep = {k: doc.get(k) for k in ("document_id", "title", "kind", "source_type", "repo",
                                        "path", "revision", "state", "author", "created_at",
                                        "updated_at", "indexed_at", "revision_committed_at",
                                        "labels", "imported_via", "url")}
        return _json({**keep, "lines": lines[:20_000], "truncated": len(lines) > 20_000,
                      "focus": {"start": start, "end": end} if start else None,
                      "link": permalink(doc.get("url"), revision=doc.get("revision"),
                                        path=doc.get("path"), start=start, end=end),
                      "demo": demo, "notice": doc.get("notice")})

    # --- agents --------------------------------------------------------------------

    def _agent_rows(self, p: Principal) -> list[dict]:
        """Every credential the server knows, with what it may do and whether it was seen.

        ``observed`` means an authenticated MCP tool call was recorded for the token; creating
        a token or copying its setup is not a connection.
        """
        usage = self.usage.summary()
        rows = []
        if self.settings.mcp.auth_mode == "none":
            seen = usage.get("anonymous")
            rows.append({"name": "local", "configured": True, "observed": seen,
                         "admin": True, "you": True, "kind": "loopback", "role": "local",
                         "status": "active"})
            return rows
        meta = web_agents.load(self.settings)
        admins = set(self.settings.web.admin_principals)
        for name in sorted(self.settings.mcp.auth_tokens):
            m = meta.get(name) or {}
            role = "admin" if name in admins else (
                "agent" if m.get("kind") == "agent" else "reader")
            rows.append({"name": name, "configured": True, "observed": usage.get(name),
                         "admin": name in admins, "you": name == p.name, "kind": "token",
                         "role": role, "status": "active", "created_at": m.get("created_at"),
                         "created_by": m.get("created_by")})
        for name in sorted(set(usage) | set(meta)):
            if name in self.settings.mcp.auth_tokens or name in {"anonymous", "local-stdio"}:
                continue
            m = meta.get(name) or {}
            rows.append({"name": name, "configured": False, "observed": usage.get(name),
                         "admin": False, "you": False, "kind": "revoked",
                         "role": "agent" if m.get("kind") == "agent" else "reader",
                         "status": "revoked", "created_at": m.get("created_at"),
                         "revoked_at": m.get("revoked_at")})
        if "local-stdio" in usage:
            rows.append({"name": "local-stdio", "configured": True,
                         "observed": usage["local-stdio"], "admin": False, "you": False,
                         "kind": "stdio", "role": "local", "status": "active"})
        return rows

    def _mcp_url(self, request: Request) -> str:
        return str(request.base_url).rstrip("/") + "/mcp"

    def agents(self, request: Request, p: Principal, body: dict) -> Response:
        mode = self.settings.mcp.auth_mode
        can_issue = mode == "token" and bool(self.settings.mcp.auth_tokens_ref)
        reason = None
        if mode == "token" and not can_issue:
            reason = ("Tokens on this server come from the deployment (AQUIFER_MCP__AUTH_TOKENS"
                      ", e.g. Secrets Manager), so they are managed there.")
        url = self._mcp_url(request)
        web = self.settings.web
        return _json({
            "auth_mode": mode, "mcp_url": url,
            "network": {"access": web.network_access, "remote_mcp_url": web.remote_mcp_url or None,
                        "loopback": request.url.hostname in {"127.0.0.1", "localhost", "::1"}},
            "clients": web_agents.CLIENTS, "oauth_note": web_agents.OAUTH_ONLY_NOTE,
            "setup": web_agents.setup(url, "agent"),
            "agents": self._agent_rows(p), "admin": p.admin,
            "can_issue": can_issue and p.admin, "issue_reason": reason,
            "identity_note": ("Aquifer identifies callers by access token. It cannot tell which "
                              "person or which agent product holds a token, so give each agent "
                              "its own token."),
        })

    def agent_create(self, request: Request, p: Principal, body: dict) -> Response:
        name = str(body.get("name") or "").strip().lower()
        if not TOKEN_NAME_RE.fullmatch(name):
            raise ApiError(400, "bad_name", "Use 2-41 lowercase letters, digits or '-', "
                                            "starting with a letter (e.g. claude-code-alex).")
        ref = self.settings.mcp.auth_tokens_ref
        if self.settings.mcp.auth_mode != "token" or not ref:
            raise ApiError(400, "not_supported", "This server's tokens are managed by the "
                                                 "deployment.")
        tokens = self.settings.mcp.auth_tokens
        if name in tokens:
            raise ApiError(409, "exists", f"A token named {name} already exists.")
        if name in self.settings.web.admin_principals or name in {"local", "local-stdio",
                                                                  "anonymous"}:
            raise ApiError(400, "reserved", f"{name} is reserved (an admin or built-in "
                                            "principal). Choose another name.")
        from aquifer.core.secrets import store_secret

        value = secrets.token_urlsafe(36)
        store_secret(ref, json.dumps({**tokens, name: value}))
        tokens[name] = value  # live: the MCP endpoint accepts it now
        self.usage.forget(name)  # a reused name must not inherit a revoked credential's calls
        web_agents.record_issued(self.settings, name, p.name)
        self._audit("token_issued", p, token_name=name, kind="agent")
        url = self._mcp_url(request)
        # The value is returned once, for the person to store; setup snippets never inline it.
        return _json({"name": name, "token": value, "mcp_url": url,
                      "setup": web_agents.setup(url, name)}, 201)

    def agent_revoke(self, request: Request, p: Principal, body: dict) -> Response:
        name = request.path_params["name"]
        tokens = self.settings.mcp.auth_tokens
        if name not in tokens:
            raise ApiError(404, "not_found", f"No token named {name}.")
        if name in self.settings.web.admin_principals or name == p.name:
            raise ApiError(400, "protected", "Admin tokens and your own token cannot be "
                                             "revoked here.")
        from aquifer.core.secrets import store_secret

        remaining = {k: v for k, v in tokens.items() if k != name}
        store_secret(self.settings.mcp.auth_tokens_ref, json.dumps(remaining))
        tokens.pop(name, None)
        web_agents.record_revoked(self.settings, name, p.name)
        self._audit("token_revoked", p, token_name=name)
        return _json({"revoked": name})


def _compose_dependencies(text: str) -> list[str]:
    """Service names listed under ``depends_on:`` in a Compose file (list form)."""
    out: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "depends_on:":
            continue
        indent = len(line) - len(line.lstrip())
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent or not nxt.strip().startswith("- "):
                break
            name = nxt.strip()[2:].strip().strip("'\"")
            if re.fullmatch(r"[\w.-]{2,40}", name):
                out.append(name)
    return out


def _needed(terms: list[str]) -> int:
    """A source is evidence when it mentions at least half of the question's topic terms."""
    return max(1, (len(terms) + 1) // 2)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _adr_subject(title: str) -> str:
    """``ADR 0007: Store ledger balances in PostgreSQL`` → ``Store ledger balances in …``."""
    t = re.sub(r"^#+\s*", "", title or "").strip()
    return re.sub(r"^(ADR[- ]?\d+[:.]?\s*)", "", t, flags=re.I)[:90]


def _discovery_error(last_error: dict) -> dict:
    text = str(last_error.get("error") or "")
    if text.startswith("SourceAuthError"):
        action = ("GitHub rejected the token. Check a new read-only token under Sources → "
                  "GitHub → Replace token.")
        code = "token_rejected"
    elif text.startswith("SourceNotFoundError"):
        action = "A repository was not found, or the token cannot read it."
        code = "repo_not_found"
    elif text.startswith("RateLimitedError"):
        action = "GitHub rate-limited the sync; it is retried automatically."
        code = "rate_limited"
    else:
        action = "See the server log (aquifer status) for details."
        code = "sync_failed"
    return {"code": code, "message": text[:300], "at": last_error.get("at"), "action": action}


def _integrator_error(exc: Exception) -> ApiError:
    from aquifer.integrator.client import classify_failure

    err = classify_failure(exc)
    return ApiError(502, err.code, f"{err.code}: {err}")
