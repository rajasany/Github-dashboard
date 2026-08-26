"""Branch-to-branch comparison, normalised across both providers.

Both sides answer the same question: *what is on `head` that is not on `base`?*

That is a three-dot comparison — the diff is taken from the **merge base**, not
from the tip of `base`. Otherwise a `base` that has moved on independently would
show its own commits as if they were reversed changes on `head`, which is the
classic way branch comparisons mislead.

  GitHub  →  GET /repos/{o}/{r}/compare/{base}...{head}
  CSR/git →  git rev-list --left-right --count base...head
             git log base..head           (commits on head only)
             git diff base...head         (diff from the merge base)
"""

from __future__ import annotations

import asyncio
from typing import Any

from .config import CsrRepo, Settings
from .csr import CsrError, GitMirror, _FLD, _LOG_FORMAT, _REC
from .github import GitHubClient, GitHubError
from .models import branch_allowed, make_commit
from .paths import derive_folders

# GitHub returns at most 250 commits and 300 files per compare; git is unbounded
# but a review UI stops being useful long before that.
MAX_FILES = 500


class CompareError(Exception):
    pass


def _status_word(ahead: int, behind: int) -> str:
    if ahead and behind:
        return "diverged"
    if ahead:
        return "ahead"
    if behind:
        return "behind"
    return "identical"


def _summarise(files: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    additions = sum(int(f.get("additions") or 0) for f in files)
    deletions = sum(int(f.get("deletions") or 0) for f in files)
    folders = derive_folders(
        [f["path"] for f in files],
        settings.folder_depth,
        settings.folder_paths,
        settings.folder_exclude,
    )
    return {
        "files_changed": len(files),
        "additions": additions,
        "deletions": deletions,
        "folders": folders,
    }


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


async def compare_github(
    client: GitHubClient, full_name: str, base: str, head: str, settings: Settings
) -> dict[str, Any]:
    key = f"github:{full_name}"
    try:
        # `basehead` is literally "base...head"; branch names may contain slashes
        # and GitHub still splits on the triple dot.
        data = await client.get_compare(full_name, base, head)
    except GitHubError as exc:
        raise CompareError(exc.message) from exc

    if not isinstance(data, dict):
        raise CompareError("Unexpected response shape from GitHub's compare API.")

    raw_files = data.get("files") or []
    files = [
        {
            "path": f.get("filename", ""),
            "status": f.get("status", "modified"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "previous_path": f.get("previous_filename"),
        }
        for f in raw_files
        if isinstance(f, dict) and f.get("filename")
    ]

    commits = [
        make_commit(
            provider="github",
            repo_key=key,
            repo_name=full_name,
            sha=c.get("sha", ""),
            message=(c.get("commit") or {}).get("message") or "",
            author_name=((c.get("commit") or {}).get("author") or {}).get("name")
            or (c.get("author") or {}).get("login")
            or "unknown",
            author_login=(c.get("author") or {}).get("login"),
            avatar_url=(c.get("author") or {}).get("avatar_url"),
            date=((c.get("commit") or {}).get("author") or {}).get("date"),
            url=c.get("html_url"),
            branch=head,
        )
        for c in (data.get("commits") or [])
        if isinstance(c, dict)
    ]
    commits.reverse()  # GitHub returns oldest-first; the feed is newest-first.

    ahead = int(data.get("ahead_by") or 0)
    behind = int(data.get("behind_by") or 0)
    total = int(data.get("total_commits") or len(commits))

    return {
        "provider": "github",
        "repo_key": key,
        "repo": full_name,
        "base": base,
        "head": head,
        "status": data.get("status") or _status_word(ahead, behind),
        "ahead_by": ahead,
        "behind_by": behind,
        "total_commits": total,
        "commits": commits,
        "commits_truncated": max(0, total - len(commits)),
        "files": files[:MAX_FILES],
        "files_truncated": max(0, len(files) - MAX_FILES),
        "merge_base": (data.get("merge_base_commit") or {}).get("sha"),
        "html_url": data.get("html_url"),
        **_summarise(files, settings),
    }


# --------------------------------------------------------------------------
# CSR / local git mirror
# --------------------------------------------------------------------------


async def compare_csr(
    mirror: GitMirror, repo: CsrRepo, base: str, head: str, settings: Settings
) -> dict[str, Any]:
    try:
        path = await mirror.sync(repo.key, repo.clone_url)
    except CsrError as exc:
        raise CompareError(str(exc)) from exc

    async def git(args: list[str]) -> str:
        return await mirror._git(["-C", str(path), *args])

    for ref in (base, head):
        try:
            await git(["rev-parse", "--verify", f"refs/heads/{ref}"])
        except CsrError:
            raise CompareError(f"Branch not found in {repo.name}: {ref}")

    try:
        counts, merge_base = await asyncio.gather(
            git(["rev-list", "--left-right", "--count", f"{base}...{head}"]),
            git(["merge-base", base, head]),
        )
    except CsrError as exc:
        raise CompareError(str(exc)) from exc

    parts = counts.split()
    behind = int(parts[0]) if parts else 0   # on base only
    ahead = int(parts[1]) if len(parts) > 1 else 0  # on head only

    # Two-dot for the commit list (what head added), three-dot for the diff
    # (measured from the merge base) — the same pair GitHub reports.
    raw_log = await git(
        ["log", f"--format={_LOG_FORMAT}", "--name-only", "--diff-merges=first-parent",
         f"{base}..{head}"]
    )
    numstat = await git(["diff", "--numstat", f"{base}...{head}"])
    namestatus = await git(["diff", "--name-status", f"{base}...{head}"])

    status_by_path: dict[str, str] = {}
    for line in namestatus.splitlines():
        bits = line.split("\t")
        if len(bits) >= 2:
            code = bits[0][:1].lower()
            status_by_path[bits[-1]] = {
                "a": "added", "m": "modified", "d": "removed",
                "r": "renamed", "c": "copied", "t": "changed",
            }.get(code, "modified")

    files: list[dict[str, Any]] = []
    for line in numstat.splitlines():
        bits = line.split("\t")
        if len(bits) < 3:
            continue
        added, deleted, path_name = bits[0], bits[1], bits[-1]
        files.append(
            {
                "path": path_name,
                "status": status_by_path.get(path_name, "modified"),
                # Binary files report "-" rather than a count.
                "additions": int(added) if added.isdigit() else 0,
                "deletions": int(deleted) if deleted.isdigit() else 0,
                "binary": not added.isdigit(),
                "previous_path": None,
            }
        )

    commits: list[dict[str, Any]] = []
    for record in raw_log.split(_REC):
        if not record.strip():
            continue
        bits = record.split(_FLD)
        if len(bits) < 4:
            continue
        sha, author, date = bits[0].strip(), bits[1].strip(), bits[2].strip()
        message = _FLD.join(bits[3:-1])
        paths = [ln.strip() for ln in bits[-1].splitlines() if ln.strip()]
        entry = make_commit(
            provider="csr",
            repo_key=repo.key,
            repo_name=repo.name,
            sha=sha,
            message=message,
            author_name=author,
            date=date,
            url=repo.commit_url(sha),
            branch=head,
            paths=paths,
        )
        entry["folders"] = derive_folders(
            paths, settings.folder_depth, settings.folder_paths, settings.folder_exclude
        )
        entry.pop("paths", None)
        commits.append(entry)

    return {
        "provider": "csr",
        "repo_key": repo.key,
        "repo": repo.name,
        "base": base,
        "head": head,
        "status": _status_word(ahead, behind),
        "ahead_by": ahead,
        "behind_by": behind,
        "total_commits": ahead,
        "commits": commits,
        "commits_truncated": 0,
        "files": files[:MAX_FILES],
        "files_truncated": max(0, len(files) - MAX_FILES),
        "merge_base": merge_base.strip() or None,
        "html_url": repo.web_url,
        **_summarise(files, settings),
    }


async def list_branches(
    settings: Settings,
    gh_client: GitHubClient,
    mirror: GitMirror,
    repo_key: str,
) -> dict[str, Any]:
    """Every branch of one repo, minus the ones config filters out.

    `branch_include` / `branch_exclude` are honoured here exactly as the activity
    feed honours them, so a branch hidden from the feed cannot reappear in the
    Summary or Compare pickers. The count of what was filtered is returned too —
    a shorter list should never be silently shorter.
    """
    if repo_key.startswith("github:"):
        full_name = repo_key.split(":", 1)[1]
        try:
            meta, branches = await asyncio.gather(
                gh_client.get_repo(full_name), gh_client.list_branches(full_name)
            )
        except GitHubError as exc:
            raise CompareError(exc.message) from exc
        all_names = sorted(b.get("name", "") for b in branches if b.get("name"))
        default = (meta or {}).get("default_branch")
    else:
        repo = next((r for r in settings.csr_repos if r.key == repo_key), None)
        if repo is None:
            raise CompareError(f"Unknown repository: {repo_key}")
        try:
            path = await mirror.sync(repo.key, repo.clone_url)
            pairs = await mirror.branches(path)
            default = await mirror.default_branch(path)
        except CsrError as exc:
            raise CompareError(str(exc)) from exc
        all_names = sorted(name for name, _ in pairs)

    allowed = [
        n for n in all_names if branch_allowed(n, settings.branch_include, settings.branch_exclude)
    ]
    # The default branch is the one people compare and tabulate against; if a
    # pattern would hide it, that is far more likely a mistake in the globs than
    # an intention, so keep it and say nothing is missing.
    if default and default in all_names and default not in allowed:
        allowed.insert(0, default)
        allowed.sort()

    return {
        "repo_key": repo_key,
        "branches": allowed,
        "branches_total": len(all_names),
        "hidden": len(all_names) - len(allowed),
        "default_branch": default,
    }
