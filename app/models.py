"""Normalised shapes shared by every provider.

Both providers emit these exact dicts so the feed and the UI never branch on
where a commit came from.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RepoResult:
    """One repository's contribution to the feed."""

    provider: str          # "github" | "csr"
    key: str               # unique across providers, e.g. "github:owner/repo"
    name: str              # display name
    url: str | None        # link to the repo in its web UI
    default_branch: str | None
    branches_total: int
    branches_shown: int
    private: bool = False
    commits: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def meta(self, branches_active: int) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "key": self.key,
            "full_name": self.name,
            "html_url": self.url,
            "default_branch": self.default_branch,
            "branches_total": self.branches_total,
            "branches_shown": self.branches_shown,
            "branches_active": branches_active,
            "private": self.private,
        }


def make_commit(
    *,
    provider: str,
    repo_key: str,
    repo_name: str,
    sha: str,
    message: str,
    author_name: str,
    date: str | None,
    url: str | None,
    author_login: str | None = None,
    avatar_url: str | None = None,
    branch: str,
    paths: list[str] | None = None,
    files_changed: int | None = None,
    files_truncated: bool = False,
) -> dict[str, Any]:
    message = (message or "").strip()
    head, _, tail = message.partition("\n")
    return {
        "provider": provider,
        "repo_key": repo_key,
        "repo": repo_name,
        "sha": sha,
        "title": head,
        "body": tail.strip(),
        "author_name": author_name or "unknown",
        "author_login": author_login,
        "avatar_url": avatar_url,
        "date": date,
        "url": url,
        "branches": [branch],
        # Populated when folder tracking is on; `folders` is derived from `paths`
        # later so a config change doesn't require re-fetching.
        "paths": paths or [],
        "folders": [],
        "files_changed": files_changed if files_changed is not None else (len(paths) if paths else 0),
        "files_truncated": files_truncated,
    }


def branch_allowed(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    return not any(fnmatch.fnmatch(name, pat) for pat in exclude)
