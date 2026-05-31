import base64
from datetime import datetime

from aquifer.connectors.github import GitHubConnector
from aquifer.core.config import GitHubSettings
from aquifer.core.models import DocumentKind, FetchJob, Source, SourceType


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHttp:
    """Routes GET calls by path; for list endpoints, indexes pages by the ``page`` param."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, path, params=None):
        self.requests.append((path, params or {}))
        route = self.routes[path]
        if isinstance(route, list):
            page = (params or {}).get("page", 1)
            payload = route[page - 1] if page - 1 < len(route) else []
            return FakeResponse(payload)
        return FakeResponse(route)


def _connector(routes, page_size=2):
    settings = GitHubSettings(page_size=page_size, repo_allowlist=["o/r"])
    return GitHubConnector(settings=settings, token="t", http_client=FakeHttp(routes))


def _job(kind, **kw):
    return FetchJob(source_id="gh", source_type=SourceType.GITHUB, repo="o/r", kind=kind, **kw)


def test_discover_emits_job_per_repo_and_kind():
    conn = _connector({})
    source = Source(id="gh", type=SourceType.GITHUB, config={"repos": ["o/r", "o/r2"]})
    jobs = list(conn.discover(source))
    kinds = {(j.repo, j.kind) for j in jobs}
    assert ("o/r", DocumentKind.README) in kinds
    assert ("o/r", DocumentKind.ISSUE) in kinds
    assert ("o/r2", DocumentKind.PR) in kinds
    assert len(jobs) == 2 * 3


def test_fetch_readme_decodes_base64():
    content = base64.b64encode(b"# Hello\nProject docs").decode()
    conn = _connector({"/repos/o/r/readme": {"content": content, "html_url": "u"}})
    docs, nxt = conn.fetch(_job(DocumentKind.README))
    assert nxt is None
    assert len(docs) == 1
    assert "Hello" in docs[0].body
    assert docs[0].kind == DocumentKind.README


def test_fetch_readme_missing_is_graceful():
    class Boom(FakeHttp):
        def get(self, path, params=None):
            raise RuntimeError("404")

    conn = GitHubConnector(settings=GitHubSettings(), token="t", http_client=Boom({}))
    docs, nxt = conn.fetch(_job(DocumentKind.README))
    assert docs == [] and nxt is None


def test_fetch_issues_skips_prs_and_paginates():
    page1 = [
        {"number": 1, "title": "Bug", "body": "broken", "html_url": "u1",
         "user": {"login": "alice"}, "labels": [{"name": "bug"}],
         "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z",
         "state": "open", "comments": 3},
        # This entry is actually a PR surfaced by the issues endpoint; must be skipped.
        {"number": 2, "title": "PR", "pull_request": {"url": "x"}, "labels": []},
    ]
    conn = _connector({"/repos/o/r/issues": [page1]}, page_size=2)
    docs, nxt = conn.fetch(_job(DocumentKind.ISSUE))

    assert [d.external_id for d in docs] == ["o/r#1"]
    assert docs[0].author == "alice"
    assert docs[0].labels == ["bug"]
    # Full page (2 == page_size) ⇒ a successor job on page 2.
    assert nxt is not None and nxt.cursor == "2" and nxt.kind == DocumentKind.ISSUE


def test_fetch_issues_short_page_has_no_successor():
    one_issue = [[{"number": 1, "title": "x", "labels": []}]]
    conn = _connector({"/repos/o/r/issues": one_issue}, page_size=2)
    _, nxt = conn.fetch(_job(DocumentKind.ISSUE))
    assert nxt is None


def test_fetch_pulls_filters_by_since_clientside():
    pulls = [
        {"number": 10, "title": "old", "labels": [], "updated_at": "2023-01-01T00:00:00Z"},
        {"number": 11, "title": "new", "labels": [], "updated_at": "2024-06-01T00:00:00Z"},
    ]
    conn = _connector({"/repos/o/r/pulls": [pulls]}, page_size=10)
    job = _job(DocumentKind.PR, since=datetime.fromisoformat("2024-01-01T00:00:00Z"))
    docs, _ = conn.fetch(job)
    assert [d.external_id for d in docs] == ["o/r#11"]
    assert docs[0].kind == DocumentKind.PR
