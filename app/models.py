"""Normalised shapes shared by every provider.

Both providers emit these exact dicts so the feed and the UI never branch on
where a commit came from.
"""

from __future__ import annotations

import fnmatch
import re
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
    # commit sha -> tag names pointing at it, for the whole repo.
    tags: dict[str, list[str]] = field(default_factory=dict)

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
            "tags_total": sum(len(v) for v in self.tags.values()),
        }


# `git cherry-pick -x` appends this line; it is the only trace a cherry-pick
# leaves in the commit itself.
_CHERRY_RECORDED = re.compile(
    r"\(cherry[ -]?picked from commit\s+([0-9a-fA-F]{7,40})\s*\)", re.IGNORECASE
)
# A hand-written mention. Weaker evidence, reported as such.
_CHERRY_MENTIONED = re.compile(r"\bcherry[- ]?pick(?:ed|ing)?\b", re.IGNORECASE)


def detect_cherry_pick(message: str) -> dict[str, Any]:
    """Whether a commit says it is a cherry-pick, and how strongly.

    Only `git cherry-pick -x` records the source commit; a plain `git cherry-pick`
    leaves *no* trace in the commit object at all, so an absence here is not
    evidence that a commit is original. The two confidence levels are kept apart
    so the UI never presents a passing mention as a recorded provenance.
    """
    text = message or ""

    recorded = _CHERRY_RECORDED.search(text)
    if recorded:
        return {
            "is_cherry_pick": True,
            "source_sha": recorded.group(1).lower(),
            "evidence": "recorded",
        }

    if _CHERRY_MENTIONED.search(text):
        return {"is_cherry_pick": True, "source_sha": None, "evidence": "mentioned"}

    return {"is_cherry_pick": False, "source_sha": None, "evidence": None}


def make_tag(
    *,
    name: str,
    commit_sha: str,
    annotated: bool,
    tagger_name: str | None = None,
    tagger_email: str | None = None,
    tagger_date: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """One tag, normalised across providers.

    Only *annotated* tags carry a tagger and a creation date — a lightweight tag
    is just a ref pointing at a commit, with no object of its own and therefore
    no author or timestamp anywhere in the repository. We report that honestly
    rather than substituting the commit's own author, which would look like data
    but answer a different question.
    """
    return {
        "name": name,
        "commit_sha": commit_sha,
        "annotated": annotated,
        "tagger_name": tagger_name or None,
        "tagger_email": tagger_email or None,
        "tagger_date": tagger_date or None,
        "message": (message or "").strip() or None,
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
        # Tag names pointing at this exact commit; filled in by the feed.
        "tags": [],
        "cherry_pick": detect_cherry_pick(message),
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
