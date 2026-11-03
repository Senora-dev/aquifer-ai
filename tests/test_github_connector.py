import base64
from datetime import datetime

import pytest

from aquifer.connectors.github import GitHubConnector
from aquifer.core.config import GitHubSettings
from aquifer.core.errors import RateLimitedError, SourceAuthError, TransientError
from aquifer.core.models import DocumentKind, FetchJob, IndexedState, Source, SourceType


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

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
        if path not in self.routes:
            return FakeResponse({"message": "Not Found"}, status_code=404)
        route = self.routes[path]
        if isinstance(route, FakeResponse):
            return route
        if callable(route):
            return route(params or {})
        if isinstance(route, list):
            page = (params or {}).get("page", 1)
            payload = route[page - 1] if page - 1 < len(route) else []
            return FakeResponse(payload)
        return FakeResponse(route)


def _connector(routes, page_size=2, index_lookup=None, **settings_kw):
    settings = GitHubSettings(page_size=page_size, repo_allowlist=["o/r"], **settings_kw)
    return GitHubConnector(settings=settings, token="t", http_client=FakeHttp(routes),
                           index_lookup=index_lookup, sleep=lambda s: None)


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
    assert ("o/r2", DocumentKind.FILE) in kinds
    assert len(jobs) == 2 * 4


def test_discover_without_file_globs_skips_file_jobs():
    settings = GitHubSettings(repo_allowlist=["o/r"], file_globs=[])
    conn = GitHubConnector(settings=settings, http_client=FakeHttp({}))
    jobs = list(conn.discover(Source(id="gh", type=SourceType.GITHUB, config={})))
    assert {j.kind for j in jobs} == {DocumentKind.README, DocumentKind.ISSUE, DocumentKind.PR}


def test_fetch_readme_decodes_base64():
    content = base64.b64encode(b"# Hello\nProject docs").decode()
    conn = _connector({"/repos/o/r/readme": {"content": content, "html_url": "u"}})
    docs, nxt = conn.fetch(_job(DocumentKind.README))
    assert nxt is None
    assert len(docs) == 1
    assert "Hello" in docs[0].body
    assert docs[0].kind == DocumentKind.README


def test_fetch_readme_missing_reconciles_it_away():
    conn = _connector({})  # every path 404s
    docs, nxt, recon = conn.fetch_with_reconciliation(_job(DocumentKind.README))
    assert docs == [] and nxt is None
    # A removed README must be removed from the index too.
    assert recon.scope == {"repo": "o/r", "kind": "readme"}
    assert recon.live_document_ids == set()


def test_fetch_readme_other_errors_are_not_treated_as_missing():
    conn = _connector({"/repos/o/r/readme": FakeResponse({}, status_code=401)})
    with pytest.raises(SourceAuthError):
        conn.fetch(_job(DocumentKind.README))


def test_fetch_readme_records_path_and_blob_sha():
    content = base64.b64encode(b"# Hi").decode()
    conn = _connector({"/repos/o/r/readme": {"content": content, "html_url": "u",
                                              "path": "README.md", "sha": "blob1"}})
    docs, _ = conn.fetch(_job(DocumentKind.README))
    assert docs[0].path == "README.md"
    assert docs[0].metadata["blob_sha"] == "blob1"
    assert docs[0].revision is None  # a blob SHA is not a commit; never mislabelled


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


def test_issues_and_pulls_are_paged_newest_first():
    conn = _connector({"/repos/o/r/issues": [[]], "/repos/o/r/pulls": [[]]})
    conn.fetch(_job(DocumentKind.ISSUE))
    conn.fetch(_job(DocumentKind.PR))
    assert all(params["direction"] == "desc" for _, params in conn.http.requests)


def test_pulls_stop_paging_once_older_than_watermark():
    pulls = [
        {"number": 11, "title": "new", "labels": [], "updated_at": "2024-06-01T00:00:00Z"},
        {"number": 10, "title": "old", "labels": [], "updated_at": "2023-01-01T00:00:00Z"},
    ]
    conn = _connector({"/repos/o/r/pulls": [pulls, pulls]}, page_size=2)
    job = _job(DocumentKind.PR, since=datetime.fromisoformat("2024-01-01T00:00:00Z"))
    docs, nxt = conn.fetch(job)
    assert [d.external_id for d in docs] == ["o/r#11"]
    assert nxt is None  # full page, but everything after it is older than the watermark


def test_merged_pr_state_is_recorded():
    pulls = [{"number": 5, "title": "x", "labels": [], "state": "closed",
              "merged_at": "2024-01-01T00:00:00Z"}]
    docs, _ = _connector({"/repos/o/r/pulls": [pulls]}, page_size=10).fetch(_job(DocumentKind.PR))
    assert docs[0].state == "merged"


def test_malformed_item_does_not_fail_the_page():
    items = [{"title": "no number"}, {"number": 2, "title": "ok", "labels": []}]
    docs, _ = _connector({"/repos/o/r/issues": [items]}, page_size=10).fetch(
        _job(DocumentKind.ISSUE)
    )
    assert [d.external_id for d in docs] == ["o/r#2"]


def test_server_errors_are_retried_then_succeed():
    calls = {"n": 0}

    def flaky(params):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse({}, status_code=502)
        return FakeResponse([{"number": 1, "title": "x", "labels": []}])

    docs, _ = _connector({"/repos/o/r/issues": flaky}, page_size=10).fetch(
        _job(DocumentKind.ISSUE)
    )
    assert len(docs) == 1 and calls["n"] == 3


def test_persistent_server_errors_raise_transient():
    conn = _connector({"/repos/o/r/issues": FakeResponse({}, status_code=503)})
    with pytest.raises(TransientError):
        conn.fetch(_job(DocumentKind.ISSUE))


def test_short_rate_limit_is_waited_out_inline():
    calls = {"n": 0}

    def limited(params):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, status_code=429, headers={"retry-after": "2"})
        return FakeResponse([])

    _connector({"/repos/o/r/issues": limited}).fetch(_job(DocumentKind.ISSUE))
    assert calls["n"] == 2


def test_long_rate_limit_raises_for_requeue():
    resp = FakeResponse({}, status_code=403, headers={"x-ratelimit-remaining": "0",
                                                       "x-ratelimit-reset": "9999999999"})
    with pytest.raises(RateLimitedError) as info:
        _connector({"/repos/o/r/issues": resp}).fetch(_job(DocumentKind.ISSUE))
    assert info.value.retry_after > 60


def _file_routes(tree_entries, truncated=False, blobs=None):
    routes = {
        "/repos/o/r": {"full_name": "o/r", "default_branch": "main"},
        "/repos/o/r/commits/main": {
            "sha": "c0ffee", "commit": {"tree": {"sha": "t1"},
                                        "committer": {"date": "2024-05-01T00:00:00Z"}},
        },
        "/repos/o/r/git/trees/t1": {"tree": tree_entries, "truncated": truncated},
    }
    for sha, text in (blobs or {}).items():
        routes[f"/repos/o/r/git/blobs/{sha}"] = {
            "content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
    return routes


def test_fetch_files_reads_matching_files_at_a_pinned_commit():
    tree = [
        {"path": "infra/main.tf", "type": "blob", "sha": "b1", "size": 10},
        {"path": "src/app.py", "type": "blob", "sha": "b2", "size": 10},
        {"path": "infra", "type": "tree", "sha": "t2"},
    ]
    conn = _connector(_file_routes(tree, blobs={"b1": 'module "x" {}'}))
    docs, nxt, recon = conn.fetch_with_reconciliation(_job(DocumentKind.FILE))
    assert [d.path for d in docs] == ["infra/main.tf"]
    doc = docs[0]
    assert doc.revision == "c0ffee"
    assert doc.url == "https://github.com/o/r/blob/c0ffee/infra/main.tf"
    assert doc.metadata["blob_sha"] == "b1"
    assert doc.updated_at is None  # file change time is unknown; never invented
    assert nxt is None
    assert recon.live_document_ids == {"github:o/r:file:infra/main.tf"}


def test_fetch_files_skips_unchanged_blobs_without_downloading():
    from aquifer.core.models import file_content_hash

    tree = [{"path": "main.tf", "type": "blob", "sha": "b1", "size": 10}]
    doc_id = "github:o/r:file:main.tf"

    def lookup(ids):
        return {doc_id: IndexedState(document_id=doc_id,
                                     content_hash=file_content_hash("b1", "main.tf"),
                                     extraction_status="ok")}

    conn = _connector(_file_routes(tree), index_lookup=lookup)  # no blob route → 404 if fetched
    docs, _, recon = conn.fetch_with_reconciliation(_job(DocumentKind.FILE))
    assert docs == []
    assert doc_id in recon.live_document_ids


def test_fetch_files_truncated_tree_never_reconciles():
    tree = [{"path": "main.tf", "type": "blob", "sha": "b1", "size": 10}]
    conn = _connector(_file_routes(tree, truncated=True, blobs={"b1": "x"}))
    _, _, recon = conn.fetch_with_reconciliation(_job(DocumentKind.FILE))
    assert recon is None


def test_fetch_files_paginates_with_the_same_commit():
    tree = [{"path": f"m{i}.tf", "type": "blob", "sha": f"b{i}", "size": 1} for i in range(3)]
    routes = _file_routes(tree, blobs={f"b{i}": "x" for i in range(3)})
    conn = _connector(routes, files_per_page=2)
    docs, nxt, _ = conn.fetch_with_reconciliation(_job(DocumentKind.FILE))
    assert len(docs) == 2 and nxt.cursor.startswith("c0ffee|t1|")
    docs2, nxt2, recon2 = conn.fetch_with_reconciliation(nxt)
    assert [d.path for d in docs2] == ["m2.tf"] and nxt2 is None and recon2 is None
    assert docs2[0].revision == "c0ffee"


def test_fetch_files_skips_oversized_and_binary_files():
    tree = [
        {"path": "big.tf", "type": "blob", "sha": "b1", "size": 10_000_000},
        {"path": "bin.tf", "type": "blob", "sha": "b2", "size": 3},
    ]
    routes = _file_routes(tree)
    routes["/repos/o/r/git/blobs/b2"] = {"content": base64.b64encode(b"\x00\x01").decode()}
    docs, _ = _connector(routes).fetch(_job(DocumentKind.FILE))
    assert docs == []


def test_validate_access_surfaces_bad_token():
    conn = _connector({"/rate_limit": FakeResponse({}, status_code=401)})
    with pytest.raises(SourceAuthError, match="401"):
        conn.validate_access()


def test_token_whitespace_is_stripped():
    conn = GitHubConnector(settings=GitHubSettings(), token="  abc123\n")
    assert conn._token == "abc123"


def test_invalid_token_is_rejected_without_echoing_it():
    secret = "ghp_" + "x" * 20 + " leaked"
    with pytest.raises(SourceAuthError) as info:
        GitHubConnector(settings=GitHubSettings(), token=secret)
    assert secret not in str(info.value) and "xxxx" not in str(info.value)


def test_http_layer_errors_never_carry_header_values():
    from aquifer.connectors.github import SourceRequestError

    class LeakyHttp:
        def get(self, path, params=None):
            raise ValueError("Illegal header value b'Bearer ghp_SECRETSECRET'")

    conn = GitHubConnector(settings=GitHubSettings(request_retries=0), token="t",
                           http_client=LeakyHttp())
    with pytest.raises(SourceRequestError) as info:
        conn.fetch(_job(DocumentKind.ISSUE))
    assert "SECRET" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
