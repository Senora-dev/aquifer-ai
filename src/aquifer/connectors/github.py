"""GitHub connector.

Implements :class:`aquifer.core.interfaces.Connector` for GitHub, normalizing repository
READMEs, issues, pull requests and selected repository files into :class:`Document` objects.
Discussions require the GraphQL API and are deferred.

Pagination is page-based: each fetch returns one page plus a successor ``FetchJob`` (or
``None``), so the ingestion worker processes one bounded page per Lambda invocation.

Reliability:

- 5xx and network errors are retried in-process with bounded exponential backoff.
- Rate limits (403/429 with ``x-ratelimit-remaining: 0`` or ``retry-after``) are slept through
  when short; otherwise :class:`RateLimitedError` tells the worker to re-enqueue with a delay.
- 401 raises :class:`SourceAuthError`; 404 raises :class:`SourceNotFoundError` — never treated
  as "empty" except where absence is meaningful (a repository without a README).
- Redirects are followed, so renamed repositories keep working (and the rename is logged).
- Issues and PRs are paged newest-first (``direction=desc``): an item updated mid-pagination
  moves to an already-read page and is picked up by the next run, instead of shifting an unread
  item out of the window. PR paging stops once a page is older than the watermark.

Files (``DocumentKind.FILE``) are read from one pinned commit of the default branch: the commit
SHA is carried in the cursor so every page of one run sees the same snapshot, and each document
records that SHA as its revision. Unchanged files (same git blob SHA as indexed) are skipped
before their content is downloaded when an index lookup is provided.

The httpx client is created lazily and can be injected for testing.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from aquifer.connectors.base import (
    cursor_to_page,
    next_page_cursor,
    parse_github_timestamp,
    path_matches,
)
from aquifer.core.config import GitHubSettings, get_settings
from aquifer.core.errors import (
    RateLimitedError,
    SourceAuthError,
    SourceNotFoundError,
    TransientError,
    is_transient,
    log_event,
    retry_call,
)
from aquifer.core.interfaces import Connector
from aquifer.core.models import (
    Document,
    DocumentKind,
    ExtractionStatus,
    FetchJob,
    IndexedState,
    Reconciliation,
    Source,
    SourceType,
    file_content_hash,
)

logger = logging.getLogger(__name__)

# Kinds this connector knows how to fetch, in discovery order.
_DISCOVERED_KINDS = (DocumentKind.README, DocumentKind.ISSUE, DocumentKind.PR)

IndexLookup = Callable[[Sequence[str]], dict[str, IndexedState]]


class GitHubConnector(Connector):
    source_type = SourceType.GITHUB.value

    def __init__(
        self,
        settings: GitHubSettings | None = None,
        token: str | None = None,
        http_client: Any | None = None,
        index_lookup: IndexLookup | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings().github
        self._token = _clean_token(token)
        self._http = http_client
        self._index_lookup = index_lookup
        self._sleep = sleep

    @property
    def http(self) -> Any:
        if self._http is None:
            import httpx

            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._http = httpx.Client(
                base_url=self._settings.api_url,
                headers=headers,
                timeout=30.0,
                follow_redirects=True,  # renamed/transferred repositories answer 301
            )
        return self._http

    # --- discovery -------------------------------------------------------

    def discover(self, source: Source) -> Iterable[FetchJob]:
        """Emit the initial fetch jobs: one per (repo, kind)."""
        repos = source.config.get("repos") or self._settings.repo_allowlist
        since = source.config.get("since")
        kinds = list(_DISCOVERED_KINDS)
        if self._settings.file_globs:
            kinds.append(DocumentKind.FILE)
        for repo in repos:
            for kind in kinds:
                yield FetchJob(
                    source_id=source.id,
                    source_type=SourceType.GITHUB,
                    repo=repo,
                    kind=kind,
                    cursor=None,
                    # Files are synced by content hash, not by time.
                    since=None if kind == DocumentKind.FILE else since,
                )

    def validate_access(self) -> dict:
        """Cheap credential check (``/rate_limit`` does not count against the limit)."""
        return self._get("/rate_limit")

    # --- fetch -----------------------------------------------------------

    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        documents, successor, _ = self.fetch_with_reconciliation(job)
        return documents, successor

    def fetch_with_reconciliation(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        if job.kind == DocumentKind.README:
            return self._fetch_readme(job)
        if job.kind == DocumentKind.ISSUE:
            return (*self._fetch_issues(job), None)
        if job.kind == DocumentKind.PR:
            return (*self._fetch_pulls(job), None)
        if job.kind == DocumentKind.FILE:
            return self._fetch_files(job)
        raise ValueError(f"GitHubConnector cannot fetch kind {job.kind!r}")

    # --- HTTP ------------------------------------------------------------

    def _request_once(self, path: str, params: dict | None) -> Any:
        try:
            response = self.http.get(path, params=params)
        except Exception as exc:
            # ``from None``: HTTP-layer exception messages can include request headers (and so
            # the token); never chain them into logs or Lambda error responses.
            if is_transient(exc):
                raise TransientError(f"GitHub request failed: {type(exc).__name__}") from None
            raise SourceRequestError(f"GitHub request failed: {type(exc).__name__}") from None
        status = getattr(response, "status_code", 200)
        headers = getattr(response, "headers", {}) or {}
        if status == 401:
            raise SourceAuthError(
                "GitHub rejected the token (401). Store a valid fine-grained token in the "
                "secret named by AQUIFER_GITHUB__TOKEN_SECRET."
            )
        if status in (403, 429):
            retry_after = headers.get("retry-after")
            remaining = headers.get("x-ratelimit-remaining")
            if status == 429 or retry_after is not None or remaining == "0":
                if retry_after is not None:
                    wait = float(retry_after)
                else:
                    reset = float(headers.get("x-ratelimit-reset") or time.time() + 60)
                    wait = max(1.0, reset - time.time())
                raise RateLimitedError("GitHub rate limit", retry_after=wait)
            raise SourceAuthError(f"GitHub denied access (403) to {path}")
        if status == 404:
            raise SourceNotFoundError(path)
        if status >= 500:
            raise TransientError(f"GitHub {status} for {path}")
        response.raise_for_status()
        return response

    def _get_response(self, path: str, params: dict | None = None) -> Any:
        max_inline = self._settings.max_inline_rate_limit_wait_s

        def retryable(exc: BaseException) -> bool:
            if isinstance(exc, RateLimitedError):
                return exc.retry_after <= max_inline
            return isinstance(exc, TransientError)

        return retry_call(
            lambda: self._request_once(path, params),
            attempts=self._settings.request_retries + 1,
            base_delay=1.0,
            max_delay=max(8.0, float(max_inline)),
            retryable=retryable,
            sleep=self._sleep,
            operation="github_get",
            logger=logger,
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._get_response(path, params).json()

    # --- README ----------------------------------------------------------

    def _fetch_readme(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        try:
            payload = self._get(f"/repos/{job.repo}/readme")
        except SourceNotFoundError:
            # No README (or it was removed): make sure none stays indexed.
            return [], None, Reconciliation(
                scope={"repo": job.repo, "kind": DocumentKind.README.value},
                live_document_ids=set(),
                reason="readme_not_found",
            )
        content = _decode_content(payload)
        if content is None:
            log_event(logger, "readme_undecodable", level=logging.WARNING, repo=job.repo)
            return [], None, None
        metadata = {}
        if payload.get("sha"):
            metadata["blob_sha"] = payload["sha"]
        doc = Document(
            id=Document.make_id(SourceType.GITHUB, job.repo, DocumentKind.README, "readme"),
            source_type=SourceType.GITHUB,
            external_id=f"{job.repo}#readme",
            kind=DocumentKind.README,
            repo=job.repo,
            title=f"{job.repo} README",
            body=content,
            url=payload.get("html_url", ""),
            path=payload.get("path"),
            fetched_at=datetime.now(UTC),
            metadata=metadata,
        )
        return [doc], None, None  # READMEs are single objects; no pagination.

    # --- issues & PRs ----------------------------------------------------

    def _list_params(self, page: int) -> dict:
        return {
            "state": "all",
            "per_page": self._settings.page_size,
            "page": page,
            "sort": "updated",
            "direction": "desc",
        }

    def _fetch_issues(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        page = cursor_to_page(job.cursor)
        params = self._list_params(page)
        if job.since is not None:
            params["since"] = job.since.isoformat()
        items = self._get(f"/repos/{job.repo}/issues", params=params)

        docs = self._normalize_all(
            job,
            # The issues endpoint also returns PRs; those are fetched by the PR job.
            [it for it in items if "pull_request" not in it],
            self._normalize_issue,
        )
        cursor = next_page_cursor(len(items), page, self._settings.page_size)
        return docs, self._successor(job, cursor)

    def _fetch_pulls(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        page = cursor_to_page(job.cursor)
        items = self._get(f"/repos/{job.repo}/pulls", params=self._list_params(page))

        fresh = []
        reached_watermark = False
        for it in items:
            updated = _safe_ts(it.get("updated_at"))
            # pulls has no server-side `since`; filter client-side on the watermark.
            if job.since is not None and updated is not None and updated < job.since:
                reached_watermark = True  # newest-first: everything after is older too
                continue
            fresh.append(it)
        docs = self._normalize_all(job, fresh, self._normalize_pr)
        cursor = None if reached_watermark else next_page_cursor(
            len(items), page, self._settings.page_size
        )
        return docs, self._successor(job, cursor)

    def _normalize_all(
        self, job: FetchJob, items: list, normalize: Callable[[str, dict], Document]
    ) -> list[Document]:
        docs = []
        for it in items:
            try:
                docs.append(normalize(job.repo, it))
            except (KeyError, TypeError, ValueError) as exc:
                # One malformed item must not fail the page (and with it, every later page).
                log_event(
                    logger,
                    "item_malformed",
                    level=logging.ERROR,
                    repo=job.repo,
                    kind=str(job.kind),
                    number=it.get("number") if isinstance(it, dict) else None,
                    error=type(exc).__name__,
                )
        return docs

    @staticmethod
    def _successor(job: FetchJob, cursor: str | None) -> FetchJob | None:
        if cursor is None:
            return None
        return job.model_copy(update={"cursor": cursor, "attempt": 0})

    # --- files -----------------------------------------------------------

    def _fetch_files(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        repo = job.repo
        if job.cursor:
            commit_sha, tree_sha, committed_raw, offset_raw = job.cursor.split("|")
            offset = int(offset_raw)
            committed_at = _safe_ts(committed_raw or None)
        else:
            meta = self._get(f"/repos/{repo}")
            full_name = meta.get("full_name")
            if full_name and full_name.lower() != repo.lower():
                log_event(
                    logger,
                    "repository_renamed",
                    level=logging.WARNING,
                    configured=repo,
                    actual=full_name,
                )
            branch = meta.get("default_branch") or "main"
            commit = self._get(f"/repos/{repo}/commits/{branch}")
            commit_sha = commit["sha"]
            tree_sha = commit["commit"]["tree"]["sha"]
            committed_raw = ((commit["commit"].get("committer") or {}).get("date")) or ""
            committed_at = _safe_ts(committed_raw or None)
            offset = 0

        tree = self._get(f"/repos/{repo}/git/trees/{tree_sha}", params={"recursive": "1"})
        entries = sorted(
            (
                e
                for e in tree.get("tree", [])
                if e.get("type") == "blob" and path_matches(e.get("path", ""),
                                                            tuple(self._settings.file_globs))
            ),
            key=lambda e: e["path"],
        )
        if len(entries) > self._settings.max_files_per_repo:
            if offset == 0:
                log_event(
                    logger,
                    "files_capped",
                    level=logging.WARNING,
                    repo=repo,
                    matched=len(entries),
                    cap=self._settings.max_files_per_repo,
                )
            entries = entries[: self._settings.max_files_per_repo]

        page = entries[offset : offset + self._settings.files_per_page]
        ids = {e["path"]: _file_doc_id(repo, e["path"]) for e in page}
        indexed = self._index_lookup(list(ids.values())) if self._index_lookup else {}

        docs: list[Document] = []
        for entry in page:
            path = entry["path"]
            doc_id = ids[path]
            state = indexed.get(doc_id)
            if (
                state is not None
                and state.content_hash == file_content_hash(entry["sha"], path)
                and state.extraction_status
                not in {ExtractionStatus.FAILED.value, ExtractionStatus.PARTIAL.value}
            ):
                continue  # unchanged blob: skip the download entirely
            size = entry.get("size") or 0
            if size > self._settings.max_file_bytes:
                log_event(logger, "file_too_large", level=logging.WARNING, repo=repo, path=path,
                          size=size, limit=self._settings.max_file_bytes)
                continue
            blob = self._get(f"/repos/{repo}/git/blobs/{entry['sha']}")
            content = _decode_content(blob)
            if content is None:
                log_event(logger, "file_undecodable", level=logging.WARNING, repo=repo,
                          path=path)
                continue
            docs.append(
                Document(
                    id=doc_id,
                    source_type=SourceType.GITHUB,
                    external_id=f"{repo}:{path}",
                    kind=DocumentKind.FILE,
                    repo=repo,
                    title=path,
                    body=content,
                    url=f"https://github.com/{repo}/blob/{commit_sha}/{path}",
                    path=path,
                    revision=commit_sha,
                    revision_committed_at=committed_at,
                    fetched_at=datetime.now(UTC),
                    metadata={"blob_sha": entry["sha"], "size": size},
                )
            )

        next_offset = offset + self._settings.files_per_page
        successor = None
        if next_offset < len(entries):
            successor = job.model_copy(
                update={
                    "cursor": f"{commit_sha}|{tree_sha}|{committed_raw}|{next_offset}",
                    "attempt": 0,
                }
            )

        reconciliation = None
        if offset == 0:
            if tree.get("truncated"):
                # An incomplete listing must never delete anything.
                log_event(logger, "tree_truncated", level=logging.WARNING, repo=repo)
            else:
                reconciliation = Reconciliation(
                    scope={"repo": repo, "kind": DocumentKind.FILE.value},
                    live_document_ids={_file_doc_id(repo, e["path"]) for e in entries},
                    reason="git_tree",
                )
        return docs, successor, reconciliation

    # --- normalization ---------------------------------------------------

    @staticmethod
    def _normalize_issue(repo: str, it: dict) -> Document:
        number = it["number"]
        return Document(
            id=Document.make_id(SourceType.GITHUB, repo, DocumentKind.ISSUE, str(number)),
            source_type=SourceType.GITHUB,
            external_id=f"{repo}#{number}",
            kind=DocumentKind.ISSUE,
            repo=repo,
            title=it.get("title") or "",
            body=it.get("body") or "",
            url=it.get("html_url", ""),
            author=(it.get("user") or {}).get("login"),
            created_at=parse_github_timestamp(it.get("created_at")),
            updated_at=parse_github_timestamp(it.get("updated_at")),
            labels=[lbl["name"] for lbl in it.get("labels", []) if isinstance(lbl, dict)],
            metadata={"state": it.get("state"), "comments": it.get("comments")},
            state=it.get("state"),
            fetched_at=datetime.now(UTC),
        )

    @staticmethod
    def _normalize_pr(repo: str, it: dict) -> Document:
        number = it["number"]
        state = "merged" if it.get("merged_at") else it.get("state")
        return Document(
            id=Document.make_id(SourceType.GITHUB, repo, DocumentKind.PR, str(number)),
            source_type=SourceType.GITHUB,
            external_id=f"{repo}#{number}",
            kind=DocumentKind.PR,
            repo=repo,
            title=it.get("title") or "",
            body=it.get("body") or "",
            url=it.get("html_url", ""),
            author=(it.get("user") or {}).get("login"),
            created_at=parse_github_timestamp(it.get("created_at")),
            updated_at=parse_github_timestamp(it.get("updated_at")),
            labels=[lbl["name"] for lbl in it.get("labels", []) if isinstance(lbl, dict)],
            metadata={"state": it.get("state"), "draft": it.get("draft"),
                      "merged_at": it.get("merged_at")},
            state=state,
            fetched_at=datetime.now(UTC),
        )


class SourceRequestError(Exception):
    """A non-retryable failure sending a request to GitHub (details withheld: may hold secrets)."""


def _clean_token(token: str | None) -> str | None:
    """Strip whitespace and reject tokens that cannot be sent in an HTTP header.

    The error message never includes the token value.
    """
    if token is None:
        return None
    token = token.strip()
    if not token:
        return None
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in token):
        raise SourceAuthError(
            "The GitHub token contains characters that are not allowed in an HTTP header "
            "(e.g. spaces or line breaks inside it). Store the bare token in the secret."
        )
    return token


def _file_doc_id(repo: str, path: str) -> str:
    return Document.make_id(SourceType.GITHUB, repo, DocumentKind.FILE, path)


def _safe_ts(value: str | None) -> datetime | None:
    try:
        return parse_github_timestamp(value)
    except ValueError:
        return None


def _decode_content(payload: dict) -> str | None:
    """Decode a GitHub contents/blob payload; ``None`` for binary or malformed content."""
    encoding = payload.get("encoding", "base64")
    raw = payload.get("content", "")
    if encoding != "base64":
        return raw if isinstance(raw, str) and "\x00" not in raw else None
    try:
        data = base64.b64decode(raw)
    except (binascii.Error, ValueError):
        return None
    if b"\x00" in data:
        return None  # binary
    return data.decode("utf-8", "replace")
