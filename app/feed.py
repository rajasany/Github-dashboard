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


async def _resolve_paths(
    gh_client: github_provider.GitHubClient,
    store: FileStore,
    targets: dict[str, tuple[str, str]],
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Look up each commit's changed-file list: disk cache first, API for the rest.

    GitHub's list-commits and compare endpoints both omit file lists, so this
    costs one API call per commit — but only per commit *ever*, since the store
    is permanent and a commit's files never change.
    """
    if not targets:
        return {}, {"requested": 0, "from_cache": 0, "fetched": 0, "failed": 0}

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

    return {**cached, **fetched}, {
        "requested": len(targets),
        "from_cache": len(cached),
        "fetched": len(fetched),
        "failed": failed,
    }


def _apply_paths(commit: dict[str, Any], hit: dict | None) -> None:
    if not hit:
        return
    commit["paths"] = hit["paths"]
    commit["files_changed"] = hit.get("files_changed", len(hit["paths"]))
    commit["files_truncated"] = bool(hit.get("truncated"))


async def _enrich_github_paths(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    store: FileStore,
    results: list[RepoResult],
) -> dict[str, Any]:
    """Attach changed-file paths to the GitHub commits inside a feed fan-out."""
    targets: dict[str, tuple[str, str]] = {}  # store key -> (repo full name, sha)
    for result in results:
        if result.provider != "github":
            continue
        for commit in result.commits:
            targets[f"{result.key}@{commit['sha']}"] = (result.name, commit["sha"])

    resolved, stats = await _resolve_paths(gh_client, store, targets)

    for result in results:
        if result.provider != "github":
            continue
        for commit in result.commits:
            _apply_paths(commit, resolved.get(f"{result.key}@{commit['sha']}"))

    return stats


async def enrich_commit_folders(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    store: FileStore,
    commits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fill `folders` and `files_changed` on a bare list of GitHub commits.

    Used by branch comparison, where commits arrive from the compare API rather
    than from a per-repo fan-out. Shares the feed's cache, so a commit already
    seen in the feed costs nothing here.
    """
    targets = {
        f"{c['repo_key']}@{c['sha']}": (c["repo_key"].split(":", 1)[1], c["sha"])
        for c in commits
        if c.get("provider") == "github" and c.get("sha")
    }
    resolved, stats = await _resolve_paths(gh_client, store, targets)

    for commit in commits:
        _apply_paths(commit, resolved.get(f"{commit['repo_key']}@{commit['sha']}"))
        commit["folders"] = derive_folders(
            commit.pop("paths", None) or [],
            settings.folder_depth,
            settings.folder_paths,
            settings.folder_exclude,
        )
    return stats


async def build_feed(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: FileStore,
    *,
    github_repos: list[str],
    csr_repos: list,
    since_dt: datetime,
    until_dt: datetime | None,
    commits_per_branch: int,
) -> dict[str, Any]:
    """Fan out across every provider, then fold into one feed.

    A commit reachable from several branches appears once, tagged with every
    branch it was found on — otherwise the default branch would duplicate every
    feature branch merged into it.

    The window is [since_dt, until_dt]; `until_dt` of None means "up to now".
    Both providers filter server-side — GitHub via the API's since/until params,
    CSR via `git log --since/--until` — so nothing outside the range is fetched.
    """
    since_dt = since_dt.replace(microsecond=0)
    since = since_dt.isoformat()
    until = until_dt.replace(microsecond=0).isoformat() if until_dt else None

    tasks = [
        github_provider.collect_repo(gh_client, name, since, until, commits_per_branch)
        for name in github_repos
    ] + [
        csr_provider.collect_repo(
            mirror, repo, since, since_dt, until, commits_per_branch, settings
        )
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
    # repo key -> {sha: [tag, ...]} for every tag in the repo, not just the ones
    # landing inside the date window — the UI needs the full set to answer
    # "what is the newest tag on this branch".
    tags_by_repo: dict[str, dict[str, list[str]]] = {}

    for result in results:
        errors.extend(result.errors)
        tags_by_repo[result.key] = result.tags

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
        # Tags pointing at this exact commit. Sorted so a release tag and its
        # aliases (v1.2.0, v1.2, latest) always list in a stable order.
        entry["tags"] = sorted(tags_by_repo.get(entry["repo_key"], {}).get(entry["sha"], []))

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

    span_days = max(1, round(((until_dt or datetime.now(timezone.utc)) - since_dt).total_seconds() / 86400))

    # Totals the UI and the report both lead with, computed once here so the two
    # can never disagree about what the same filter selected.
    files_changed = sum(int(c.get("files_changed") or 0) for c in feed)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "until": until,
        "window_days": span_days,
        "files_changed": files_changed,
        "repos": sorted(repo_meta, key=lambda r: (r["provider"], r["full_name"])),
        "commits": feed,
        "errors": errors,
        "rate_limit": gh_client.rate_limit.as_dict(),
        "providers": sorted({r["provider"] for r in repo_meta}),
        # Full sha -> tags map per repo, so the UI can resolve a tag for any
        # commit it holds without another round trip.
        "tags": tags_by_repo,
        "tags_total": sum(len(v) for m in tags_by_repo.values() for v in m.values()),
        "folders": {
            "enabled": settings.folders_enabled,
            "depth": settings.folder_depth,
            "patterns": settings.folder_paths,
            "exclude": settings.folder_exclude,
            **folder_stats,
        },
    }
