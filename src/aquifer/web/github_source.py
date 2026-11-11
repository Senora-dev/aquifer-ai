"""The native GitHub source, configured at run time from the UI.

The deployment's environment (``AQUIFER_GITHUB__REPO_ALLOWLIST``, ``AQUIFER_GITHUB__TOKEN_SECRET``)
stays authoritative: repositories chosen in the UI are added to it, never replace it, and a
token configured by the deployment is used as is. The UI's choices live in
``<node.data_dir>/github-source.json`` (no secrets: the token is stored as a secret reference).

Indexing uses the existing discovery, worker and pipeline code; this module only changes which
repositories and which credential that code sees.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from aquifer.core.config import GitHubSettings, Settings

REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")

# Classic / OAuth scopes that allow changing something. Aquifer never writes, but the person
# connecting should know when a token can.
WRITE_SCOPES = frozenset({
    "repo", "public_repo", "repo:status", "repo_deployment", "write:org", "admin:org",
    "workflow", "delete_repo", "write:packages", "admin:repo_hook", "admin:org_hook",
    "write:discussion", "admin:public_key", "admin:gpg_key", "user", "gist", "project",
    "codespace", "write:repo_hook",
})

# What the native connector reads, shown before connecting.
READS = [
    "README of each repository",
    "Issues: title, body, state, labels, author, timestamps",
    "Pull requests: title, body, state, labels, author, timestamps",
]

ACCESS_NOTE = ("Everything indexed is readable by every Aquifer access token of this deployment "
               "and by everyone signed in to this UI (one trust domain), regardless of "
               "per-user permissions in GitHub.")


class GitHubAccessError(Exception):
    """GitHub refused or could not be reached; ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def config_path(settings: Settings) -> Path:
    return Path(settings.node.data_dir) / "github-source.json"


def load(settings: Settings) -> dict:
    path = config_path(settings)
    if not path.is_file():
        return {"repos": [], "token_ref": ""}
    data = json.loads(path.read_text())
    data.setdefault("repos", [])
    data.setdefault("token_ref", "")
    return data


def _save(settings: Settings, data: dict) -> None:
    path = config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def env_github() -> GitHubSettings:
    """The deployment's own GitHub settings (from the environment only).

    Read through ``Settings``: ``GitHubSettings()`` alone does not see the nested
    ``AQUIFER_GITHUB__*`` variables and would return an empty allowlist.
    """
    return Settings().github


def apply(settings: Settings) -> None:
    """Make ``settings.github`` the union of the deployment's and the UI's configuration."""
    env = env_github()
    ui = load(settings)
    repos = list(env.repo_allowlist)
    repos += [r for r in ui["repos"] if r not in repos]
    settings.github.repo_allowlist = repos
    settings.github.token_secret = env.token_secret or ui.get("token_ref") or ""


def describe(settings: Settings) -> dict:
    env = env_github()
    ui = load(settings)
    return {
        "deployment_repos": list(env.repo_allowlist),
        "ui_repos": [r for r in ui["repos"] if r not in env.repo_allowlist],
        "token_source": "deployment" if env.token_secret else (
            "ui" if ui.get("token_ref") else None),
        "connected_by": ui.get("connected_by"),
        "connected_at": ui.get("connected_at"),
        "token_type": ui.get("token_type"),
        "login": ui.get("login"),
    }


def connect(settings: Settings, repos: list[str], *, token_ref: str | None, principal: str,
            probe: dict) -> list[str]:
    """Record the chosen repositories (and the token's reference). Returns the new ones."""
    bad = [r for r in repos if not REPO_RE.fullmatch(r)]
    if bad:
        raise ValueError(f"not an owner/repo name: {bad}")
    data = load(settings)
    current = set(data["repos"]) | set(env_github().repo_allowlist)
    added = [r for r in repos if r not in current]
    data["repos"] = sorted(set(data["repos"]) | set(repos))
    if token_ref:
        data["token_ref"] = token_ref
    data.update(connected_by=principal, connected_at=datetime.now(UTC).isoformat(),
                token_type=probe.get("token_type"), login=probe.get("login"))
    _save(settings, data)
    apply(settings)
    return added


def remove_repo(settings: Settings, repo: str) -> bool:
    data = load(settings)
    if repo not in data["repos"]:
        return False
    data["repos"] = [r for r in data["repos"] if r != repo]
    _save(settings, data)
    apply(settings)
    return True


# --- talking to GitHub -------------------------------------------------------------------

def token_type(token: str) -> str:
    for prefix, kind in (("github_pat_", "fine-grained"), ("ghp_", "classic"),
                         ("gho_", "oauth"), ("ghu_", "app-user"), ("ghs_", "app-installation")):
        if token.startswith(prefix):
            return kind
    return "unknown"


def clean_token(token: str) -> str:
    token = (token or "").strip()
    if not token or len(token) > 255 or any(c.isspace() for c in token):
        raise GitHubAccessError("token_malformed", "That does not look like a GitHub token.")
    return token


def probe(token: str, settings: Settings, *, http=None, max_pages: int = 5) -> dict:  # noqa: ANN001
    """Check a token and list the repositories it can read. Never returns the token."""
    import httpx

    client = http or httpx.Client(
        base_url=settings.github.api_url, timeout=20.0, follow_redirects=True,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "Authorization": f"Bearer {token}"})

    def get(path: str, params: dict | None = None):  # noqa: ANN202
        try:
            resp = client.get(path, params=params)
        except Exception:  # noqa: BLE001 - the message could carry headers; never chain it
            raise GitHubAccessError(
                "github_unreachable",
                f"Could not reach GitHub at {settings.github.api_url}. Check the server's "
                "network access and try again.") from None
        if resp.status_code == 401:
            raise GitHubAccessError(
                "token_rejected", "GitHub rejected this token (401). It may be mistyped, "
                "expired or revoked. Create a new read-only token and try again.")
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubAccessError("rate_limited",
                                    "GitHub's rate limit for this token is exhausted. Try again "
                                    "after it resets.")
        return resp

    try:
        user = get("/user")
        kind = token_type(token)
        scopes_header = user.headers.get("x-oauth-scopes")
        scopes = ([s.strip() for s in scopes_header.split(",") if s.strip()]
                  if scopes_header is not None else None)
        login = None
        repos: list[dict] = []
        truncated = False
        if user.status_code == 200:
            login = (user.json() or {}).get("login")
            for page in range(1, max_pages + 1):
                resp = get("/user/repos", {"per_page": 100, "page": page, "sort": "pushed"})
                if resp.status_code != 200:
                    raise GitHubAccessError("github_error",
                                            f"GitHub answered {resp.status_code} when listing "
                                            "repositories.")
                items = resp.json() or []
                repos += items
                if len(items) < 100:
                    break
            else:
                truncated = True
        else:
            # App installation tokens cannot read /user.
            resp = get("/installation/repositories", {"per_page": 100})
            if resp.status_code != 200:
                raise GitHubAccessError("github_error",
                                        f"GitHub answered {user.status_code} for this token.")
            repos = (resp.json() or {}).get("repositories") or []
    finally:
        if http is None:
            client.close()

    out_repos = []
    for r in repos:
        if not isinstance(r, dict) or not REPO_RE.fullmatch(r.get("full_name") or ""):
            continue
        perms = r.get("permissions") or {}
        out_repos.append({
            "full_name": r["full_name"],
            "private": bool(r.get("private")),
            "archived": bool(r.get("archived")),
            "description": (r.get("description") or "")[:200],
            "pushed_at": r.get("pushed_at"),
            "html_url": r.get("html_url"),
            "role_can_write": bool(perms.get("push") or perms.get("admin")),
        })
    write_scopes = sorted(set(scopes or []) & WRITE_SCOPES)
    return {
        "login": login,
        "token_type": kind,
        "scopes": scopes,
        "write_scopes": write_scopes,
        "repos": out_repos,
        "truncated": truncated,
    }


def access_review(probe_result: dict, settings: Settings) -> dict:
    """What connecting means, from facts about the token and Aquifer's configuration."""
    kind = probe_result.get("token_type")
    notes = []
    if probe_result.get("write_scopes"):
        notes.append({"level": "warning", "text": (
            "This token can also write (scopes: " + ", ".join(probe_result["write_scopes"])
            + "). Aquifer only reads, but a fine-grained token with read-only Contents, Issues, "
            "Pull requests and Metadata limits the damage if it leaks.")})
    elif kind == "fine-grained":
        notes.append({"level": "info", "text": (
            "Fine-grained token: GitHub does not report its permissions through the API. "
            "Aquifer needs read-only Contents, Issues, Pull requests and Metadata.")})
    elif probe_result.get("scopes") == []:
        notes.append({"level": "info", "text": "This token has no scopes: it can read public "
                                               "repositories only."})
    return {
        "reads": READS + [f"Files matching: {', '.join(settings.github.file_globs)}"
                          if settings.github.file_globs else "No repository files"],
        "writes": "Nothing. Aquifer only sends read requests to GitHub.",
        "stored": "The token is stored on this server as a secret reference "
                  "(never shown again, never sent to agents).",
        "visibility": ACCESS_NOTE,
        "schedule_s": settings.node.sync_interval_s,
        "notes": notes,
    }


# --- content preview (before anything is indexed) ------------------------------------------

PREVIEW_MAX_REPOS = 10
PREVIEW_SAMPLES = 5


def preview_content(token: str, settings: Settings, repos: list[str], *, http=None) -> dict:  # noqa: ANN001
    """What indexing would read from each repository, from read-only GitHub calls.

    The same rules as indexing: the README, files matching ``github.file_globs`` at the default
    branch head (capped at ``max_files_per_repo``), issues and pull requests. Issue and PR counts
    come from the first page only, so they are reported as "at least" when the page is full.
    Nothing is stored or indexed.
    """
    import httpx

    from aquifer.connectors.base import path_matches

    client = http or httpx.Client(
        base_url=settings.github.api_url, timeout=20.0, follow_redirects=True,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    auth = {"Authorization": f"Bearer {token}"}
    globs = tuple(settings.github.file_globs)

    def get(path: str, params: dict | None = None):  # noqa: ANN202
        try:
            return client.get(path, params=params, headers=auth)
        except Exception:  # noqa: BLE001 - never chain: the exception could carry headers
            raise GitHubAccessError(
                "github_unreachable", f"Could not reach GitHub at {settings.github.api_url}."
            ) from None

    def listing(repo: str, kind: str) -> dict:
        resp = get(f"/repos/{repo}/{kind}", {"state": "all", "per_page": 100, "sort": "updated",
                                             "direction": "desc"})
        if resp.status_code != 200:
            return {"count": 0, "at_least": False, "sample": [], "error": resp.status_code}
        items = resp.json() or []
        if kind == "issues":  # the issues endpoint also returns pull requests
            items = [i for i in items if "pull_request" not in i]
        return {"count": len(items), "at_least": len(resp.json() or []) >= 100,
                "sample": [{"number": i.get("number"), "title": (i.get("title") or "")[:140],
                            "state": i.get("state")} for i in items[:PREVIEW_SAMPLES]]}

    out = []
    try:
        for repo in repos[:PREVIEW_MAX_REPOS]:
            if not REPO_RE.fullmatch(repo):
                continue
            meta = get(f"/repos/{repo}")
            if meta.status_code != 200:
                out.append({"repo": repo, "error": f"GitHub answered {meta.status_code} for this "
                                                   "repository."})
                continue
            branch = (meta.json() or {}).get("default_branch") or "main"
            readme = get(f"/repos/{repo}/readme").status_code == 200
            files: list[str] = []
            truncated = False
            if globs:
                tree = get(f"/repos/{repo}/git/trees/{branch}", {"recursive": "1"})
                if tree.status_code == 200:
                    body = tree.json() or {}
                    truncated = bool(body.get("truncated"))
                    files = sorted(e["path"] for e in body.get("tree", [])
                                   if e.get("type") == "blob" and path_matches(e.get("path", ""),
                                                                                globs))
            cap = settings.github.max_files_per_repo
            out.append({
                "repo": repo, "branch": branch, "readme": readme,
                "files": {"count": min(len(files), cap), "matched": len(files),
                          "capped": len(files) > cap, "tree_truncated": truncated,
                          "sample": files[:PREVIEW_SAMPLES * 2]},
                "issues": listing(repo, "issues"),
                "pulls": listing(repo, "pulls"),
            })
    finally:
        if http is None:
            client.close()
    return {"repos": out, "omitted": max(0, len(repos) - PREVIEW_MAX_REPOS),
            "file_globs": list(globs)}
