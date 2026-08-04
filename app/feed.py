"""Merges every provider into one deduped, sorted commit feed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings
from .models import RepoResult
from .paths import derive_folders
from .store import FileStore


async def _enrich_github_paths(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    store: FileStore,
    results: list[RepoResult],
) -> dict[str, Any]:
    """Attach changed-file paths to GitHub commits, using the disk cache first.

    GitHub's list-commits endpoint has no file list, so this costs one API call
    per commit — but only per commit *ever*, since the store is permanent.
    """
    targets: dict[str, tuple[str, str]] = {}  # store key -> (repo full name, sha)
    for result in results:
        if result.provider != "github":
            continue
        for commit in result.commits:
            targets[f"{result.key}@{commit['sha']}"] = (result.name, commit["sha"])

    if not targets:
        return {"requested": 0, "from_cache": 0, "fetched": 0, "failed": 0}

    cached = await store.get_many(list(targets))
    missing = {k: v for k, v in targets.items() if k not in cached}

    fetched: dict[str, dict] = {}
    failed = 0

    async def fetch(store_key: str, full_name: str, sha: str) -> None:
        nonlocal failed
        try:
            paths, truncated = await gh_client.get_commit_files(full_name, sha)
        except github_provider.GitHubError:
            # Folder data is best-effort: a failure here must not sink the feed.
            failed += 1
            return
        fetched[store_key] = {
            "paths": paths,
            "files_changed": len(paths),
            "truncated": truncated,
        }

    await asyncio.gather(*(fetch(k, name, sha) for k, (name, sha) in missing.items()))

    if fetched:
        await store.put_many(
            [
                (key, val["paths"], val["files_changed"], val["truncated"])
                for key, val in fetched.items()
            ]
        )

    resolved = {**cached, **fetched}
    for result in results:
        if result.provider != "github":
            continue
        for commit in result.commits:
            hit = resolved.get(f"{result.key}@{commit['sha']}")
            if hit:
                commit["paths"] = hit["paths"]
                commit["files_changed"] = hit.get("files_changed", len(hit["paths"]))
                commit["files_truncated"] = bool(hit.get("truncated"))

    return {
        "requested": len(targets),
        "from_cache": len(cached),
        "fetched": len(fetched),
        "failed": failed,
    }


async def build_feed(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: FileStore,
    *,
    github_repos: list[str],
    csr_repos: list,
    days: int,
    commits_per_branch: int,
) -> dict[str, Any]:
    """Fan out across every provider, then fold into one feed.

    A commit reachable from several branches appears once, tagged with every
    branch it was found on — otherwise the default branch would duplicate every
    feature branch merged into it.
    """
    since_dt = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0)
    since = since_dt.isoformat()

    tasks = [
        github_provider.collect_repo(gh_client, name, since, commits_per_branch)
        for name in github_repos
    ] + [
        csr_provider.collect_repo(mirror, repo, since, since_dt, commits_per_branch, settings)
        for repo in csr_repos
    ]

    results: list[RepoResult] = list(await asyncio.gather(*tasks))

    folder_stats: dict[str, Any] = {}
    if settings.folders_enabled:
        # CSR paths already arrived with the git log; only GitHub needs enriching.
        folder_stats = await _enrich_github_paths(settings, gh_client, store, results)

    errors: list[dict[str, str]] = []
    repo_meta: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}

    for result in results:
        errors.extend(result.errors)

        active: set[str] = set()
        for commit in result.commits:
            # Provider + repo + sha: a sha is only unique within one repository.
            dedup_key = f"{result.key}@{commit['sha']}"
            branch = commit["branches"][0]
            active.add(branch)

            entry = merged.get(dedup_key)
            if entry is None:
                merged[dedup_key] = commit
            elif branch not in entry["branches"]:
                entry["branches"].append(branch)

        repo_meta.append(result.meta(branches_active=len(active)))

    feed = sorted(merged.values(), key=lambda c: c["date"] or "", reverse=True)

    defaults = {r["key"]: r["default_branch"] for r in repo_meta}
    for entry in feed:
        default_branch = defaults.get(entry["repo_key"])
        entry["on_default"] = bool(default_branch) and default_branch in entry["branches"]
        entry["branches"].sort(key=lambda b: (b != default_branch, b))

        if settings.folders_enabled:
            entry["folders"] = derive_folders(
                entry.get("paths") or [],
                settings.folder_depth,
                settings.folder_paths,
                settings.folder_exclude,
            )
        # Raw paths are only needed to derive folders; don't ship them to the browser.
        entry.pop("paths", None)

    # Per-repo folder rollup, so the UI can show which services each repo contains.
    for meta in repo_meta:
        touched = sorted(
            {f for c in feed if c["repo_key"] == meta["key"] for f in c["folders"]}
        )
        meta["folders"] = touched

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "window_days": days,
        "repos": sorted(repo_meta, key=lambda r: (r["provider"], r["full_name"])),
        "commits": feed,
        "errors": errors,
        "rate_limit": gh_client.rate_limit.as_dict(),
        "providers": sorted({r["provider"] for r in repo_meta}),
        "folders": {
            "enabled": settings.folders_enabled,
            "depth": settings.folder_depth,
            "patterns": settings.folder_paths,
            "exclude": settings.folder_exclude,
            **folder_stats,
        },
    }
