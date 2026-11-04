"""Known MCP providers: preconfigured URLs, authentication and (where verified) stream templates.

Every entry says what was actually verified and when. A provider without templates still works
through the generic path (`aquifer sources propose`), which samples the server's own tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aquifer.core.models import DocumentKind
from aquifer.integrator.models import DetailSpec, FieldMap, Pagination, StreamSpec


@dataclass
class Provider:
    id: str
    name: str
    url: str
    auth: str  # none | bearer | oauth
    verified: str
    headers: dict[str, str] = field(default_factory=dict)
    scope: str = ""
    credential_help: str = ""
    requires_client_registration: bool = False
    templates: dict = field(default_factory=dict)  # name -> fn(target: str) -> StreamSpec
    template_target: str = ""  # what a template is applied to (e.g. "owner/repo")
    notes: str = ""


def _stream_name(prefix: str, target: str) -> str:
    """``discussions`` + ``acme/payments`` → ``discussions-payments`` (repository name only;
    pass a different stream name in the plan if two owners share a repository name)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", target.split("/")[-1].lower()).strip("-")
    return f"{prefix}-{slug}"[:41].rstrip("-")


def _split_repo(target: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", target):
        raise ValueError(f"expected owner/repo, got {target!r}")
    owner, repo = target.split("/", 1)
    return owner, repo


def _github_discussions(target: str) -> StreamSpec:
    owner, repo = _split_repo(target)
    return StreamSpec(
        name=_stream_name("discussions", target), kind=DocumentKind.DISCUSSION,
        tool="list_discussions",
        args={"owner": owner, "repo": repo, "orderBy": "UPDATED_AT", "direction": "DESC"},
        items="discussions",
        pagination=Pagination(type="cursor", param="after", next="pageInfo.endCursor",
                              has_more="pageInfo.hasNextPage", size_param="perPage", size=50),
        fields=FieldMap(id="number", title="title", url="html_url", updated_at="updated_at",
                        created_at="created_at", author="user.login", labels=""),
        # The listing has no body: fetch it, and use the same call to confirm deletions.
        detail=DetailSpec(tool="get_discussion",
                          args={"owner": owner, "repo": repo, "discussionNumber": "{id}"},
                          body="body", not_found=r"could not resolve to a discussion"),
        enumeration="complete", ordered_by_updated_desc=True)


def _github_releases(target: str) -> StreamSpec:
    owner, repo = _split_repo(target)
    return StreamSpec(
        name=_stream_name("releases", target), kind=DocumentKind.RELEASE,
        tool="list_releases", args={"owner": owner, "repo": repo},
        items="", pagination=Pagination(type="page", param="page", start=1,
                                        size_param="perPage", size=50),
        # Releases expose no update time: every run lists them and compares content.
        fields=FieldMap(id="tag_name", title="name|tag_name", body="body", url="html_url",
                        created_at="published_at", author="author.login"),
        verify=DetailSpec(tool="get_release_by_tag",
                          args={"owner": owner, "repo": repo, "tag": "{id}"},
                          not_found=r"404 not found"),
        enumeration="complete")


PROVIDERS: dict[str, Provider] = {
    "github": Provider(
        id="github", name="GitHub (remote MCP server)", url="https://api.githubcopilot.com/mcp/",
        auth="bearer",
        headers={"X-MCP-Readonly": "true", "X-MCP-Toolsets": "repos,discussions"},
        credential_help=("A fine-grained personal access token with read-only access to the "
                         "repositories (Contents, Discussions: read). `gh auth token` also "
                         "works for a test."),
        verified=("2026-11-04, end to end: personal-access-token auth, server-side read-only "
                  "mode, 28 read-only tools, cursor and page pagination, not-found errors, "
                  "discussions and releases import, updates and deletions."),
        templates={"discussions": _github_discussions, "releases": _github_releases},
        template_target="owner/repo",
        notes=("Imports what the native GitHub connector does not: discussions and release "
               "notes. Issues, PRs and files keep using the native connector.")),
    "atlassian": Provider(
        id="atlassian", name="Atlassian Rovo MCP (Jira, Confluence)",
        url="https://mcp.atlassian.com/v1/mcp", auth="oauth",
        verified=("2026-11-04, metadata only: OAuth 2.1 with dynamic client registration, "
                  "PKCE S256 and refresh tokens. Tools not verified (needs an Atlassian Cloud "
                  "site); use `aquifer sources propose` to build streams from its tools."),
        notes="Your Atlassian organization admin may need to allow the Rovo MCP server."),
    "linear": Provider(
        id="linear", name="Linear", url="https://mcp.linear.app/mcp", auth="oauth",
        scope="read",
        verified=("2026-11-04, metadata only: OAuth 2.1 with dynamic client registration, "
                  "PKCE S256, refresh tokens, scopes read/write (Aquifer requests read). Tools "
                  "not verified.")),
    "notion": Provider(
        id="notion", name="Notion", url="https://mcp.notion.com/mcp", auth="oauth",
        verified=("2026-11-04, metadata only: OAuth 2.1 with dynamic client registration, "
                  "PKCE S256 and refresh tokens. Tools not verified.")),
    "sentry": Provider(
        id="sentry", name="Sentry", url="https://mcp.sentry.dev/mcp", auth="oauth",
        verified=("2026-11-04, metadata only: OAuth 2.1 with dynamic client registration, "
                  "PKCE S256 and refresh tokens. Tools not verified.")),
    "slack": Provider(
        id="slack", name="Slack", url="https://mcp.slack.com/mcp", auth="oauth",
        requires_client_registration=True,
        verified=("2026-11-04, metadata only: OAuth without dynamic client registration — you "
                  "must create a Slack app and pass its client id (and secret). Tools not "
                  "verified."),
        notes="Slack's terms restrict storing message data; check them before importing."),
}


def get_provider(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise KeyError(f"unknown provider {provider_id!r}; known: {', '.join(PROVIDERS)} "
                       "(or use --url for any MCP server)") from None
