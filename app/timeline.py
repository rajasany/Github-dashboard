"""Repository + folder + a time window → commits on a timeline.

The Summary table answers "what are the commits, with their tags". This answers
a different question: *when did work happen in this folder*. Same underlying
commits, ordered and counted for reading down a time axis rather than across a
row.

Two paths, because the cost differs sharply:

  * a **named branch** asks the provider for that branch alone — one page of
    commits;
  * **every branch** fans out the way the activity feed does and folds the
    result, so a commit reachable from several branches appears once carrying
    all of them. That costs a request per branch, which is why it is not the
    only mode.

Bucketing into days is deliberately left to the caller. A commit at 23:40 UTC
belongs to a different day in Auckland than in Los Angeles, and only the browser
knows which one the reader means.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings
from .feed import build_feed
from .store import FileStore
from .summary import SummaryError, build_summary


class TimelineError(Exception):
    pass


def _entry(
    *,
    sha: str,
    url: str | None,
    title: str,
    author: str,
    author_login: str | None,
    date: str | None,
    folders: list[str],
    branches: list[str],
    tags: list[Any],
    cherry_pick: Any,
    files_changed: int,
) -> dict[str, Any]:
    """One point on the timeline, in the one shape the UI reads."""
    return {
        "sha": sha,
        "short": (sha or "")[:10],
        "url": url,
        "title": title,
        "author": author,
        "author_login": author_login,
        "date": date,
        "folders": folders,
        "branches": branches,
        "tags": tags,
        "cherry_pick": cherry_pick,
        "files_changed": files_changed,
    }


async def build_timeline(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: FileStore,
    *,
    repo_key: str,
    branch: str = "",
    folder: str = "",
    since_dt: datetime,
    until_dt: datetime | None,
    limit: int,
) -> dict[str, Any]:
    repo_name = repo_key.split(":", 1)[-1]
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    capped = False

    if branch:
        try:
            # Folder filtering happens here, not there, so the folder picker can
            # still offer every folder the window contains.
            summary = await build_summary(
                settings, gh_client, mirror, store,
                repo_key=repo_key, branch=branch, folder=None,
                since_dt=since_dt, until_dt=until_dt, limit=limit,
            )
        except SummaryError as exc:
            raise TimelineError(str(exc)) from exc

        repo_name = summary["repo"]
        capped = summary["capped"]
        for row in summary["rows"]:
            entries.append(_entry(
                sha=row["sha"], url=row["url"], title=row["title"],
                author=row["author_name"], author_login=row.get("author_login"),
                date=row["date"], folders=row.get("folders") or [], branches=[branch],
                tags=row.get("tags") or [], cherry_pick=row.get("cherry_pick"),
                files_changed=row.get("files_changed") or 0,
            ))
    else:
        github_repos = [repo_key.split(":", 1)[1]] if repo_key.startswith("github:") else []
        csr_repos = [r for r in settings.csr_repos if r.key == repo_key]
        if not github_repos and not csr_repos:
            raise TimelineError(f"Unknown repository: {repo_key}")

        feed = await build_feed(
            settings, gh_client, mirror, store,
            github_repos=github_repos, csr_repos=csr_repos,
            since_dt=since_dt, until_dt=until_dt, commits_per_branch=limit,
        )
        errors = [e.get("message", str(e)) if isinstance(e, dict) else str(e)
                  for e in (feed.get("errors") or [])]
        tags_for_repo = (feed.get("tags") or {}).get(repo_key, {})
        for commit in feed["commits"]:
            if commit.get("repo_key") != repo_key:
                continue
            repo_name = commit.get("repo") or repo_name
            entries.append(_entry(
                sha=commit["sha"], url=commit.get("url"), title=commit.get("title", ""),
                author=commit.get("author_name", ""), author_login=commit.get("author_login"),
                date=commit.get("date"), folders=commit.get("folders") or [],
                branches=sorted(commit.get("branches") or []),
                tags=tags_for_repo.get(commit["sha"], []),
                cherry_pick=commit.get("cherry_pick"),
                files_changed=commit.get("files_changed") or 0,
            ))

    # Every folder the window touched, for the picker — computed before the
    # filter so narrowing to one folder does not empty the list you narrowed with.
    folders_available = sorted({f for e in entries for f in e["folders"]})

    if folder:
        entries = [e for e in entries if folder in e["folders"]]

    entries.sort(key=lambda e: (e["date"] or "", e["sha"]), reverse=True)

    authors: dict[str, int] = {}
    for entry in entries:
        authors[entry["author"]] = authors.get(entry["author"], 0) + 1

    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "branch": branch,
        "folder": folder,
        "since": since_dt.replace(microsecond=0).isoformat(),
        "until": until_dt.replace(microsecond=0).isoformat() if until_dt else None,
        "commits": entries,
        "count": len(entries),
        "folders_available": folders_available,
        "authors": [
            {"name": name, "commits": n}
            for name, n in sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "files_changed": sum(e["files_changed"] for e in entries),
        "tagged": sum(1 for e in entries if e["tags"]),
        # detect_cherry_pick always returns a dict, so truthiness would count
        # every commit. Only the flag inside it means anything.
        "cherry_picks": sum(
            1 for e in entries
            if isinstance(e["cherry_pick"], dict) and e["cherry_pick"].get("is_cherry_pick")
        ),
        "all_branches": not branch,
        # "All branches" means every branch config permits. Saying so matters:
        # a commit on an excluded branch is absent, and silence about that reads
        # as the commit not existing.
        "branch_filter": {
            "include": list(settings.branch_include),
            "exclude": list(settings.branch_exclude),
            "filtered": bool(settings.branch_include or settings.branch_exclude),
        },
        # True when a branch page filled up: older commits in the window may exist.
        "capped": capped,
        "limit": limit,
        "errors": errors,
    }
