"""GitHub connector.

Implements :class:`aquifer.core.interfaces.Connector` for GitHub, normalizing issues, pull
requests, and repository READMEs into :class:`Document` objects. Discussions require the
GraphQL API and are deferred (the ``Connector`` interface makes adding them additive).

Pagination is page-based: each :meth:`fetch` call returns one page plus a successor
``FetchJob`` (or ``None`` when exhausted), so the ingestion worker can process one bounded
page per Lambda invocation. Incremental runs pass a ``since`` watermark; the issues endpoint
filters server-side, while pulls are filtered client-side.

The httpx client is created lazily and can be injected for testing.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from typing import Any

from aquifer.connectors.base import (
    cursor_to_page,
    next_page_cursor,
    parse_github_timestamp,
)
from aquifer.core.config import GitHubSettings, get_settings
from aquifer.core.interfaces import Connector
from aquifer.core.models import Document, DocumentKind, FetchJob, Source, SourceType

# Kinds this connector knows how to fetch, in discovery order.
_DISCOVERED_KINDS = (DocumentKind.README, DocumentKind.ISSUE, DocumentKind.PR)


class GitHubConnector(Connector):
    source_type = SourceType.GITHUB.value

    def __init__(
        self,
        settings: GitHubSettings | None = None,
        token: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings().github
        self._token = token
        self._http = http_client

    @property
    def http(self) -> Any:
        if self._http is None:
            import httpx

            headers = {"Accept": "application/vnd.github+json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._http = httpx.Client(
                base_url=self._settings.api_url,
                headers=headers,
                timeout=30.0,
            )
        return self._http

    # --- discovery -------------------------------------------------------

    def discover(self, source: Source) -> Iterable[FetchJob]:
        """Emit the initial fetch jobs: one per (repo, kind)."""
        repos = source.config.get("repos") or self._settings.repo_allowlist
        since = source.config.get("since")
        for repo in repos:
            for kind in _DISCOVERED_KINDS:
                yield FetchJob(
                    source_id=source.id,
                    source_type=SourceType.GITHUB,
                    repo=repo,
                    kind=kind,
                    cursor=None,
                    since=since,
                )

    # --- fetch -----------------------------------------------------------

    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        if job.kind == DocumentKind.README:
            return self._fetch_readme(job)
        if job.kind == DocumentKind.ISSUE:
            return self._fetch_issues(job)
        if job.kind == DocumentKind.PR:
            return self._fetch_pulls(job)
        raise ValueError(f"GitHubConnector cannot fetch kind {job.kind!r}")

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = self.http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def _fetch_readme(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        try:
            payload = self._get(f"/repos/{job.repo}/readme")
        except Exception:
            # No README is a normal, non-fatal condition; skip it.
            return [], None
        content = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
        doc = Document(
            id=Document.make_id(SourceType.GITHUB, job.repo, DocumentKind.README, "readme"),
            source_type=SourceType.GITHUB,
            external_id=f"{job.repo}#readme",
            kind=DocumentKind.README,
            repo=job.repo,
            title=f"{job.repo} README",
            body=content,
            url=payload.get("html_url", ""),
        )
        return [doc], None  # READMEs are single objects; no pagination.

    def _fetch_issues(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        page = cursor_to_page(job.cursor)
        params = {
            "state": "all",
            "per_page": self._settings.page_size,
            "page": page,
            "sort": "updated",
            "direction": "asc",
        }
        if job.since is not None:
            params["since"] = job.since.isoformat()
        items = self._get(f"/repos/{job.repo}/issues", params=params)

        docs = [
            self._normalize_issue(job.repo, it)
            for it in items
            if "pull_request" not in it  # the issues endpoint also returns PRs; skip them here
        ]
        cursor = next_page_cursor(len(items), page, self._settings.page_size)
        return docs, self._successor(job, cursor)

    def _fetch_pulls(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        page = cursor_to_page(job.cursor)
        params = {
            "state": "all",
            "per_page": self._settings.page_size,
            "page": page,
            "sort": "updated",
            "direction": "asc",
        }
        items = self._get(f"/repos/{job.repo}/pulls", params=params)

        docs = []
        for it in items:
            updated = parse_github_timestamp(it.get("updated_at"))
            # pulls has no server-side `since`; filter client-side on the watermark.
            if job.since is not None and updated is not None and updated < job.since:
                continue
            docs.append(self._normalize_pr(job.repo, it))
        cursor = next_page_cursor(len(items), page, self._settings.page_size)
        return docs, self._successor(job, cursor)

    @staticmethod
    def _successor(job: FetchJob, cursor: str | None) -> FetchJob | None:
        if cursor is None:
            return None
        return job.model_copy(update={"cursor": cursor})

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
        )

    @staticmethod
    def _normalize_pr(repo: str, it: dict) -> Document:
        number = it["number"]
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
            metadata={"state": it.get("state"), "draft": it.get("draft")},
        )
