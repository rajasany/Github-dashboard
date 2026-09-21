"""Process an uploaded sheet of repo/folder rows into a release summary.

For each row: resolve the repository, find the latest commit touching the named
folder on the named branch, summarise what changed there, and propose a tag from
the user's convention.

Rows are processed concurrently but each row's outcome is independent — one that
cannot be resolved reports why and the rest still produce results.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from . import naming
from .config import Settings
from .models import detect_cherry_pick
from .paths import changed_directories

# Bounded so a large sheet cannot fan out into thousands of API calls.
MAX_ROWS = 100
# A mirror fetched this recently is reused rather than fetched again.
SYNC_MAX_AGE = 60.0
COMMITS_SCANNED = 100


class BulkError(Exception):
    pass


def resolve_repo_key(raw: str, settings: Settings) -> str | None:
    """Match a spreadsheet cell against the configured repositories.

    Accepts the full name, the bare repo name, a repo key, or a pasted URL, so
    whatever a colleague typed into the sheet has a fair chance of resolving.
    """
    text = (raw or "").strip().strip("/")
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        text = "/".join(text.split("/")[-2:])
    if text.endswith(".git"):
        text = text[: -len(".git")]

    known: list[tuple[str, str]] = [(f"github:{n}", n) for n in settings.repos]
    known += [(r.key, r.name) for r in settings.csr_repos]

    lowered = text.lower()
    for key, name in known:
        if lowered in (key.lower(), name.lower()):
            return key
    # Bare repository name, e.g. "Insurance" for "rajasany/Insurance".
    matches = [key for key, name in known if name.split("/")[-1].lower() == lowered]
    return matches[0] if len(matches) == 1 else None


async def _github_row(
    client: github_provider.GitHubClient,
    settings: Settings,
    repo_key: str,
    folder: str | None,
    branch: str | None,
    since: datetime,
) -> dict[str, Any]:
    full_name = repo_key.split(":", 1)[1]
    meta, branches, tags_by_sha = await asyncio.gather(
        client.get_repo(full_name),
        client.list_branches(full_name),
        client.list_tag_details(full_name),
    )
    names = [b.get("name") for b in branches if b.get("name")]
    target = branch or (meta or {}).get("default_branch") or (names[0] if names else None)
    if target and target not in names:
        raise BulkError(f"no branch “{target}” in {full_name}")

    raw = await client.list_commits(
        full_name, target, since.isoformat(), None, COMMITS_SCANNED
    )
    commits = [github_provider._commit_from_api(c, repo_key, full_name, target) for c in raw]  # noqa: SLF001

    # Folder scoping needs each commit's file list; the permanent cache makes
    # this cheap on anything already seen by the feed.
    if folder:
        scoped = await client.commit_shas_for_path(full_name, target, folder, 3) \
            if hasattr(client, "commit_shas_for_path") else None
        shas, _ = (scoped if scoped else await github_provider.commit_shas_for_path(
            client, full_name, target, folder, 3
        ))
        allowed = set(shas)
        commits = [c for c in commits if c["sha"] in allowed]

    all_tag_names = [t["name"] for group in tags_by_sha.values() for t in group]
    return {
        "repo": full_name,
        "branch": target,
        "commits": commits,
        "tags_by_sha": tags_by_sha,
        "all_tag_names": all_tag_names,
        "url": (meta or {}).get("html_url"),
    }


async def _csr_row(
    mirror: csr_provider.GitMirror,
    settings: Settings,
    repo: Any,
    folder: str | None,
    branch: str | None,
    since: datetime,
) -> dict[str, Any]:
    # Several rows commonly name one repository; one fetch per run serves them all.
    path = await mirror.sync(repo.key, repo.clone_url, max_age=SYNC_MAX_AGE)
    default = await mirror.default_branch(path)
    names = [n for n, _ in await mirror.branches(path)]
    target = branch or default or (names[0] if names else None)
    if target and target not in names:
        raise BulkError(f"no branch “{target}” in {repo.name}")

    args = ["-C", str(path), "log", f"--since={since.isoformat()}",
            f"--max-count={COMMITS_SCANNED}",
            f"--format={csr_provider._LOG_FORMAT}", "--name-only",  # noqa: SLF001
            "--diff-merges=first-parent", f"refs/heads/{target}"]
    if folder:
        args += ["--", folder]
    raw = await mirror._git(args)  # noqa: SLF001

    commits: list[dict[str, Any]] = []
    for record in raw.split(csr_provider._REC):  # noqa: SLF001
        if not record.strip():
            continue
        bits = record.split(csr_provider._FLD)  # noqa: SLF001
        if len(bits) < 4:
            continue
        message = csr_provider._FLD.join(bits[3:-1])  # noqa: SLF001
        paths = [ln.strip() for ln in bits[-1].splitlines() if ln.strip()]
        commits.append({
            "sha": bits[0].strip(), "author_name": bits[1].strip(), "date": bits[2].strip(),
            "title": message.strip().split("\n", 1)[0], "body": message,
            "url": repo.commit_url(bits[0].strip()),
            "_paths": paths, "files_changed": len(paths),
            "cherry_pick": detect_cherry_pick(message),
        })

    tags_by_sha = await mirror.tag_details(path)
    return {
        "repo": repo.name,
        "branch": target,
        "commits": commits,
        "tags_by_sha": tags_by_sha,
        "all_tag_names": [t["name"] for g in tags_by_sha.values() for t in g],
        "url": repo.web_url,
    }


async def process_rows(
    settings: Settings,
    gh_client: github_provider.GitHubClient,
    mirror: csr_provider.GitMirror,
    store: Any,
    *,
    rows: list[dict[str, Any]],
    convention: str,
    since_dt: datetime,
    default_branch: str | None = None,
) -> dict[str, Any]:
    from .feed import enrich_commit_folders

    if len(rows) > MAX_ROWS:
        raise BulkError(f"{len(rows)} rows is more than the {MAX_ROWS} this will process at once.")

    unknown = naming.describe_unknown(convention)
    if unknown:
        raise BulkError("Unknown placeholder(s) in the convention: " + ", ".join(f"{{{u}}}" for u in unknown))

    # Proposals are made against tags that already exist *and* against the other
    # proposals in this run, so two rows sharing a pattern do not both claim
    # the same number.
    claimed: dict[str, list[str]] = {}
    lock = asyncio.Lock()

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "row": row.get("_row"),
            "input_repo": row.get("repo", ""),
            "folder": (row.get("folder") or "").strip().strip("/") or None,
            "branch": (row.get("branch") or "").strip() or default_branch or None,
            "note": row.get("note") or "",
            "error": None,
        }

        repo_key = resolve_repo_key(row.get("repo", ""), settings)
        if not repo_key:
            out["error"] = f"“{row.get('repo','')}” is not a configured repository"
            return out
        out["repo_key"] = repo_key

        try:
            if repo_key.startswith("github:"):
                data = await _github_row(gh_client, settings, repo_key,
                                         out["folder"], out["branch"], since_dt)
                if settings.folders_enabled and data["commits"]:
                    await enrich_commit_folders(settings, gh_client, store, data["commits"])
            else:
                repo = next(r for r in settings.csr_repos if r.key == repo_key)
                data = await _csr_row(mirror, settings, repo, out["folder"], out["branch"], since_dt)
        except (BulkError, github_provider.GitHubError, csr_provider.CsrError) as exc:
            out["error"] = getattr(exc, "message", None) or str(exc)
            return out

        out["repo"] = data["repo"]
        out["branch"] = data["branch"]
        out["repo_url"] = data["url"]

        commits = data["commits"]
        out["commit_count"] = len(commits)
        if not commits:
            out["error"] = (
                f"no commits in {out['folder'] or 'the repository'} on {out['branch']} in this period"
            )
            return out

        latest = commits[0]
        out["latest_sha"] = latest["sha"]
        out["latest_short"] = latest["sha"][:10]
        out["latest_title"] = latest.get("title", "")
        out["latest_author"] = latest.get("author_name", "")
        out["latest_date"] = latest.get("date")
        out["latest_url"] = latest.get("url")

        out["authors"] = sorted({c.get("author_name", "") for c in commits if c.get("author_name")})
        out["files_changed"] = sum(int(c.get("files_changed") or 0) for c in commits)
        dates = sorted(c.get("date") or "" for c in commits if c.get("date"))
        out["first_change"] = dates[0] if dates else None
        out["last_change"] = dates[-1] if dates else None
        out["cherry_picks"] = sum(
            1 for c in commits if (c.get("cherry_pick") or {}).get("is_cherry_pick")
        )

        paths = [p for c in commits for p in (c.get("_paths") or c.get("paths") or [])]
        out["directories"] = changed_directories(paths, settings.folder_exclude)[:8]

        existing = [t["name"] for t in data["tags_by_sha"].get(latest["sha"], [])]
        out["existing_tags"] = existing

        async with lock:
            pool = list(data["all_tag_names"]) + claimed.get(repo_key, [])
            try:
                proposed = naming.render(
                    convention,
                    repo=data["repo"], folder=out["folder"], branch=out["branch"],
                    sha=latest["sha"], commit_date=latest.get("date"),
                    existing_tags=pool,
                )
            except naming.NamingError as exc:
                out["error"] = str(exc)
                return out
            claimed.setdefault(repo_key, []).append(proposed)

        out["proposed_tag"] = proposed
        out["tag_exists"] = proposed in data["all_tag_names"]
        return out

    results = list(await asyncio.gather(*(one(r) for r in rows)))
    ok = [r for r in results if not r.get("error")]

    return {
        "convention": convention,
        "since": since_dt.isoformat(),
        "rows": results,
        "total": len(results),
        "resolved": len(ok),
        "failed": len(results) - len(ok),
        "commits_total": sum(r.get("commit_count") or 0 for r in ok),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
