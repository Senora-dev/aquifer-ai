"""A connector for repositories checked out on the local disk (development, demos, benchmark).

It produces the same :class:`Document` shapes as the GitHub connector — files matching
``file_globs`` (``DocumentKind.FILE``), the README, and optionally issues/PRs from fixture JSON —
so everything downstream (extraction, chunking, indexing, the MCP tools) behaves exactly as in a
deployment. It needs no network, credentials, or AWS.

Layout of a repository directory::

    <repo-dir>/                       any files; those matching file_globs are ingested
    <repo-dir>/README.md
    <repo-dir>/.aquifer/source.json   optional: {"repo": "owner/name", "revision": "...",
                                       "url_base": "https://github.com/owner/name"}
    <repo-dir>/.aquifer/issues.json   optional: [{"number", "title", "body", "state",
    <repo-dir>/.aquifer/pulls.json                 "created_at", "updated_at", "labels"}]

Provenance is reported honestly: ``revision`` is the value declared in ``source.json`` or, for a
git checkout, ``git rev-parse HEAD``; otherwise it is ``None``. Permalinks are only built when a
``url_base`` and a revision are known.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from aquifer.connectors.base import parse_github_timestamp, path_matches
from aquifer.core.interfaces import Connector
from aquifer.core.models import (
    Document,
    DocumentKind,
    FetchJob,
    Reconciliation,
    Source,
    SourceType,
)

DEFAULT_GLOBS = (
    "**/*.tf",
    "**/docker-compose*.yml",
    "**/docker-compose*.yaml",
    "**/compose.yml",
    "**/compose.yaml",
    "**/catalog-info.yaml",
    "**/catalog-info.yml",
    "**/adr/**/*.md",
    "**/adrs/**/*.md",
    "**/decisions/**/*.md",
)
_SKIP_DIRS = {".git", ".aquifer", "node_modules", ".venv", "__pycache__", ".terraform"}


class LocalRepoConnector(Connector):
    source_type = SourceType.GITHUB.value

    def __init__(self, repo_dirs: dict[str, Path], file_globs: Iterable[str] = DEFAULT_GLOBS,
                 max_file_bytes: int = 262_144) -> None:
        """``repo_dirs`` maps ``owner/name`` → directory."""
        self.repo_dirs = {name: Path(d) for name, d in repo_dirs.items()}
        self.file_globs = tuple(file_globs)
        self.max_file_bytes = max_file_bytes

    @classmethod
    def from_root(cls, root: Path, **kw) -> LocalRepoConnector:
        """Every immediate subdirectory of ``root`` is a repository."""
        dirs: dict[str, Path] = {}
        for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
            meta = _source_meta(d)
            dirs[meta.get("repo") or f"local/{d.name}"] = d
        return cls(dirs, **kw)

    @property
    def repos(self) -> list[str]:
        return sorted(self.repo_dirs)

    def discover(self, source: Source) -> Iterable[FetchJob]:
        for repo in source.config.get("repos") or self.repos:
            for kind in (DocumentKind.README, DocumentKind.FILE, DocumentKind.ISSUE,
                         DocumentKind.PR):
                yield FetchJob(source_id=source.id, source_type=SourceType.GITHUB, repo=repo,
                               kind=kind)

    def fetch(self, job: FetchJob) -> tuple[list[Document], FetchJob | None]:
        docs, successor, _ = self.fetch_with_reconciliation(job)
        return docs, successor

    def fetch_with_reconciliation(
        self, job: FetchJob
    ) -> tuple[list[Document], FetchJob | None, Reconciliation | None]:
        root = self.repo_dirs[job.repo]
        meta = _source_meta(root)
        revision = meta.get("revision") or _git_head(root)
        url_base = meta.get("url_base")
        now = datetime.now(UTC)

        if job.kind == DocumentKind.README:
            readme = next((p for p in sorted(root.iterdir())
                           if p.is_file() and p.name.lower().startswith("readme")), None)
            if readme is None:
                return [], None, Reconciliation(scope={"repo": job.repo, "kind": "readme"},
                                                reason="readme_not_found")
            return [self._file_doc(job.repo, root, readme, DocumentKind.README, revision,
                                   url_base, now)], None, None

        if job.kind == DocumentKind.FILE:
            docs = []
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root).as_posix()
                if (not path.is_file() or set(path.relative_to(root).parts) & _SKIP_DIRS
                        or not path_matches(rel, self.file_globs)
                        or path.stat().st_size > self.max_file_bytes):
                    continue
                docs.append(self._file_doc(job.repo, root, path, DocumentKind.FILE, revision,
                                           url_base, now))
            recon = Reconciliation(scope={"repo": job.repo, "kind": "file"},
                                   live_document_ids={d.id for d in docs}, reason="local_tree")
            return docs, None, recon

        name = "issues.json" if job.kind == DocumentKind.ISSUE else "pulls.json"
        fixture = root / ".aquifer" / name
        items = json.loads(fixture.read_text()) if fixture.exists() else []
        docs = [self._item_doc(job.repo, job.kind, it, url_base, now) for it in items]
        return docs, None, None

    def _file_doc(self, repo, root, path, kind, revision, url_base, now) -> Document:  # noqa: ANN001
        rel = path.relative_to(root).as_posix()
        url = f"{url_base}/blob/{revision}/{rel}" if url_base and revision else ""
        return Document(
            id=(Document.make_id(SourceType.GITHUB, repo, kind, rel) if kind == DocumentKind.FILE
                else Document.make_id(SourceType.GITHUB, repo, kind, "readme")),
            source_type=SourceType.GITHUB,
            external_id=f"{repo}:{rel}",
            kind=kind,
            repo=repo,
            title=rel if kind == DocumentKind.FILE else f"{repo} README",
            body=path.read_text(encoding="utf-8", errors="replace"),
            url=url,
            path=rel,
            revision=revision if kind == DocumentKind.FILE else None,
            fetched_at=now,
        )

    @staticmethod
    def _item_doc(repo, kind, it, url_base, now) -> Document:  # noqa: ANN001
        number = it["number"]
        segment = "issues" if kind == DocumentKind.ISSUE else "pull"
        return Document(
            id=Document.make_id(SourceType.GITHUB, repo, kind, str(number)),
            source_type=SourceType.GITHUB,
            external_id=f"{repo}#{number}",
            kind=kind,
            repo=repo,
            title=it.get("title") or "",
            body=it.get("body") or "",
            url=f"{url_base}/{segment}/{number}" if url_base else "",
            author=it.get("author"),
            created_at=parse_github_timestamp(it.get("created_at")),
            updated_at=parse_github_timestamp(it.get("updated_at")),
            labels=list(it.get("labels") or []),
            state=it.get("state"),
            fetched_at=now,
        )


def _source_meta(root: Path) -> dict:
    meta = Path(root) / ".aquifer" / "source.json"
    return json.loads(meta.read_text()) if meta.exists() else {}


def _git_head(root: Path) -> str | None:
    if not (Path(root) / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None
