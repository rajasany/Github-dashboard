"""Find a commit by hash: which repo and branches hold it, and where it sits.

"Where it sits" is answered per branch, because a commit has no single position
in a repository — it can be an ancestor of several branches at different depths:

  distance_to_head   commits added on that branch since this one
  position           this commit's ordinal from the root (1 = first commit)
  total_commits      commits on the branch, so position reads as "N of M"

GitHub has no "which branches contain this commit" API, so containment is
established with one three-dot comparison per branch: `base=<sha> head=<branch>`
returns `behind_by == 0` exactly when the commit is an ancestor of the branch,
and `ahead_by` is then the distance to that branch's head.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings
from .paths import changed_directories, derive_folders

SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

# One comparison per branch is fine for a normal repo and abusive for one with
# hundreds of branches; past this we stop and say so.
MAX_BRANCHES_PROBED = 25

# Folder position is enumerated per directory the commit touched; both sides are
# bounded so a commit spanning many directories cannot fan out unboundedly.
MAX_FOLDERS_POSITIONED = 6
MAX_FOLDER_PAGES = 5


class LookupError_(Exception):
    pass


def normalise_sha(raw: str) -> str:
    """Accept a bare hash or anything that ends in one (a pasted commit URL)."""
    text = (raw or "").strip()
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    text = text.split("?")[0].split("#")[0]
    if not SHA_RE.match(text):
        raise LookupError_(
            "That does not look like a commit hash. Paste 4–40 hex characters, "
            "or a commit URL."
        )
    return text.lower()


async def _lookup_github(
    client: github_provider.GitHubClient, full_name: str, sha: str, settings: Settings
) -> dict[str, Any] | None:
    key = f"github:{full_name}"
    try:
        commit = await client._get(f"/repos/{full_name}/commits/{sha}")  # noqa: SLF001
    except github_provider.GitHubError as exc:
        if exc.status in (404, 422):
            return None  # not in this repository
        raise

    if not isinstance(commit, dict) or not commit.get("sha"):
        return None

    full_sha = commit["sha"]
    meta, branch_list = await asyncio.gather(
        client.get_repo(full_name), client.list_branches(full_name)
    )
    default_branch = (meta or {}).get("default_branch")
    names = [b.get("name") for b in branch_list if b.get("name")]
    probed, truncated = names[:MAX_BRANCHES_PROBED], max(0, len(names) - MAX_BRANCHES_PROBED)

    failed: list[str] = []

    async def probe(branch: str) -> dict[str, Any] | None:
        try:
            cmp_ = await client.get_compare(full_name, full_sha, branch)
        except github_provider.GitHubError:
            # A failed probe is not the same as "the branch does not contain it";
            # record it so the UI never implies an answer we did not get.
            failed.append(branch)
            return None
        # behind_by counts commits the *base* has that the head lacks. Zero means
        # this commit is reachable from the branch tip.
        if int(cmp_.get("behind_by") or 0) != 0:
            return None
        ahead = int(cmp_.get("ahead_by") or 0)
        total = await github_provider.count_branch_commits(client, full_name, branch)
        return {
            "name": branch,
            "is_default": branch == default_branch,
            "distance_to_head": ahead,
            "is_head": ahead == 0,
            "total_commits": total,
            "position": (total - ahead) if isinstance(total, int) else None,
        }

    found = [b for b in await asyncio.gather(*(probe(n) for n in probed)) if b]
    found.sort(key=lambda b: (not b["is_default"], b["distance_to_head"], b["name"]))

    tags_by_sha = await client.list_tag_details(full_name)
    paths = [f.get("filename", "") for f in (commit.get("files") or []) if isinstance(f, dict)]
    paths = [p for p in paths if p]
    folders = derive_folders(
        paths, settings.folder_depth, settings.folder_paths, settings.folder_exclude
    )
    # The directories the files actually live in — what you want when inspecting
    # one commit, as opposed to the service bucket used for grouping.
    directories = changed_directories(paths, settings.folder_exclude)
    author = (commit.get("commit") or {}).get("author") or {}
    gh_author = commit.get("author") or {}
    stats = commit.get("stats") or {}

    # Where this commit sits *within each folder it touched* — a different
    # question from its position in the branch as a whole. Counted only over
    # commits that touched that directory.
    folder_positions: list[dict[str, Any]] = []
    primary = found[0]["name"] if found else default_branch
    if primary:
        async def in_folder(directory: str) -> dict[str, Any]:
            path_filter = "" if directory == "(repo root)" else directory
            if not path_filter:
                # A path filter of "" would match the whole repository, which is
                # the number we are trying not to report.
                return {"folder": directory, "branch": primary, "position": None,
                        "total": None, "capped": False,
                        "note": "root-level files are not a directory"}
            try:
                shas_newest_first, capped = await github_provider.commit_shas_for_path(
                    client, full_name, primary, path_filter, MAX_FOLDER_PAGES
                )
            except github_provider.GitHubError:
                return {"folder": directory, "branch": primary, "position": None,
                        "total": None, "capped": False, "note": "could not be counted"}

            total = len(shas_newest_first)
            try:
                index = shas_newest_first.index(full_sha)
            except ValueError:
                return {"folder": directory, "branch": primary, "position": None,
                        "total": total if not capped else None, "capped": capped,
                        "note": "not found within the range searched" if capped else None}
            # Newest first, so the oldest is position 1.
            return {"folder": directory, "branch": primary, "position": total - index,
                    "total": total, "capped": capped, "note": None}

        folder_positions = list(
            await asyncio.gather(*(in_folder(d) for d in directories[:MAX_FOLDERS_POSITIONED]))
        )

    return {
        "provider": "github",
        "repo_key": key,
        "repo": full_name,
        "sha": full_sha,
        "url": commit.get("html_url"),
        "title": ((commit.get("commit") or {}).get("message") or "").split("\n", 1)[0],
        "body": "\n".join(((commit.get("commit") or {}).get("message") or "").split("\n")[1:]).strip(),
        "author_name": author.get("name") or gh_author.get("login") or "unknown",
        "author_login": gh_author.get("login"),
        "author_email": author.get("email"),
        "date": author.get("date"),
        "committer_name": ((commit.get("commit") or {}).get("committer") or {}).get("name"),
        "committer_date": ((commit.get("commit") or {}).get("committer") or {}).get("date"),
        "parents": [p.get("sha") for p in (commit.get("parents") or []) if p.get("sha")],
        "files_changed": len(commit.get("files") or []),
        "additions": stats.get("additions"),
        "deletions": stats.get("deletions"),
        "folders": folders,
        "directories": directories,
        "folder_positions": folder_positions,
        "folders_unpositioned": max(0, len(directories) - MAX_FOLDERS_POSITIONED),
        "branches": found,
        "branches_probed": len(probed),
        "branches_unprobed": truncated,
        "branches_failed": failed,
        "tags": tags_by_sha.get(full_sha, []),
        "nearest_tag": None,  # no cheap "describe" equivalent on the REST API
        "default_branch": default_branch,
    }


async def _lookup_csr(
    mirror: csr_provider.GitMirror, repo: Any, sha: str, settings: Settings
) -> dict[str, Any] | None:
    try:
        path = await mirror.sync(repo.key, repo.clone_url)
    except csr_provider.CsrError:
        return None

    async def git(args: list[str]) -> str:
        return await mirror._git(["-C", str(path), *args])  # noqa: SLF001

    try:
        full_sha = (await git(["rev-parse", "--verify", f"{sha}^{{commit}}"])).strip()
    except csr_provider.CsrError:
        return None
    if not full_sha:
        return None

    fields = ["%H", "%an", "%ae", "%aI", "%cn", "%cI", "%P", "%s", "%b"]
    raw = await git(["show", "-s", f"--format={csr_provider._FLD.join(fields)}", full_sha])  # noqa: SLF001
    bits = raw.split(csr_provider._FLD)  # noqa: SLF001
    (h, an, ae, ai, cn, ci, parents, subject) = (bits + [""] * 9)[:8]
    body = bits[8] if len(bits) > 8 else ""

    default_branch = await mirror.default_branch(path)
    contains = await git(["for-each-ref", "--contains", full_sha, "--format=%(refname:short)", "refs/heads/"])
    branch_names = [b.strip() for b in contains.splitlines() if b.strip()]

    async def describe(branch: str) -> dict[str, Any]:
        ahead = int((await git(["rev-list", "--count", f"{full_sha}..{branch}"])).strip() or 0)
        total = int((await git(["rev-list", "--count", branch])).strip() or 0)
        return {
            "name": branch,
            "is_default": branch == default_branch,
            "distance_to_head": ahead,
            "is_head": ahead == 0,
            "total_commits": total,
            "position": total - ahead,
        }

    found = list(await asyncio.gather(*(describe(b) for b in branch_names)))
    found.sort(key=lambda b: (not b["is_default"], b["distance_to_head"], b["name"]))

    tags_by_sha = await mirror.tag_details(path)

    # git can answer "the closest tag reachable from here" for free; GitHub cannot.
    nearest = None
    try:
        name = (await git(["describe", "--tags", "--abbrev=0", full_sha])).strip()
        if name and not any(t["name"] == name for t in tags_by_sha.get(full_sha, [])):
            behind = int((await git(["rev-list", "--count", f"{name}..{full_sha}"])).strip() or 0)
            nearest = {"name": name, "commits_after": behind}
    except csr_provider.CsrError:
        nearest = None

    numstat = await git(["show", "--numstat", "--format=", full_sha])
    adds = dels = files = 0
    changed_paths: list[str] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            changed_paths.append(parts[-1])
            if parts[0].isdigit():
                adds += int(parts[0])
            if parts[1].isdigit():
                dels += int(parts[1])
    folders = derive_folders(
        changed_paths, settings.folder_depth, settings.folder_paths, settings.folder_exclude
    )
    directories = changed_directories(changed_paths, settings.folder_exclude)

    # git counts path-scoped history directly: commits touching <dir> that are
    # reachable from this commit, over the same on that branch.
    folder_positions: list[dict[str, Any]] = []
    primary = found[0]["name"] if found else default_branch
    if primary:
        for directory in directories[:MAX_FOLDERS_POSITIONED]:
            if directory == "(repo root)":
                folder_positions.append({
                    "folder": directory, "branch": primary, "position": None,
                    "total": None, "capped": False,
                    "note": "root-level files are not a directory",
                })
                continue
            try:
                position = int((await git(
                    ["rev-list", "--count", full_sha, "--", directory]
                )).strip() or 0)
                total = int((await git(
                    ["rev-list", "--count", primary, "--", directory]
                )).strip() or 0)
            except csr_provider.CsrError:
                folder_positions.append({
                    "folder": directory, "branch": primary, "position": None,
                    "total": None, "capped": False, "note": "could not be counted",
                })
                continue
            folder_positions.append({
                "folder": directory, "branch": primary, "position": position,
                "total": total, "capped": False, "note": None,
            })

    return {
        "provider": "csr",
        "repo_key": repo.key,
        "repo": repo.name,
        "sha": h.strip() or full_sha,
        "url": repo.commit_url(full_sha),
        "title": subject.strip(),
        "body": body.strip(),
        "author_name": an.strip(),
        "author_login": None,
        "author_email": ae.strip(),
        "date": ai.strip(),
        "committer_name": cn.strip(),
        "committer_date": ci.strip(),
        "parents": parents.split(),
        "files_changed": files,
        "additions": adds,
        "deletions": dels,
        "folders": folders,
        "directories": directories,
        "folder_positions": folder_positions,
        "folders_unpositioned": max(0, len(directories) - MAX_FOLDERS_POSITIONED),
        "branches": found,
        "branches_probed": len(branch_names),
        "branches_unprobed": 0,
        "branches_failed": [],
        "tags": tags_by_sha.get(full_sha, []),
        "nearest_tag": nearest,
        "default_branch": default_branch,
    }


async def lookup_commit(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    raw_sha: str,
) -> dict[str, Any]:
    """Search every configured repository for the commit, in parallel."""
    sha = normalise_sha(raw_sha)

    tasks = [_lookup_github(gh_client, name, sha, settings) for name in settings.repos]
    tasks += [_lookup_csr(mirror, repo, sha, settings) for repo in settings.csr_repos]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    matches = [r for r in results if isinstance(r, dict)]
    errors = [str(r) for r in results if isinstance(r, Exception)]

    return {
        "query": raw_sha.strip(),
        "sha": sha,
        "found": bool(matches),
        "matches": matches,
        "searched": len(settings.repos) + len(settings.csr_repos),
        "errors": errors,
    }
