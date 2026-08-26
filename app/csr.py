"""Google Cloud Source Repositories provider.

CSR has no REST API for branches or commit history — sourcerepo.googleapis.com
only lists/creates/deletes repositories. So history is read from git itself: a
bare `--mirror` clone kept in a local cache directory, refreshed with `git fetch`
and queried with `for-each-ref` / `git log`.

Auth is an OAuth access token from gcloud, handed to git as an Authorization
header via GIT_CONFIG_* environment variables so it never appears in argv.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CsrRepo, Settings
from .models import RepoResult, branch_allowed, make_commit, make_tag

# git log record/field separators — chosen so commit messages can't contain them.
_REC = "\x1e"
_FLD = "\x1f"
# The record separator leads each entry so that --name-only file lists, which git
# appends after the format output, land at the tail of the record.
_LOG_FORMAT = _REC + _FLD.join(["%H", "%an", "%aI", "%B"]) + _FLD

TOKEN_TTL = 45 * 60  # gcloud access tokens last ~60 min; refresh early.


class CsrError(Exception):
    # Set on subclasses the UI needs to react to specifically, e.g. offering a
    # sign-in link rather than just printing the message. None means "generic".
    code: str | None = None


class GcloudAuthRequired(CsrError):
    code = "gcloud_auth_required"


class TokenProvider:
    """Caches `gcloud auth print-access-token` output."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if self.settings.gcloud_token:
            return self.settings.gcloud_token

        async with self._lock:
            if self._token and (time.monotonic() - self._fetched_at) < TOKEN_TTL:
                return self._token

            gcloud = shutil.which("gcloud")
            if not gcloud:
                raise CsrError("`gcloud` not found on PATH. Install the Google Cloud SDK.")

            cmd = [gcloud, "auth", "print-access-token"]
            if self.settings.gcloud_account:
                cmd.append(f"--account={self.settings.gcloud_account}")

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                detail = (err or b"").decode().strip().splitlines()
                hint = detail[-1] if detail else "unknown error"
                raise GcloudAuthRequired(f"Not signed in to Google Cloud. ({hint})")

            self._token = out.decode().strip()
            self._fetched_at = time.monotonic()
            return self._token

    async def is_authenticated(self) -> bool:
        """Cheap local check — reads gcloud's credential store, no network call."""
        if self.settings.gcloud_token:
            return True
        gcloud = shutil.which("gcloud")
        if not gcloud:
            return False
        proc = await asyncio.create_subprocess_exec(
            gcloud,
            "auth",
            "list",
            "--filter=status:ACTIVE",
            "--format=value(account)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return proc.returncode == 0 and bool(out.decode().strip())


class GitMirror:
    """Bare mirror clones of remote repos, read locally."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.mirror_dir
        self.tokens = TokenProvider(settings)
        # Network git operations are serialised more tightly than local reads.
        self._net = asyncio.Semaphore(max(1, min(4, settings.max_concurrency)))

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        safe = key.replace(":", "_").replace("/", "_")[:60]
        return self.root / f"{safe}-{digest}.git"

    async def _git(
        self, args: list[str], *, token: str | None = None, cwd: Path | None = None
    ) -> str:
        env = dict(os.environ)
        # Never block on an interactive credential or SSH prompt.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        env["GCM_INTERACTIVE"] = "never"
        if token:
            # Passed via env, not argv, so the token stays out of `ps` output.
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.git_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise CsrError(f"git timed out after {self.settings.git_timeout}s: git {args[0]}")

        if proc.returncode != 0:
            message = (err or b"").decode().strip() or (out or b"").decode().strip()
            raise CsrError(_scrub(message.splitlines()[-1] if message else "git failed"))
        return (out or b"").decode()

    async def sync(self, key: str, clone_url: str) -> Path:
        """Clone on first use, otherwise fetch. Returns the local mirror path."""
        path = self.path_for(key)
        token = await self.tokens.get()

        async with self._net:
            if (path / "HEAD").exists():
                await self._git(
                    ["-C", str(path), "fetch", "--prune", "--quiet", "origin"], token=token
                )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                await self._git(
                    ["clone", "--mirror", "--quiet", clone_url, str(path)], token=token
                )
        return path

    async def default_branch(self, path: Path) -> str | None:
        try:
            name = (await self._git(["-C", str(path), "symbolic-ref", "--short", "HEAD"])).strip()
            if name:
                return name
        except CsrError:
            pass
        for candidate in ("main", "master"):
            try:
                await self._git(["-C", str(path), "rev-parse", "--verify", f"refs/heads/{candidate}"])
                return candidate
            except CsrError:
                continue
        return None

    async def branches(self, path: Path) -> list[tuple[str, str]]:
        """(branch name, tip commit date) for every local head."""
        raw = await self._git(
            [
                "-C",
                str(path),
                "for-each-ref",
                f"--format=%(refname:short){_FLD}%(committerdate:iso-strict)",
                "refs/heads/",
            ]
        )
        out: list[tuple[str, str]] = []
        for line in raw.splitlines():
            if _FLD in line:
                name, _, date = line.partition(_FLD)
                out.append((name.strip(), date.strip()))
        return out

    async def tags(self, path: Path) -> dict[str, list[str]]:
        """Map commit sha -> tag names pointing at it.

        `%(*objectname)` is the dereferenced target of an annotated tag and is
        empty for a lightweight one, so prefer it and fall back to `%(objectname)`.
        """
        raw = await self._git(
            [
                "-C",
                str(path),
                "for-each-ref",
                f"--format=%(refname:short){_FLD}%(objectname){_FLD}%(*objectname)",
                "refs/tags/",
            ]
        )
        by_sha: dict[str, list[str]] = {}
        for line in raw.splitlines():
            bits = line.split(_FLD)
            if len(bits) < 3:
                continue
            name, direct, dereferenced = bits[0].strip(), bits[1].strip(), bits[2].strip()
            sha = dereferenced or direct
            if name and sha:
                by_sha.setdefault(sha, []).append(name)
        return by_sha

    async def tag_details(self, path: Path) -> dict[str, list[dict[str, Any]]]:
        """commit sha -> full tag records. One call: git exposes every field.

        `taggername`/`taggerdate` are populated only for annotated tags; a
        lightweight tag has no tag object and therefore no author or date.
        """
        fields = [
            "%(refname:short)",
            "%(objecttype)",
            "%(objectname)",
            "%(*objectname)",
            "%(taggername)",
            "%(taggeremail)",
            "%(taggerdate:iso-strict)",
            "%(contents:subject)",
        ]
        raw = await self._git(
            ["-C", str(path), "for-each-ref", f"--format={_FLD.join(fields)}", "refs/tags/"]
        )

        by_sha: dict[str, list[dict[str, Any]]] = {}
        for line in raw.splitlines():
            bits = line.split(_FLD)
            if len(bits) < 8:
                continue
            name, kind, obj, deref, tagger, email, date, subject = (b.strip() for b in bits[:8])
            commit_sha = deref or obj
            if not name or not commit_sha:
                continue
            annotated = kind == "tag"
            by_sha.setdefault(commit_sha, []).append(
                make_tag(
                    name=name,
                    commit_sha=commit_sha,
                    annotated=annotated,
                    tagger_name=tagger,
                    tagger_email=email.strip("<>") if email else None,
                    tagger_date=date,
                    # For a lightweight tag `contents:subject` falls through to the
                    # *commit's* subject. Showing that as a tag message would be a
                    # plausible-looking lie, so drop it.
                    message=subject if annotated else None,
                )
            )
        for tags in by_sha.values():
            tags.sort(key=lambda t: t["name"])
        return by_sha

    async def log(
        self,
        path: Path,
        branch: str,
        since: str,
        until: str | None,
        limit: int,
        with_files: bool,
    ) -> list[dict[str, Any]]:
        args = [
            "-C",
            str(path),
            "log",
            f"--since={since}",
            f"--max-count={limit}",
            f"--format={_LOG_FORMAT}",
            f"refs/heads/{branch}",
        ]
        if until:
            args.insert(-1, f"--until={until}")
        if with_files:
            # --diff-merges=first-parent makes merge commits report a file list,
            # matching what the GitHub API returns for a merge.
            args.insert(-1, "--name-only")
            args.insert(-1, "--diff-merges=first-parent")
        else:
            args.insert(-1, "--no-patch")

        raw = await self._git(args)

        commits: list[dict[str, Any]] = []
        for record in raw.split(_REC):
            if not record.strip():
                continue
            parts = record.split(_FLD)
            if len(parts) < 4:
                continue

            sha, author, date = parts[0], parts[1], parts[2]
            if with_files:
                # Fields are sha|author|date|message|<files>; the message may itself
                # contain a separator, so take from both ends inward.
                message = _FLD.join(parts[3:-1])
                files = [line.strip() for line in parts[-1].splitlines() if line.strip()]
            else:
                message = _FLD.join(parts[3:])
                files = []

            commits.append(
                {
                    "sha": sha.strip(),
                    "author": author.strip(),
                    "date": date.strip(),
                    "message": message,
                    "paths": files,
                }
            )
        return commits


def _scrub(text: str) -> str:
    """Keep bearer tokens out of anything shown in the UI."""
    if "Bearer " in text:
        text = text.split("Bearer ")[0] + "Bearer <redacted>"
    return text[:400]


def _tip_is_recent(tip: str, since_dt: datetime) -> bool:
    """Compare branch tip to the window start.

    Must parse, not string-compare: git emits the committer's local offset
    (`+05:30`) while `since` is UTC, so lexicographic order is meaningless.
    """
    try:
        parsed = datetime.fromisoformat(tip)
    except ValueError:
        return True  # unparseable — keep the branch rather than silently drop it
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= since_dt


async def collect_repo(
    mirror: GitMirror,
    repo: CsrRepo,
    since: str,
    since_dt: datetime,
    until: str | None,
    commits_per_branch: int,
    settings: Settings,
) -> RepoResult:
    """Mirror one CSR repo and turn its recent history into normalised commits."""
    try:
        path = await mirror.sync(repo.key, repo.clone_url)
    except CsrError as exc:
        return RepoResult(
            provider="csr",
            key=repo.key,
            name=repo.name,
            url=repo.web_url,
            default_branch=None,
            branches_total=0,
            branches_shown=0,
            private=True,
            errors=[{"repo": repo.name, "error": str(exc), "code": exc.code}],
        )

    errors: list[dict[str, str]] = []
    default = await mirror.default_branch(path)
    all_branches = await mirror.branches(path)
    try:
        repo_tags = await mirror.tags(path)
    except CsrError:
        repo_tags = {}  # tags are decoration, never a reason to lose the feed

    allowed = [
        (name, tip)
        for name, tip in all_branches
        if branch_allowed(name, settings.branch_include, settings.branch_exclude)
    ]
    # Local git gives us branch tip dates for free, so stale branches are skipped
    # before we spend a `git log` on them — the GitHub REST API can't do this.
    recent = [(name, tip) for name, tip in allowed if _tip_is_recent(tip, since_dt)]

    commits: list[dict[str, Any]] = []
    for name, _tip in recent:
        try:
            entries = await mirror.log(
                path, name, since, until, commits_per_branch, with_files=settings.folders_enabled
            )
        except CsrError as exc:
            errors.append({"repo": f"{repo.name}@{name}", "error": str(exc), "code": exc.code})
            continue
        for entry in entries:
            commits.append(
                make_commit(
                    provider="csr",
                    repo_key=repo.key,
                    repo_name=repo.name,
                    sha=entry["sha"],
                    message=entry["message"],
                    author_name=entry["author"],
                    date=entry["date"],
                    url=repo.commit_url(entry["sha"]),
                    branch=name,
                    paths=entry["paths"],
                )
            )

    return RepoResult(
        provider="csr",
        key=repo.key,
        name=repo.name,
        url=repo.web_url,
        default_branch=default,
        branches_total=len(all_branches),
        branches_shown=len(allowed),
        private=True,
        commits=commits,
        errors=errors,
        tags=repo_tags,
    )
