"""Creating tags: stage locally first, push to the remote only on request.

A tag on a shared remote is awkward to retract, so this is deliberately two
steps. Staging records the tag in this app's own SQLite store and nowhere else —
nothing leaves the machine. Pushing is a separate, explicitly confirmed action
that creates the tag on GitHub or in the CSR repository.

Staged tags are annotated: an annotated tag carries a tagger and a message,
which is what makes the "tag creator / tag date" columns meaningful. Creating a
lightweight tag would leave those permanently blank.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings

# git's own rules, trimmed to the cases a person is likely to hit.
_BAD_TAG = re.compile(r"(^[./-])|([./]$)|(\.\.)|(@\{)|([\x00-\x20~^:?*\[\\])|(\.lock$)|(//)")


class TagError(Exception):
    pass


def validate_tag_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise TagError("Give the tag a name.")
    if len(name) > 200:
        raise TagError("That tag name is too long.")
    if _BAD_TAG.search(name):
        raise TagError(
            "Not a valid git tag name. Avoid spaces, '~ ^ : ? * [ \\', '..', "
            "and leading or trailing '.' '/' '-'."
        )
    return name


class TagStore:
    """Staged tags, kept alongside the commit-file cache."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS staged_tags (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_key   TEXT NOT NULL,
                    repo       TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    message    TEXT NOT NULL DEFAULT '',
                    sha        TEXT NOT NULL,
                    commit_title TEXT NOT NULL DEFAULT '',
                    tagger     TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    pushed_at  REAL,
                    push_error TEXT,
                    UNIQUE (repo_key, name)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["pushed"] = data.get("pushed_at") is not None
        return data

    def add(self, **kw: Any) -> dict[str, Any]:
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO staged_tags
                       (repo_key, repo, name, message, sha, commit_title, tagger, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        kw["repo_key"], kw["repo"], kw["name"], kw.get("message", ""),
                        kw["sha"], kw.get("commit_title", ""), kw.get("tagger", ""), time.time(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise TagError(f"A tag named “{kw['name']}” is already staged for this repository.")
            row = conn.execute("SELECT * FROM staged_tags WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._row(row)

    def list(self, repo_key: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if repo_key:
                rows = conn.execute(
                    "SELECT * FROM staged_tags WHERE repo_key = ? ORDER BY created_at DESC", (repo_key,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM staged_tags ORDER BY created_at DESC").fetchall()
            return [self._row(r) for r in rows]

    def get(self, tag_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM staged_tags WHERE id = ?", (tag_id,)).fetchone()
            return self._row(row) if row else None

    def mark_pushed(self, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE staged_tags SET pushed_at = ?, push_error = NULL WHERE id = ?",
                (time.time(), tag_id),
            )

    def mark_failed(self, tag_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE staged_tags SET push_error = ? WHERE id = ?", (error[:400], tag_id))

    def delete(self, tag_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM staged_tags WHERE id = ?", (tag_id,)).rowcount > 0


# --------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------


async def stage_tag(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: TagStore,
    *,
    repo_key: str,
    sha: str,
    name: str,
    message: str,
) -> dict[str, Any]:
    """Record a tag locally after checking the commit and the name are usable."""
    name = validate_tag_name(name)

    if repo_key.startswith("github:"):
        full_name = repo_key.split(":", 1)[1]
        try:
            commit = await gh_client._get(f"/repos/{full_name}/commits/{sha}")  # noqa: SLF001
        except github_provider.GitHubError as exc:
            raise TagError(f"That commit is not in {full_name}: {exc.message}") from exc
        full_sha = (commit or {}).get("sha")
        title = ((commit or {}).get("commit") or {}).get("message", "").split("\n", 1)[0]
        existing = await gh_client.list_tag_details(full_name)
        repo_name = full_name
    else:
        repo = next((r for r in settings.csr_repos if r.key == repo_key), None)
        if repo is None:
            raise TagError(f"Unknown repository: {repo_key}")
        try:
            path = await mirror.sync(repo.key, repo.clone_url)
            full_sha = (
                await mirror._git(["-C", str(path), "rev-parse", "--verify", f"{sha}^{{commit}}"])  # noqa: SLF001
            ).strip()
            title = (
                await mirror._git(["-C", str(path), "show", "-s", "--format=%s", full_sha])  # noqa: SLF001
            ).strip()
            existing = await mirror.tag_details(path)
        except csr_provider.CsrError as exc:
            raise TagError(str(exc)) from exc
        repo_name = repo.name

    if not full_sha:
        raise TagError("Could not resolve that commit.")

    # Refuse to shadow a tag that already exists on the remote — pushing it later
    # would either fail or, worse, look like it moved.
    for tags in existing.values():
        for tag in tags:
            if tag["name"] == name:
                raise TagError(
                    f"“{name}” already exists in {repo_name}, on commit {tag['commit_sha'][:7]}."
                )

    return store.add(
        repo_key=repo_key,
        repo=repo_name,
        name=name,
        message=(message or "").strip(),
        sha=full_sha,
        commit_title=title,
        tagger=settings.tagger_name or "Repo Change Dashboard",
    )


# --------------------------------------------------------------------------
# pushing
# --------------------------------------------------------------------------


async def push_tag(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: TagStore,
    tag_id: int,
) -> dict[str, Any]:
    """Create the staged tag on the remote. This is the step that leaves the machine."""
    staged = store.get(tag_id)
    if staged is None:
        raise TagError("That staged tag no longer exists.")
    if staged["pushed"]:
        raise TagError(f"“{staged['name']}” has already been pushed.")

    try:
        if staged["repo_key"].startswith("github:"):
            await _push_github(gh_client, staged, settings)
        else:
            await _push_csr(settings, mirror, staged)
    except Exception as exc:  # surfaced to the UI, and recorded on the row
        store.mark_failed(tag_id, str(exc))
        raise TagError(str(exc)) from exc

    store.mark_pushed(tag_id)
    # The tag list for this repo is now stale.
    gh_client.cache.clear()
    return store.get(tag_id) or {}


async def _push_github(
    client: github_provider.GitHubClient, staged: dict[str, Any], settings: Settings
) -> None:
    full_name = staged["repo_key"].split(":", 1)[1]
    if not settings.token:
        raise TagError("Pushing a tag to GitHub needs a GITHUB_TOKEN with write access.")

    # Two calls: the annotated tag object, then the ref that points at it.
    tag_obj = await _gh_post(
        client,
        f"/repos/{full_name}/git/tags",
        {
            "tag": staged["name"],
            "message": staged["message"] or staged["name"],
            "object": staged["sha"],
            "type": "commit",
            "tagger": {
                "name": settings.tagger_name or "Repo Change Dashboard",
                "email": settings.tagger_email or "noreply@example.invalid",
                "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        },
    )
    await _gh_post(
        client,
        f"/repos/{full_name}/git/refs",
        {"ref": f"refs/tags/{staged['name']}", "sha": tag_obj["sha"]},
    )


async def _gh_post(
    client: github_provider.GitHubClient, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    async with client._semaphore:  # noqa: SLF001
        response = await client._client.post(path, json=payload)  # noqa: SLF001
    client.rate_limit.update(response.headers)
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("message") or json.dumps(body)[:200]
        except Exception:
            detail = response.text[:200]
        if response.status_code in (401, 403):
            raise TagError(
                f"GitHub refused the write ({response.status_code}). The token needs "
                f"push access to this repository. {detail}"
            )
        raise TagError(f"GitHub returned {response.status_code}: {detail}")
    return response.json()


async def _push_csr(
    settings: Settings, mirror: csr_provider.GitMirror, staged: dict[str, Any]
) -> None:
    repo = next((r for r in settings.csr_repos if r.key == staged["repo_key"]), None)
    if repo is None:
        raise TagError(f"Unknown repository: {staged['repo_key']}")

    path = await mirror.sync(repo.key, repo.clone_url)
    token = await mirror.tokens.get()
    name, message, sha = staged["name"], staged["message"] or staged["name"], staged["sha"]

    # Identity for the annotated tag object, supplied per-invocation so nothing
    # depends on the machine's global git config.
    ident = {
        "GIT_COMMITTER_NAME": settings.tagger_name or "Repo Change Dashboard",
        "GIT_COMMITTER_EMAIL": settings.tagger_email or "noreply@example.invalid",
        "GIT_AUTHOR_NAME": settings.tagger_name or "Repo Change Dashboard",
        "GIT_AUTHOR_EMAIL": settings.tagger_email or "noreply@example.invalid",
    }
    await mirror._git(  # noqa: SLF001
        ["-C", str(path), "-c", f"user.name={ident['GIT_COMMITTER_NAME']}",
         "-c", f"user.email={ident['GIT_COMMITTER_EMAIL']}",
         "tag", "-a", name, "-m", message, sha]
    )
    try:
        # `remote.origin.mirror` is set by `clone --mirror`, and under it a push
        # synchronises *every* ref — including deleting remote refs that are gone
        # locally. Disable it for this invocation and name one explicit refspec,
        # so the only thing that can reach the remote is this single tag.
        await mirror._git(  # noqa: SLF001
            [
                "-C", str(path),
                "-c", "remote.origin.mirror=false",
                "push", "origin", f"refs/tags/{name}:refs/tags/{name}",
            ],
            token=token,
        )
    except csr_provider.CsrError as exc:
        # Roll the local tag back so a retry is not blocked by our own leftover.
        await mirror._git(["-C", str(path), "tag", "-d", name])  # noqa: SLF001
        raise TagError(f"Push rejected: {exc}") from exc


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------



# Cost guards: for GitHub, "which branches contain this commit" costs one
# comparison per (tag, branch) pair, so both sides are bounded and the caller is
# told when a bound was hit rather than shown a quietly partial answer.
MAX_TAGS_FOR_BRANCHES = 30
MAX_BRANCHES_FOR_TAGS = 10


async def tags_for_repo(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: TagStore,
    repo_key: str,
    enrich_folders,
) -> dict[str, Any]:
    """One repository's tags, each with the folders and branches of its commit.

    A tag names a commit, not a branch, so "the branch a tag was created on" is
    really *the branches whose history contains that commit* — often more than
    one. That is what the branch column reports.
    """
    from .paths import derive_folders

    capped = {"tags": 0, "branches": 0}

    if repo_key.startswith("github:"):
        full_name = repo_key.split(":", 1)[1]
        try:
            by_sha = await gh_client.list_tag_details(full_name)
            meta, branch_list = await asyncio.gather(
                gh_client.get_repo(full_name), gh_client.list_branches(full_name)
            )
        except github_provider.GitHubError as exc:
            return {
                "repo_key": repo_key, "repo": full_name, "provider": "github",
                "tags": [], "error": exc.message, "branches_known": [],
                "capped": capped, "staged": store.list(repo_key),
            }

        default_branch = (meta or {}).get("default_branch")
        all_branches = [b.get("name") for b in branch_list if b.get("name")]
        branches = all_branches[:MAX_BRANCHES_FOR_TAGS]
        capped["branches"] = max(0, len(all_branches) - len(branches))

        shas = list(by_sha)[:MAX_TAGS_FOR_BRANCHES]
        capped["tags"] = max(0, len(by_sha) - len(shas))

        commits = [{"provider": "github", "repo_key": repo_key, "sha": sha} for sha in by_sha]
        if settings.folders_enabled and commits:
            await enrich_folders(commits)
        folders_by_sha = {c["sha"]: c.get("folders") or [] for c in commits}

        async def containing(sha: str) -> tuple[str, list[str]]:
            async def holds(branch: str) -> str | None:
                try:
                    cmp_ = await gh_client.get_compare(full_name, sha, branch)
                except github_provider.GitHubError:
                    return None
                return branch if int(cmp_.get("behind_by") or 0) == 0 else None

            hits = [b for b in await asyncio.gather(*(holds(b) for b in branches)) if b]
            hits.sort(key=lambda b: (b != default_branch, b))
            return sha, hits

        found = dict(await asyncio.gather(*(containing(s) for s in shas)))

        tags = [
            {
                **tag,
                "folders": folders_by_sha.get(sha, []),
                # None (rather than []) means "not probed", which the UI shows as
                # unknown instead of claiming no branch holds it.
                "branches": found.get(sha) if sha in found else None,
            }
            for sha, group in by_sha.items()
            for tag in group
        ]
        return {
            "repo_key": repo_key, "repo": full_name, "provider": "github",
            "tags": tags, "error": None, "default_branch": default_branch,
            "branches_known": all_branches, "capped": capped,
            "staged": store.list(repo_key),
        }

    repo = next((r for r in settings.csr_repos if r.key == repo_key), None)
    if repo is None:
        raise TagError(f"Unknown repository: {repo_key}")

    try:
        path = await mirror.sync(repo.key, repo.clone_url)
        by_sha = await mirror.tag_details(path)
        default_branch = await mirror.default_branch(path)
        all_branches = [name for name, _ in await mirror.branches(path)]
    except csr_provider.CsrError as exc:
        return {
            "repo_key": repo_key, "repo": repo.name, "provider": "csr",
            "tags": [], "error": str(exc), "branches_known": [],
            "capped": capped, "staged": store.list(repo_key),
        }

    tags = []
    for sha, group in by_sha.items():
        # Local git answers containment directly — no per-branch probing needed.
        try:
            raw_branches = await mirror._git(  # noqa: SLF001
                ["-C", str(path), "for-each-ref", "--contains", sha,
                 "--format=%(refname:short)", "refs/heads/"]
            )
            holders = [b.strip() for b in raw_branches.splitlines() if b.strip()]
            holders.sort(key=lambda b: (b != default_branch, b))
        except csr_provider.CsrError:
            holders = None

        try:
            numstat = await mirror._git(  # noqa: SLF001
                ["-C", str(path), "show", "--numstat", "--format=", sha]
            )
        except csr_provider.CsrError:
            numstat = ""
        paths = [ln.split("\t")[-1] for ln in numstat.splitlines() if ln.count("\t") >= 2]
        folders = derive_folders(
            paths, settings.folder_depth, settings.folder_paths, settings.folder_exclude
        )
        tags.extend({**tag, "folders": folders, "branches": holders} for tag in group)

    tags.sort(key=lambda x: (x.get("tagger_date") or "", x["name"]), reverse=True)
    return {
        "repo_key": repo_key, "repo": repo.name, "provider": "csr",
        "tags": tags, "error": None, "default_branch": default_branch,
        "branches_known": all_branches, "capped": capped,
        "staged": store.list(repo_key),
    }


async def tag_overview(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: TagStore,
    enrich_folders,
) -> dict[str, Any]:
    """Every tag in every configured repo, with the folders its commit touched."""
    from .paths import derive_folders

    async def for_github(full_name: str) -> dict[str, Any]:
        key = f"github:{full_name}"
        try:
            by_sha = await gh_client.list_tag_details(full_name)
        except github_provider.GitHubError as exc:
            return {"key": key, "repo": full_name, "provider": "github", "tags": [], "error": exc.message}

        commits = [{"provider": "github", "repo_key": key, "sha": sha} for sha in by_sha]
        if settings.folders_enabled and commits:
            await enrich_folders(commits)
        folders_by_sha = {c["sha"]: c.get("folders") or [] for c in commits}

        tags = [
            {**tag, "folders": folders_by_sha.get(sha, [])}
            for sha, group in by_sha.items()
            for tag in group
        ]
        return {"key": key, "repo": full_name, "provider": "github", "tags": tags, "error": None}

    async def for_csr(repo: Any) -> dict[str, Any]:
        try:
            path = await mirror.sync(repo.key, repo.clone_url)
            by_sha = await mirror.tag_details(path)
        except csr_provider.CsrError as exc:
            return {"key": repo.key, "repo": repo.name, "provider": "csr", "tags": [], "error": str(exc)}

        tags = []
        for sha, group in by_sha.items():
            try:
                raw = await mirror._git(  # noqa: SLF001
                    ["-C", str(path), "show", "--numstat", "--format=", sha]
                )
            except csr_provider.CsrError:
                raw = ""
            paths = [ln.split("\t")[-1] for ln in raw.splitlines() if ln.count("\t") >= 2]
            folders = derive_folders(
                paths, settings.folder_depth, settings.folder_paths, settings.folder_exclude
            )
            tags.extend({**tag, "folders": folders} for tag in group)
        return {"key": repo.key, "repo": repo.name, "provider": "csr", "tags": tags, "error": None}

    results = await asyncio.gather(
        *[for_github(n) for n in settings.repos], *[for_csr(r) for r in settings.csr_repos]
    )

    for entry in results:
        entry["tags"].sort(key=lambda t: (t.get("tagger_date") or "", t["name"]), reverse=True)

    return {
        "repos": sorted(results, key=lambda r: r["repo"]),
        "total": sum(len(r["tags"]) for r in results),
        "staged": store.list(),
    }
