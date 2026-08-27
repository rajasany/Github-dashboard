"""Take a pasted list of commits, check they belong together, and order them.

The ordering is by **position in the branch's history**, not by timestamp.
Commit dates are author dates: a rebase, a cherry-pick or a fabricated date can
put them out of step with the real ancestry. Where the two disagree the answer
is still the ancestry order, and the disagreement is reported rather than hidden.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings
from .lookup import normalise_sha

# A paste of more than this is almost certainly a mistake, and on GitHub it
# would cost two API calls per commit.
MAX_COMMITS = 60

_SPLIT = re.compile(r"[\s,;]+")


class OrderError(Exception):
    pass


def parse_commit_list(raw: str) -> tuple[list[str], list[dict[str, str]]]:
    """Split a pasted blob into hashes, keeping whatever could not be read.

    Accepts newlines, commas, semicolons or spaces as separators, and tolerates
    full commit URLs and `-` / `*` bullet prefixes so a list copied out of a
    ticket or a changelog works without hand-editing.
    """
    entries = [e.strip().lstrip("-*•").strip() for e in _SPLIT.split(raw or "")]
    # A bullet on its own line collapses to an empty string once stripped; that
    # is punctuation, not a failed hash, so it must not surface as a rejection.
    entries = [e for e in entries if e]

    shas: list[str] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()

    for entry in entries:
        try:
            sha = normalise_sha(entry)
        except Exception:
            rejected.append({"input": entry, "reason": "not a commit hash"})
            continue
        if sha in seen:
            rejected.append({"input": entry, "reason": "duplicate"})
            continue
        seen.add(sha)
        shas.append(sha)

    return shas, rejected


# --------------------------------------------------------------------------
# per-provider resolution
# --------------------------------------------------------------------------


async def _github_commit(client: github_provider.GitHubClient, full_name: str, sha: str):
    try:
        data = await client._get(f"/repos/{full_name}/commits/{sha}")  # noqa: SLF001
    except github_provider.GitHubError as exc:
        if exc.status in (404, 422):
            return None
        raise
    return data if isinstance(data, dict) and data.get("sha") else None


async def _find_repo(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    sha: str,
) -> str | None:
    """Which configured repository holds this commit."""

    async def gh(name: str) -> str | None:
        return f"github:{name}" if await _github_commit(gh_client, name, sha) else None

    async def csr(repo) -> str | None:
        try:
            path = await mirror.sync(repo.key, repo.clone_url)
            out = await mirror._git(  # noqa: SLF001
                ["-C", str(path), "rev-parse", "--verify", f"{sha}^{{commit}}"]
            )
            return repo.key if out.strip() else None
        except csr_provider.CsrError:
            return None

    results = await asyncio.gather(
        *[gh(n) for n in settings.repos], *[csr(r) for r in settings.csr_repos],
        return_exceptions=True,
    )
    return next((r for r in results if isinstance(r, str)), None)


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------


async def order_commits(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    *,
    raw: str,
    repo_key: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    shas, rejected = parse_commit_list(raw)

    if not shas:
        raise OrderError("No commit hashes found in that list.")
    if len(shas) > MAX_COMMITS:
        raise OrderError(f"That is {len(shas)} commits; {MAX_COMMITS} is the most this will order at once.")

    # The repository is settled by the first resolvable commit, then every other
    # commit is checked against it — that is the "same repo" rule.
    resolved_key = repo_key
    if not resolved_key:
        for sha in shas:
            resolved_key = await _find_repo(settings, gh_client, mirror, sha)
            if resolved_key:
                break
    if not resolved_key:
        raise OrderError(
            "None of those commits could be found in any configured repository."
        )

    if resolved_key.startswith("github:"):
        return await _order_github(
            settings, gh_client, mirror, resolved_key, shas, rejected, branch
        )
    return await _order_csr(settings, gh_client, mirror, resolved_key, shas, rejected, branch)


def _finish(
    *,
    repo_key: str,
    repo: str,
    provider: str,
    branch: str | None,
    branches_available: list[str],
    default_branch: str | None,
    found: list[dict[str, Any]],
    problems: list[dict[str, str]],
    rejected: list[dict[str, str]],
    total_commits: int | None,
) -> dict[str, Any]:
    """Sort by ancestry, mark the newest, and note any date/ancestry disagreement."""
    # position is the commit's ordinal from the branch root, so descending
    # position is newest-first along the branch.
    on_branch = [c for c in found if c.get("position") is not None]
    undated = [c for c in found if c.get("position") is None]

    on_branch.sort(key=lambda c: c["position"], reverse=True)
    undated.sort(key=lambda c: c.get("date") or "", reverse=True)
    ordered = on_branch + undated

    by_date = sorted(found, key=lambda c: c.get("date") or "", reverse=True)
    date_disagrees = [c["sha"] for c in ordered] != [c["sha"] for c in by_date]

    for index, commit in enumerate(ordered):
        commit["rank"] = index + 1
        commit["is_latest"] = index == 0

    return {
        "repo_key": repo_key,
        "repo": repo,
        "provider": provider,
        "branch": branch,
        "branches_available": branches_available,
        "default_branch": default_branch,
        "total_commits": total_commits,
        "commits": ordered,
        "count": len(ordered),
        "problems": problems,
        "rejected": rejected,
        "date_order_differs": date_disagrees,
        "ordered_by": "ancestry" if on_branch else "date",
    }


async def _order_github(
    settings: Settings,
    client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    repo_key: str,
    shas: list[str],
    rejected: list[dict[str, str]],
    branch: str | None,
) -> dict[str, Any]:
    full_name = repo_key.split(":", 1)[1]
    meta, branch_list, tags_by_sha = await asyncio.gather(
        client.get_repo(full_name),
        client.list_branches(full_name),
        client.list_tag_details(full_name),
    )
    default_branch = (meta or {}).get("default_branch")
    names = [b.get("name") for b in branch_list if b.get("name")]
    target = branch or default_branch or (names[0] if names else None)
    if target and target not in names:
        raise OrderError(f"{full_name} has no branch called “{target}”.")

    total = await github_provider.count_branch_commits(client, full_name, target) if target else None

    problems: list[dict[str, str]] = []

    async def resolve(sha: str) -> dict[str, Any] | None:
        commit = await _github_commit(client, full_name, sha)
        if not commit:
            # Say where it *does* live, rather than only that it is not here.
            elsewhere = await _find_repo(settings, client, mirror, sha)
            problems.append(
                {
                    "sha": sha,
                    "reason": f"not in {full_name}"
                    + (f" — it is in {elsewhere.split(':', 1)[1]}" if elsewhere else ""),
                }
            )
            return None

        full_sha = commit["sha"]
        author = (commit.get("commit") or {}).get("author") or {}
        gh_author = commit.get("author") or {}

        position = distance = None
        on_branch = False
        if target:
            try:
                cmp_ = await client.get_compare(full_name, full_sha, target)
                if int(cmp_.get("behind_by") or 0) == 0:
                    on_branch = True
                    distance = int(cmp_.get("ahead_by") or 0)
                    position = (total - distance) if isinstance(total, int) else None
            except github_provider.GitHubError:
                problems.append({"sha": full_sha[:10], "reason": "could not check branch membership"})

        if target and not on_branch:
            problems.append({"sha": full_sha[:10], "reason": f"not on branch “{target}”"})
            return None

        return {
            "sha": full_sha,
            "url": commit.get("html_url"),
            "title": ((commit.get("commit") or {}).get("message") or "").split("\n", 1)[0],
            "author_name": author.get("name") or gh_author.get("login") or "unknown",
            "author_login": gh_author.get("login"),
            "date": author.get("date"),
            "position": position,
            "distance_to_head": distance,
            "tags": tags_by_sha.get(full_sha, []),
        }

    found = [c for c in await asyncio.gather(*(resolve(s) for s in shas)) if c]
    return _finish(
        repo_key=repo_key, repo=full_name, provider="github", branch=target,
        branches_available=names, default_branch=default_branch,
        found=found, problems=problems, rejected=rejected, total_commits=total,
    )


async def _order_csr(
    settings: Settings,
    client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    repo_key: str,
    shas: list[str],
    rejected: list[dict[str, str]],
    branch: str | None,
) -> dict[str, Any]:
    repo = next((r for r in settings.csr_repos if r.key == repo_key), None)
    if repo is None:
        raise OrderError(f"Unknown repository: {repo_key}")

    path = await mirror.sync(repo.key, repo.clone_url)

    async def git(args: list[str]) -> str:
        return await mirror._git(["-C", str(path), *args])  # noqa: SLF001

    default_branch = await mirror.default_branch(path)
    names = [n for n, _ in await mirror.branches(path)]
    target = branch or default_branch or (names[0] if names else None)
    if target and target not in names:
        raise OrderError(f"{repo.name} has no branch called “{target}”.")

    tags_by_sha = await mirror.tag_details(path)
    total = int((await git(["rev-list", "--count", target])).strip() or 0) if target else None

    problems: list[dict[str, str]] = []
    found: list[dict[str, Any]] = []

    for sha in shas:
        try:
            full_sha = (await git(["rev-parse", "--verify", f"{sha}^{{commit}}"])).strip()
        except csr_provider.CsrError:
            elsewhere = await _find_repo(settings, client, mirror, sha)
            problems.append({
                "sha": sha,
                "reason": f"not in {repo.name}"
                + (f" — it is in {elsewhere.split(':', 1)[1]}" if elsewhere else ""),
            })
            continue

        if target:
            contains = await git(
                ["for-each-ref", "--contains", full_sha, "--format=%(refname:short)", "refs/heads/"]
            )
            if target not in [b.strip() for b in contains.splitlines()]:
                problems.append({"sha": full_sha[:10], "reason": f"not on branch “{target}”"})
                continue

        distance = int((await git(["rev-list", "--count", f"{full_sha}..{target}"])).strip() or 0) if target else None
        raw = await git(["show", "-s", f"--format=%an{csr_provider._FLD}%aI{csr_provider._FLD}%s", full_sha])  # noqa: SLF001
        bits = (raw.split(csr_provider._FLD) + ["", "", ""])[:3]

        found.append({
            "sha": full_sha,
            "url": repo.commit_url(full_sha),
            "title": bits[2].strip(),
            "author_name": bits[0].strip(),
            "author_login": None,
            "date": bits[1].strip(),
            "position": (total - distance) if isinstance(total, int) and distance is not None else None,
            "distance_to_head": distance,
            "tags": tags_by_sha.get(full_sha, []),
        })

    return _finish(
        repo_key=repo_key, repo=repo.name, provider="csr", branch=target,
        branches_available=names, default_branch=default_branch,
        found=found, problems=problems, rejected=rejected, total_commits=total,
    )
