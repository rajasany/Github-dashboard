"""Async GitHub REST client + the aggregation that produces the unified commit feed."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .cache import TTLCache
from .config import Settings
from .models import RepoResult, branch_allowed, make_commit, make_tag

USER_AGENT = "github-change-dashboard/0.1"


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimit:
    """Last-seen rate limit headers, so the UI can show remaining budget."""

    def __init__(self) -> None:
        self.limit: int | None = None
        self.remaining: int | None = None
        self.reset: int | None = None

    def update(self, headers: httpx.Headers) -> None:
        if "x-ratelimit-limit" in headers:
            self.limit = _int_or_none(headers.get("x-ratelimit-limit"))
            self.remaining = _int_or_none(headers.get("x-ratelimit-remaining"))
            self.reset = _int_or_none(headers.get("x-ratelimit-reset"))

    def as_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "remaining": self.remaining, "reset": self.reset}


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = TTLCache(settings.cache_ttl)
        self.rate_limit = RateLimit()
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=settings.api_base,
            timeout=httpx.Timeout(20.0),
            # Renamed or transferred repos answer 301; without this the redirect
            # body ("Moved Permanently") would be parsed as if it were data.
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
                **({"Authorization": f"Bearer {settings.token}"} if settings.token else {}),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = f"{path}?{sorted((params or {}).items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        async with self._semaphore:
            try:
                response = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                raise GitHubError(f"Network error calling {path}: {exc}") from exc

        self.rate_limit.update(response.headers)

        if response.status_code == 401:
            raise GitHubError("GitHub rejected the token (401). Check GITHUB_TOKEN.", 401)
        if response.status_code == 403 and self.rate_limit.remaining == 0:
            raise GitHubError("GitHub API rate limit exhausted.", 403)
        if response.status_code == 403:
            raise GitHubError(f"Forbidden (403) for {path}. Token may lack access.", 403)
        if response.status_code == 404:
            raise GitHubError(f"Not found (404): {path}. Check the name and token access.", 404)
        if response.status_code == 409:
            # Empty repository — no commits yet.
            return []
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned {response.status_code} for {path}", response.status_code)

        data = response.json()
        self.cache.set(cache_key, data)
        return data

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        return await self._get(f"/repos/{full_name}")

    async def list_branches(self, full_name: str, max_pages: int = 3) -> list[dict[str, Any]]:
        branches: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = await self._get(
                f"/repos/{full_name}/branches", {"per_page": 100, "page": page}
            )
            if not isinstance(batch, list) or not batch:
                break
            branches.extend(batch)
            if len(batch) < 100:
                break
        return branches

    async def list_commits(
        self, full_name: str, branch: str, since: str, until: str | None, per_page: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"sha": branch, "since": since, "per_page": per_page}
        if until:
            params["until"] = until
        data = await self._get(f"/repos/{full_name}/commits", params)
        return data if isinstance(data, list) else []

    async def list_tags(self, full_name: str, max_pages: int = 3) -> dict[str, list[str]]:
        """Map commit sha -> tag names pointing at it.

        The `/tags` endpoint already dereferences annotated tags, so `commit.sha`
        is the commit itself rather than the tag object.
        """
        by_sha: dict[str, list[str]] = {}
        for page in range(1, max_pages + 1):
            batch = await self._get(f"/repos/{full_name}/tags", {"per_page": 100, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            for tag in batch:
                sha = ((tag or {}).get("commit") or {}).get("sha")
                name = (tag or {}).get("name")
                if sha and name:
                    by_sha.setdefault(sha, []).append(name)
            if len(batch) < 100:
                break
        return by_sha

    async def list_tag_details(self, full_name: str, max_pages: int = 3) -> dict[str, list[dict[str, Any]]]:
        """commit sha -> full tag records, including tagger and tag date.

        The plain `/tags` endpoint carries no tagger, so this walks the refs
        instead: `object.type == "tag"` means an annotated tag whose own object
        has to be fetched for its tagger; `"commit"` means a lightweight tag,
        which has no tagger anywhere.
        """
        refs: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            try:
                batch = await self._get(
                    f"/repos/{full_name}/git/refs/tags", {"per_page": 100, "page": page}
                )
            except GitHubError as exc:
                # A repo with no tags at all answers 404 here, which is not an error.
                if exc.status == 404:
                    return {}
                raise
            if not isinstance(batch, list) or not batch:
                break
            refs.extend(batch)
            if len(batch) < 100:
                break

        async def resolve(ref: dict[str, Any]) -> dict[str, Any] | None:
            name = str(ref.get("ref", "")).removeprefix("refs/tags/")
            obj = ref.get("object") or {}
            sha, kind = obj.get("sha"), obj.get("type")
            if not name or not sha:
                return None

            if kind != "tag":
                return make_tag(name=name, commit_sha=sha, annotated=False)

            try:
                detail = await self._get(f"/repos/{full_name}/git/tags/{sha}")
            except GitHubError:
                # Fall back to the ref alone rather than dropping the tag.
                return make_tag(name=name, commit_sha=sha, annotated=True)
            tagger = (detail or {}).get("tagger") or {}
            return make_tag(
                name=name,
                commit_sha=((detail or {}).get("object") or {}).get("sha") or sha,
                annotated=True,
                tagger_name=tagger.get("name"),
                tagger_email=tagger.get("email"),
                tagger_date=tagger.get("date"),
                message=(detail or {}).get("message"),
            )

        resolved = await asyncio.gather(*(resolve(r) for r in refs))

        by_sha: dict[str, list[dict[str, Any]]] = {}
        for tag in resolved:
            if tag:
                by_sha.setdefault(tag["commit_sha"], []).append(tag)
        for tags in by_sha.values():
            tags.sort(key=lambda t: t["name"])
        return by_sha

    async def get_compare(self, full_name: str, base: str, head: str) -> dict[str, Any]:
        """Three-dot comparison: what `head` has that `base` does not.

        GitHub caps the embedded commit list at 250 and the file list at 300;
        `total_commits` still reports the true figure so the UI can say so.
        """
        data = await self._get(
            f"/repos/{full_name}/compare/{base}...{head}", {"per_page": 250}
        )
        return data if isinstance(data, dict) else {}

    async def get_commit_files(self, full_name: str, sha: str) -> tuple[list[str], bool]:
        """Changed paths for one commit.

        The list-commits endpoint omits file lists, so folder attribution needs
        this extra call per commit. Results are immutable, hence cached on disk.
        Returns (paths, truncated) — GitHub caps `files` at 300 entries.
        """
        data = await self._get(f"/repos/{full_name}/commits/{sha}")
        if not isinstance(data, dict):
            return [], False
        files = data.get("files") or []
        paths = [f.get("filename", "") for f in files if isinstance(f, dict)]
        return [p for p in paths if p], len(files) >= 300


async def collect_branch(
    client: GitHubClient,
    full_name: str,
    branch: str,
    since: str,
    until: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Normalised commits for one branch only — no fan-out over the whole repo."""
    raw = await client.list_commits(full_name, branch, since, until, limit)
    key = f"github:{full_name}"
    return [_commit_from_api(c, key, full_name, branch) for c in raw]


async def count_branch_commits(client: GitHubClient, full_name: str, branch: str) -> int | None:
    """Total commits reachable from a branch tip.

    There is no count endpoint, so this asks for a single commit per page and
    reads the page number of the `last` link — that number *is* the total.
    Returns None when the header is absent (a branch of one page).
    """
    import re

    path = f"/repos/{full_name}/commits"
    params = {"sha": branch, "per_page": 1}
    async with client._semaphore:  # noqa: SLF001 — same package, needs the raw headers
        try:
            response = await client._client.get(path, params=params)  # noqa: SLF001
        except httpx.RequestError:
            return None
    client.rate_limit.update(response.headers)
    if response.status_code >= 400:
        return None

    link = response.headers.get("link", "")
    match = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
    if match:
        return int(match.group(1))
    # No `last` link means a single page: count what came back.
    body = response.json()
    return len(body) if isinstance(body, list) else None


async def commit_shas_for_path(
    client: GitHubClient,
    full_name: str,
    branch: str,
    path: str,
    max_pages: int = 5,
) -> tuple[list[str], bool]:
    """Every commit on `branch` that touched `path`, newest first.

    Returned as (shas, capped). `capped` is True when the history was longer than
    max_pages, in which case the caller must not present the count as complete.
    Enumerating is exact; the alternative — counting by date — misplaces commits
    whose author date was rewritten by a rebase.
    """
    shas: list[str] = []
    for page in range(1, max_pages + 1):
        batch = await client._get(  # noqa: SLF001
            f"/repos/{full_name}/commits",
            {"sha": branch, "path": path, "per_page": 100, "page": page},
        )
        if not isinstance(batch, list) or not batch:
            return shas, False
        shas.extend(c["sha"] for c in batch if isinstance(c, dict) and c.get("sha"))
        if len(batch) < 100:
            return shas, False
    return shas, True


def _commit_from_api(raw: dict[str, Any], repo_key: str, repo_name: str, branch: str) -> dict[str, Any]:
    commit = raw.get("commit") or {}
    author = commit.get("author") or {}
    gh_author = raw.get("author") or {}
    return make_commit(
        provider="github",
        repo_key=repo_key,
        repo_name=repo_name,
        sha=raw.get("sha", ""),
        message=commit.get("message") or "",
        author_name=author.get("name") or gh_author.get("login") or "unknown",
        author_login=gh_author.get("login"),
        avatar_url=gh_author.get("avatar_url"),
        date=author.get("date"),
        url=raw.get("html_url"),
        branch=branch,
    )


async def collect_repo(
    client: GitHubClient,
    full_name: str,
    since: str,
    until: str | None,
    commits_per_branch: int,
) -> RepoResult:
    """Fetch one GitHub repo's branches and their recent commits."""
    settings = client.settings
    key = f"github:{full_name}"

    try:
        meta, branches, tags = await asyncio.gather(
            client.get_repo(full_name),
            client.list_branches(full_name),
            client.list_tags(full_name),
        )
    except GitHubError as exc:
        return RepoResult(
            provider="github",
            key=key,
            name=full_name,
            url=f"https://github.com/{full_name}",
            default_branch=None,
            branches_total=0,
            branches_shown=0,
            errors=[{"repo": full_name, "error": exc.message}],
        )

    if not isinstance(meta, dict) or not meta.get("default_branch"):
        return RepoResult(
            provider="github",
            key=key,
            name=full_name,
            url=f"https://github.com/{full_name}",
            default_branch=None,
            branches_total=0,
            branches_shown=0,
            errors=[{"repo": full_name, "error": "Unexpected response shape from GitHub."}],
        )

    selected = [
        b
        for b in branches
        if branch_allowed(b.get("name", ""), settings.branch_include, settings.branch_exclude)
    ]

    errors: list[dict[str, str]] = []

    async def fetch(branch_name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return branch_name, await client.list_commits(
                full_name, branch_name, since, until, commits_per_branch
            )
        except GitHubError as exc:
            errors.append({"repo": f"{full_name}@{branch_name}", "error": exc.message})
            return branch_name, []

    results = await asyncio.gather(*(fetch(b["name"]) for b in selected))

    commits: list[dict[str, Any]] = []
    for branch_name, raw_commits in results:
        for raw in raw_commits:
            commits.append(_commit_from_api(raw, key, full_name, branch_name))

    return RepoResult(
        provider="github",
        key=key,
        name=full_name,
        url=meta.get("html_url"),
        default_branch=meta.get("default_branch"),
        branches_total=len(branches),
        branches_shown=len(selected),
        private=bool(meta.get("private", False)),
        commits=commits,
        errors=errors,
        tags=tags,
    )
