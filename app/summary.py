"""Repository + branch + folder → a flat table of commits and their tags.

This is the tabular counterpart to the activity feed: one row per commit, with
the tag metadata (tagger, tag date) that the feed's chips deliberately omit.

Only the chosen branch is queried, rather than fanning out over every branch the
way the feed does, so picking one branch costs one page of commits instead of one
per branch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings
from .feed import enrich_commit_folders
from .models import make_commit
from .paths import derive_folders
from .store import FileStore


class SummaryError(Exception):
    pass


def _matches_folder(commit: dict[str, Any], folder: str | None) -> bool:
    if not folder:
        return True
    return folder in (commit.get("folders") or [])


async def build_summary(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: FileStore,
    *,
    repo_key: str,
    branch: str,
    folder: str | None,
    since_dt: datetime,
    until_dt: datetime | None,
    limit: int,
) -> dict[str, Any]:
    since = since_dt.replace(microsecond=0).isoformat()
    until = until_dt.replace(microsecond=0).isoformat() if until_dt else None

    if repo_key.startswith("github:"):
        full_name = repo_key.split(":", 1)[1]
        try:
            commits = await github_provider.collect_branch(
                gh_client, full_name, branch, since, until, limit
            )
            tags_by_sha = await gh_client.list_tag_details(full_name)
        except github_provider.GitHubError as exc:
            raise SummaryError(exc.message) from exc
        if settings.folders_enabled:
            await enrich_commit_folders(settings, gh_client, store, commits)
        repo_name = full_name
    else:
        repo = next((r for r in settings.csr_repos if r.key == repo_key), None)
        if repo is None:
            raise SummaryError(f"Unknown repository: {repo_key}")
        try:
            path = await mirror.sync(repo.key, repo.clone_url)
            entries = await mirror.log(
                path, branch, since, until, limit, with_files=settings.folders_enabled
            )
            tags_by_sha = await mirror.tag_details(path)
        except csr_provider.CsrError as exc:
            raise SummaryError(str(exc)) from exc

        commits = []
        for entry in entries:
            commit = make_commit(
                provider="csr",
                repo_key=repo.key,
                repo_name=repo.name,
                sha=entry["sha"],
                message=entry["message"],
                author_name=entry["author"],
                date=entry["date"],
                url=repo.commit_url(entry["sha"]),
                branch=branch,
                paths=entry["paths"],
            )
            commit["folders"] = derive_folders(
                entry["paths"], settings.folder_depth, settings.folder_paths, settings.folder_exclude
            )
            commit.pop("paths", None)
            commits.append(commit)
        repo_name = repo.name

    rows = []
    for commit in commits:
        commit.pop("paths", None)
        if not _matches_folder(commit, folder):
            continue
        tags = tags_by_sha.get(commit["sha"], [])
        rows.append(
            {
                "sha": commit["sha"],
                "url": commit["url"],
                "author_name": commit["author_name"],
                "author_login": commit.get("author_login"),
                "date": commit["date"],
                "title": commit["title"],
                "folders": commit.get("folders") or [],
                "cherry_pick": commit.get("cherry_pick"),
                "files_changed": commit.get("files_changed") or 0,
                "tags": tags,
            }
        )

    folders_seen = sorted({f for c in commits for f in (c.get("folders") or [])})

    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "branch": branch,
        "folder": folder,
        "since": since,
        "until": until,
        "rows": rows,
        "row_count": len(rows),
        "commits_scanned": len(commits),
        "folders_available": folders_seen,
        "tagged_rows": sum(1 for r in rows if r["tags"]),
        # True when the branch page filled up: older commits may exist.
        "capped": len(commits) >= limit,
        "limit": limit,
    }
