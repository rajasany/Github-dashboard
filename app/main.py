"""FastAPI app: serves the dashboard and proxies each provider so credentials stay server-side."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import (
    auth, bulk, compare, lookup, migration_sheet, migrations, naming, ordering, reference,
    report, spreadsheet, sso, summary, tagging,
)
from . import timeline as timeline_view
from .config import load_settings
from .csr import GitMirror
from .db import Database
from .feed import build_feed, enrich_commit_folders
from .github import GitHubClient, GitHubError
from .store import FileStore

STATIC_DIR = Path(__file__).parent / "static"

settings = load_settings()
gh_client: GitHubClient | None = None
mirror: GitMirror | None = None
store: FileStore | None = None
tag_store: tagging.TagStore | None = None
migration_store: migrations.MigrationStore | None = None
reference_store: reference.ReferenceStore | None = None
database: Database | None = None
# config.yaml only decides whether the tab exists; the lists come from the
# database once it is running, so `settings.migrations` is replaced per request.
migrations_enabled: bool = False
# Set when the configured database could not be reached at startup.
database_error: str = ""

# Single-flight state for the local `gcloud auth login` flow. This app is a
# localhost, single-user tool — the subprocess opens a browser and writes to
# *this machine's* gcloud credential store, so it only makes sense run locally.
_gcloud_login_task: asyncio.Task | None = None
_gcloud_login_error: str | None = None


async def _run_gcloud_login() -> None:
    global _gcloud_login_error
    _gcloud_login_error = None
    gcloud = shutil.which("gcloud")
    if not gcloud:
        _gcloud_login_error = "`gcloud` not found on PATH. Install the Google Cloud SDK."
        return
    proc = await asyncio.create_subprocess_exec(
        gcloud,
        "auth",
        "login",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        detail = (err or b"").decode().strip().splitlines()
        _gcloud_login_error = detail[-1] if detail else "gcloud auth login did not complete."


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gh_client, mirror, store, tag_store, migration_store, reference_store, database
    gh_client = GitHubClient(settings)
    mirror = GitMirror(settings)
    # The two git caches stay on SQLite: one is rebuildable derived data, the
    # other stages tags against a local mirror. Neither is shared application
    # state, so neither belongs in the configured database.
    store = FileStore(settings.store_path)
    tag_store = tagging.TagStore(settings.tag_store_path)

    global migrations_enabled
    migrations_enabled = settings.migrations.enabled
    if migrations_enabled:
        database = Database(settings.database_url)
        try:
            database.verify()
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            # The rest of the dashboard reads git and needs no database, so an
            # unreachable one disables the Migrations tab rather than the app,
            # and the tab says exactly why.
            global database_error
            database_error = str(exc)
            print(f"\n  *** Migrations tab unavailable ***\n  {database_error}\n", flush=True)
            database.close()
            database = None
            yield
            await gh_client.aclose()
            return
        print(f"  migration register: {database.describe}", flush=True)
        migration_store = migrations.MigrationStore(database)
        reference_store = reference.ReferenceStore(database)
        # First run against an empty database: take the lists from config.yaml
        # and the microservice file, once. After that the database is the source.
        seeded = reference_store.seed(settings.migrations)
        if seeded:
            print(f"  seeded reference lists from config: {seeded}", flush=True)
        seeded_roles = reference_store.seed_roles(settings.auth.roles)
        if seeded_roles:
            print(f"  seeded role assignments from config: {seeded_roles}", flush=True)
        settings.auth.live_roles = reference_store.roles()
    if settings.auth.dev_mode:
        scope = "ANY address" if settings.auth.dev_allow_remote else "loopback only"
        disabled = " — IGNORED, because auth.trusted_proxies is set" if settings.auth.trusted_proxies else ""
        print(
            f"\n  *** auth.dev_mode is ON ({scope}){disabled}. Identity is self-declared:\n"
            f"      anyone who can reach this app can choose any role. Testing only. ***\n",
            flush=True,
        )
    yield
    await gh_client.aclose()
    if database is not None:
        database.close()


app = FastAPI(title="Repo Change Dashboard", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Paths that must stay reachable to someone not yet signed in: the page itself
# and its assets (so a sign-in prompt can be shown), the sign-in routes, and the
# liveness probe. Everything else is data.
PUBLIC_PREFIXES = ("/static/", "/auth/")
PUBLIC_PATHS = {"/", "/favicon.ico", "/api/health", "/api/auth/whoami"}


@app.middleware("http")
async def require_identity(request, call_next):
    """Refuse unattributable requests, as one rule rather than 50 decorators.

    Guarding each endpoint by hand is how `/api/tags/push` came to be reachable
    without signing in: the guard is easy to leave off a new route, and nothing
    notices. Here a new endpoint is protected by default and has to be named
    above to be public, which is the safer way round.
    """
    path = request.url.path
    if (
        not settings.auth.enforced
        or path in PUBLIC_PATHS
        or path.startswith(PUBLIC_PREFIXES)
    ):
        return await call_next(request)

    if not current_user(request).signed_in:
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": auth.SIGN_IN_HELP}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """Make the browser revalidate the page and its scripts on every load.

    Without a Cache-Control header browsers fall back to heuristic caching, and
    can happily pair a freshly-edited index.html with a cached app.js from a
    previous version — which presents as "the new UI is there but nothing in it
    responds". ETags still make the revalidation a cheap 304.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _asset_version(name: str) -> str:
    """A short fingerprint of a static file, from its size and mtime."""
    try:
        stat = (STATIC_DIR / name).stat()
    except OSError:
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the shell with fingerprinted asset URLs.

    Cache-Control alone proved insufficient: a browser holding a heuristically
    fresh copy of /static/app.js can pair it with a new index.html and never
    revalidate, which presents as a UI whose controls are all inert. Changing the
    URL when the file changes removes the browser's discretion entirely.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={_asset_version('app.js')}")
    html = html.replace("/static/styles.css", f"/static/styles.css?v={_asset_version('styles.css')}")
    html = html.replace(
        "/static/migrations.js", f"/static/migrations.js?v={_asset_version('migrations.js')}"
    )
    html = html.replace(
        "/static/timeline.js", f"/static/timeline.js?v={_asset_version('timeline.js')}"
    )
    return HTMLResponse(html)


@app.get("/api/config")
async def get_config() -> dict:
    """What the UI needs to render before any provider call happens."""
    return {
        "configured": settings.configured,
        # Lets the page detect that it is itself a cached copy — see app.js.
        "app_version": _asset_version("app.js"),
        "has_token": bool(settings.token),
        "repos": settings.repos,
        "csr_repos": [{"project": r.project, "repo": r.repo, "key": r.key} for r in settings.csr_repos],
        "providers": (["github"] if settings.has_github else []) + (["csr"] if settings.has_csr else []),
        "defaults": {
            "days": settings.days,
            "commits_per_branch": settings.commits_per_branch,
        },
        "branch_include": settings.branch_include,
        "branch_exclude": settings.branch_exclude,
        "cache_ttl": settings.cache_ttl,
        "folders": {
            "enabled": settings.folders_enabled,
            "depth": settings.folder_depth,
            "patterns": settings.folder_paths,
            "exclude": settings.folder_exclude,
        },
    }


def _resolve_window(days: int | None, since: str | None, until: str | None) -> tuple[datetime, datetime | None]:
    """Turn the request's date arguments into a concrete [since, until] window.

    `since`/`until` are calendar dates (YYYY-MM-DD) interpreted in UTC. `until`
    is inclusive of the whole day named, which is what a person picking "to 5 Aug"
    means — the naive reading would silently drop that day's commits.
    """
    now = datetime.now(timezone.utc)

    until_dt: datetime | None = None
    if until:
        try:
            day = date.fromisoformat(until)
        except ValueError:
            raise HTTPException(status_code=422, detail="`until` must be a date, e.g. 2026-08-05.")
        until_dt = datetime.combine(day, time.max, tzinfo=timezone.utc)

    if since:
        try:
            day = date.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=422, detail="`since` must be a date, e.g. 2026-07-01.")
        since_dt = datetime.combine(day, time.min, tzinfo=timezone.utc)
    else:
        # No explicit start: fall back to a lookback counted from the window's end.
        span = days or settings.days
        since_dt = (until_dt or now) - timedelta(days=span)

    if until_dt and until_dt < since_dt:
        raise HTTPException(status_code=422, detail="`until` is before `since`.")

    return since_dt, until_dt


@app.get("/api/feed")
async def get_feed(
    days: int = Query(default=None, ge=1, le=3650, description="Lookback in days; ignored when `since` is given."),
    since: str = Query(default=None, description="Start date, YYYY-MM-DD (UTC). Overrides `days`."),
    until: str = Query(default=None, description="End date, YYYY-MM-DD (UTC), inclusive. Defaults to now."),
    commits_per_branch: int = Query(default=None, ge=1, le=100),
    key: list[str] | None = Query(
        default=None, description="Restrict to these repo keys, e.g. github:owner/repo or csr:project/repo"
    ),
    refresh: bool = False,
) -> dict:
    assert gh_client is not None and mirror is not None and store is not None

    if not settings.configured:
        raise HTTPException(
            status_code=400,
            detail="No repositories configured. Copy config.example.yaml to config.yaml.",
        )

    # A token is not strictly required for GitHub — public repos are readable
    # unauthenticated, capped at 60 requests/hour. The UI warns in that mode.
    wanted = set(key) if key else None
    known = set(settings.all_keys())
    if wanted and not (wanted & known):
        raise HTTPException(status_code=400, detail="None of the requested repo keys are in config.yaml.")

    github_repos = [r for r in settings.repos if wanted is None or f"github:{r}" in wanted]
    csr_repos = [r for r in settings.csr_repos if wanted is None or r.key in wanted]

    since_dt, until_dt = _resolve_window(days, since, until)

    if refresh:
        gh_client.cache.clear()

    try:
        return await build_feed(
            settings,
            gh_client,
            mirror,
            store,
            github_repos=github_repos,
            csr_repos=csr_repos,
            since_dt=since_dt,
            until_dt=until_dt,
            commits_per_branch=commits_per_branch or settings.commits_per_branch,
        )
    except GitHubError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message) from exc


@app.get("/api/summary")
async def get_summary(
    key: str = Query(description="Repo key, e.g. github:owner/repo"),
    branch: str = Query(description="Branch to tabulate"),
    folder: str = Query(default=None, description="Restrict to commits touching this folder"),
    days: int = Query(default=None, ge=1, le=3650),
    since: str = Query(default=None, description="Start date, YYYY-MM-DD (UTC)."),
    until: str = Query(default=None, description="End date, YYYY-MM-DD (UTC), inclusive."),
    limit: int = Query(default=100, ge=1, le=300, description="Max commits scanned on the branch."),
) -> dict:
    """One row per commit on a branch, with full tag metadata."""
    assert gh_client is not None and mirror is not None and store is not None

    if key not in set(settings.all_keys()):
        raise HTTPException(status_code=400, detail=f"Unknown repository: {key}")

    since_dt, until_dt = _resolve_window(days, since, until)
    try:
        return await summary.build_summary(
            settings,
            gh_client,
            mirror,
            store,
            repo_key=key,
            branch=branch,
            folder=folder or None,
            since_dt=since_dt,
            until_dt=until_dt,
            limit=limit,
        )
    except summary.SummaryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/lookup")
async def get_lookup(
    sha: str = Query(description="Commit hash (4–40 hex chars) or a commit URL"),
) -> dict:
    """Which repository and branches hold a commit, and where it sits in each."""
    assert gh_client is not None and mirror is not None

    if not settings.configured:
        raise HTTPException(status_code=400, detail="No repositories configured.")
    try:
        return await lookup.lookup_commit(settings, gh_client, mirror, sha)
    except lookup.LookupError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message) from exc


class StageTagRequest(BaseModel):
    repo_key: str
    sha: str = Field(min_length=4, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=2000)


@app.get("/api/tags/staged")
async def list_staged_tags(key: str = Query(default=None)) -> dict:
    assert tag_store is not None
    return {"staged": tag_store.list(key)}


@app.post("/api/tags/stage")
async def stage_tag(body: StageTagRequest) -> dict:
    """Record a tag locally. Nothing is sent to the remote by this call."""
    assert gh_client is not None and mirror is not None and tag_store is not None
    if body.repo_key not in set(settings.all_keys()):
        raise HTTPException(status_code=400, detail=f"Unknown repository: {body.repo_key}")
    try:
        staged = await tagging.stage_tag(
            settings, gh_client, mirror, tag_store,
            repo_key=body.repo_key, sha=body.sha, name=body.name, message=body.message,
        )
    except tagging.TagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"staged": staged}


@app.post("/api/tags/push/{tag_id}")
async def push_staged_tag(tag_id: int) -> dict:
    """Publish a staged tag to its remote. This is the outward-facing step."""
    assert gh_client is not None and mirror is not None and tag_store is not None
    try:
        pushed = await tagging.push_tag(settings, gh_client, mirror, tag_store, tag_id)
    except tagging.TagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"pushed": pushed}


@app.delete("/api/tags/staged/{tag_id}")
async def discard_staged_tag(tag_id: int) -> dict:
    assert tag_store is not None
    existing = tag_store.get(tag_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No such staged tag.")
    if existing["pushed"]:
        raise HTTPException(
            status_code=422,
            detail="That tag has already been pushed; discarding it here would not remove it "
                   "from the remote. Delete it on the host instead.",
        )
    tag_store.delete(tag_id)
    return {"discarded": tag_id}


@app.get("/api/tags/overview")
async def tags_overview(
    key: str = Query(default=None, description="Repo key; omit for a cross-repo listing."),
) -> dict:
    """Tags for one repository, with the folders and branches of each tagged commit.

    Without `key` this falls back to a cross-repo listing that omits branch
    information — establishing which branches contain a commit costs one API call
    per (tag, branch) pair on GitHub, which is only affordable one repo at a time.
    """
    assert gh_client is not None and mirror is not None and store is not None and tag_store is not None

    async def enrich(commits):
        return await enrich_commit_folders(settings, gh_client, store, commits)

    if key:
        if key not in set(settings.all_keys()):
            raise HTTPException(status_code=400, detail=f"Unknown repository: {key}")
        try:
            return await tagging.tags_for_repo(settings, gh_client, mirror, tag_store, key, enrich)
        except tagging.TagError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return await tagging.tag_overview(settings, gh_client, mirror, tag_store, enrich)


class OrderRequest(BaseModel):
    commits: str = Field(min_length=1, max_length=20000)
    repo_key: str | None = None
    branch: str | None = None


@app.post("/api/commits/order")
async def order_commits(body: OrderRequest) -> dict:
    """Validate a pasted commit list and order it newest-first along one branch."""
    assert gh_client is not None and mirror is not None

    if not settings.configured:
        raise HTTPException(status_code=400, detail="No repositories configured.")
    if body.repo_key and body.repo_key not in set(settings.all_keys()):
        raise HTTPException(status_code=400, detail=f"Unknown repository: {body.repo_key}")

    try:
        return await ordering.order_commits(
            settings, gh_client, mirror,
            raw=body.commits, repo_key=body.repo_key, branch=body.branch,
        )
    except ordering.OrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message) from exc


@app.get("/api/bulk/template")
async def bulk_template() -> Response:
    """A starter workbook, so the expected columns are shown rather than guessed."""
    return Response(
        content=spreadsheet.build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="release-plan-template.xlsx"'},
    )


@app.get("/api/bulk/placeholders")
async def bulk_placeholders() -> dict:
    return {"placeholders": [{"token": tok, "help": txt} for tok, txt in naming.PLACEHOLDERS]}


@app.post("/api/bulk/process")
async def bulk_process(
    file: UploadFile = File(..., description="A .xlsx or .csv of repo/folder rows"),
    convention: str = Form(...),
    days: int = Form(default=None),
    since: str = Form(default=None),
    branch: str = Form(default=None),
) -> dict:
    """Summarise each row of an uploaded sheet and propose a tag for it."""
    assert gh_client is not None and mirror is not None and store is not None

    if not settings.configured:
        raise HTTPException(status_code=400, detail="No repositories configured.")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="That file is larger than 5 MB.")

    try:
        sheet = spreadsheet.parse_sheet(file.filename or "", content)
    except spreadsheet.SheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    since_dt, _ = _resolve_window(days, since, None)
    try:
        result = await bulk.process_rows(
            settings, gh_client, mirror, store,
            rows=sheet["rows"], convention=convention,
            since_dt=since_dt, default_branch=(branch or None),
        )
    except bulk.BulkError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result["sheet"] = {
        "filename": file.filename,
        "columns": sheet["columns"],
        "header_row": sheet["header_row"],
        "ignored": sheet["ignored"],
    }
    return result


@app.get("/api/branches")
async def get_branches(key: str = Query(description="Repo key, e.g. github:owner/repo")) -> dict:
    """All branches of one repository, for the comparison pickers."""
    assert gh_client is not None and mirror is not None
    if key not in set(settings.all_keys()):
        raise HTTPException(status_code=400, detail=f"Unknown repository: {key}")
    try:
        return await compare.list_branches(settings, gh_client, mirror, key)
    except compare.CompareError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/compare")
async def get_compare(
    key: str = Query(description="Repo key, e.g. github:owner/repo"),
    base: str = Query(description="Branch to compare against"),
    head: str = Query(description="Branch whose extra work you want to see"),
) -> dict:
    """What `head` has that `base` does not, measured from their merge base."""
    assert gh_client is not None and mirror is not None and store is not None

    if key not in set(settings.all_keys()):
        raise HTTPException(status_code=400, detail=f"Unknown repository: {key}")
    if base == head:
        raise HTTPException(status_code=400, detail="Pick two different branches to compare.")

    try:
        if key.startswith("github:"):
            result = await compare.compare_github(
                gh_client, key.split(":", 1)[1], base, head, settings
            )
            # GitHub's compare payload carries no per-commit file list, so folder
            # attribution needs the same permanently-cached lookup the feed uses.
            if settings.folders_enabled and result["commits"]:
                await enrich_commit_folders(settings, gh_client, store, result["commits"])
        else:
            repo = next(r for r in settings.csr_repos if r.key == key)
            result = await compare.compare_csr(mirror, repo, base, head, settings)
    except compare.CompareError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    default_branch = None
    try:
        default_branch = (await compare.list_branches(settings, gh_client, mirror, key)).get(
            "default_branch"
        )
    except compare.CompareError:
        pass

    # These commits are on `head` and absent from `base`, so they are "on the
    # default branch" precisely when `head` is it. Without this the field is
    # missing and every downstream consumer reads it as False — which would make
    # a report of the default branch claim none of its commits had landed.
    on_default = bool(default_branch) and head == default_branch
    for commit in result["commits"]:
        commit["on_default"] = on_default

    result["default_branch"] = default_branch
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


class ReportRequest(BaseModel):
    """What the browser sends to have its current view turned into a document.

    The commits travel with the request rather than being re-queried, so the
    report is exactly the rows on screen — there is no second copy of the filter
    logic on the server that could drift out of step with the UI.
    """

    format: Literal["pdf", "pptx"]
    criteria: dict[str, Any] = Field(default_factory=dict)
    commits: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("commits")
    @classmethod
    def _bounded(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > 20000:
            raise ValueError("Too many commits for one report; narrow the date range.")
        return value


@app.post("/api/report")
async def create_report(request: ReportRequest) -> Response:
    if not request.commits:
        raise HTTPException(
            status_code=400,
            detail="Nothing to report — the current filter matches no commits.",
        )

    roll = report.aggregate(request.commits, request.criteria)

    if request.format == "pdf":
        payload = await asyncio.to_thread(report.build_pdf, roll)
        media = "application/pdf"
        extension = "pdf"
    else:
        payload = await asyncio.to_thread(report.build_pptx, roll)
        media = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        extension = "pptx"

    name = report.report_filename(request.criteria, extension)
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(len(payload)),
        },
    )


@app.get("/auth/gcloud", include_in_schema=False)
async def gcloud_auth_page() -> FileResponse:
    """A real page navigation (not a fetch) so the browser's address bar and
    history reflect leaving the dashboard to sign in, then coming back."""
    return FileResponse(STATIC_DIR / "gcloud-auth.html")


@app.post("/api/gcloud/login")
async def start_gcloud_login(request: Request) -> dict:
    global _gcloud_login_task
    peer = request.client.host if request.client else None
    if not auth._is_loopback(peer):
        # `gcloud auth login` authenticates the *machine*, not the caller. On a
        # shared host that would change the identity every other user's data is
        # read with, so it is a laptop convenience only.
        raise HTTPException(
            status_code=403,
            detail="gcloud sign-in runs on the server itself, so it is only offered "
                   "from the machine the server runs on. On a deployed instance, give "
                   "the service its own credentials instead — see the README.",
        )
    if not settings.has_csr:
        raise HTTPException(status_code=400, detail="No Google Cloud Source Repositories configured.")
    if _gcloud_login_task is None or _gcloud_login_task.done():
        _gcloud_login_task = asyncio.create_task(_run_gcloud_login())
    return {"started": True}


@app.get("/api/gcloud/status")
async def gcloud_status() -> dict:
    assert mirror is not None
    running = _gcloud_login_task is not None and not _gcloud_login_task.done()
    authenticated = await mirror.tokens.is_authenticated()
    return {"running": running, "authenticated": authenticated, "error": None if running else _gcloud_login_error}


def _resolve_instant_window(
    days: int | None,
    since: str, until: str,
    since_epoch: float | None, until_epoch: float | None,
) -> tuple[datetime, datetime | None]:
    """A window to the minute, not to the day.

    The browser sends instants it computed from the viewer's own clock, so a
    window ending "today at 17:00" means that moment wherever they are. ISO
    strings are accepted for anything driving this by hand, and a naive one is
    read as UTC. Falling back to `days` gives the configured default window.
    """

    def instant(epoch: float | None, text: str, what: str) -> datetime | None:
        if epoch is not None:
            return datetime.fromtimestamp(float(epoch), timezone.utc)
        text = (text or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"`{what}` must be a date and time, e.g. 2026-09-01T09:30.",
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    until_dt = instant(until_epoch, until, "until")
    since_dt = instant(since_epoch, since, "since")
    if since_dt is None:
        span = days if days and days > 0 else settings.days
        since_dt = datetime.now(timezone.utc) - timedelta(days=span)
    if until_dt and until_dt <= since_dt:
        raise HTTPException(status_code=422, detail="The window must end after it starts.")
    return since_dt, until_dt


@app.get("/api/timeline")
async def timeline(
    key: str = Query(..., description="github:owner/repo or csr:project/repo"),
    branch: str = Query(default="", description="one branch, or blank for all"),
    folder: str = Query(default=""),
    days: int | None = Query(default=None),
    since: str = Query(default=""),
    until: str = Query(default=""),
    since_epoch: float | None = Query(default=None),
    until_epoch: float | None = Query(default=None),
    limit: int = Query(default=0, ge=0, le=300),
) -> dict:
    """Commits in one repository's folder over a window, ordered for a timeline."""
    assert gh_client is not None and mirror is not None and store is not None
    if key not in settings.all_keys():
        raise HTTPException(status_code=404, detail=f"Not a tracked repository: {key}")

    since_dt, until_dt = _resolve_instant_window(days, since, until, since_epoch, until_epoch)
    try:
        return await timeline_view.build_timeline(
            settings, gh_client, mirror, store,
            repo_key=key, branch=branch, folder=folder,
            since_dt=since_dt, until_dt=until_dt,
            limit=limit or settings.commits_per_branch,
        )
    except timeline_view.TimelineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "configured": settings.configured,
        "github_repos": len(settings.repos),
        "csr_repos": len(settings.csr_repos),
    }


# --------------------------------------------------------------------------- #
# Migration requests
# --------------------------------------------------------------------------- #


def current_user(request: Request) -> auth.User:
    """Identify the caller from the proxy header, or dev mode (see app/auth.py)."""
    # Role assignments live in the database; refresh the view of them before
    # deciding what this caller may do, so a change takes effect immediately.
    if reference_store is not None:
        settings.auth.live_roles = reference_store.roles()
    peer = request.client.host if request.client else None
    return auth.resolve_user(request.headers, peer, settings, request.cookies)


def _require_migrations() -> migrations.MigrationStore:
    if not migrations_enabled:
        raise HTTPException(
            status_code=404,
            detail="Migration requests are not enabled. Add a `migrations:` section to config.yaml.",
        )
    if database_error:
        raise HTTPException(status_code=503, detail=database_error)
    assert migration_store is not None and reference_store is not None
    # The domain functions validate against `settings.migrations`; point it at
    # what the database currently holds, so an edit in the admin screen takes
    # effect on the next request rather than the next restart.
    live = reference_store.snapshot()
    live.enabled = True
    settings.migrations = live
    return migration_store


def _refuse_if_frozen(store_: migrations.MigrationStore) -> None:
    if store_.active_freeze():
        raise HTTPException(
            status_code=423,
            detail="Record entry is frozen. An approver can lift the freeze.",
        )


def _editable(user: auth.User, row: dict[str, Any]) -> list[str]:
    """Which fields this person may change on this record, right now.

    A freeze makes that nothing at all. Computing it here rather than in the
    client is what lets the form render a frozen record read-only without
    knowing the rule.
    """
    assert migration_store is not None
    if migration_store.active_freeze():
        return []
    return migrations.editable_fields(user, row)


def _lists() -> migrations.MigrationConfig:
    """The reference lists as they stand in the database right now."""
    assert reference_store is not None
    return reference_store.snapshot()


@app.exception_handler(auth.AuthError)
async def _auth_error(request: Request, exc: auth.AuthError) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(migrations.MigrationError)
async def _migration_error(request: Request, exc: migrations.MigrationError) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {"detail": str(exc), "fields": exc.fields}, status_code=exc.status
    )


@app.get("/api/migrations/meta")
async def migrations_meta(request: Request) -> dict:
    """Everything the tab needs to render: who you are, the lists, the field rules."""
    store_ = _require_migrations()
    user = current_user(request)
    cfg = _lists()
    freeze = store_.active_freeze()

    peer = request.client.host if request.client else None
    dev_on, dev_why = auth.dev_mode_available(peer, settings)
    # Only exact addresses can be offered; a glob like *@example.com names nobody.
    identities: set[str] = set()
    for patterns in settings.auth.roles.values():
        identities.update(p for p in patterns if "*" not in p and "?" not in p)
    if settings.auth.dev_user:
        identities.add(settings.auth.dev_user)

    return {
        "user": user.as_dict(),
        "auth_configured": settings.auth.configured,
        "dev": {
            "enabled": dev_on,
            "configured": settings.auth.dev_mode,
            "why_not": "" if dev_on else dev_why,
            "cookie": auth.DEV_COOKIE,
            "identities": [
                {"email": who, "roles": sorted(auth.roles_for(who, settings))}
                for who in sorted(identities)
            ],
        },
        "fields": migrations.field_spec(),
        "options": migrations.option_lists(cfg),
        "services": [
            {"name": m.name, "repos": m.repos, "track_leads": m.track_leads,
             "allow_full_merge": m.allow_full_merge}
            for m in cfg.microservices
        ],
        "services_source": {
            "file": str(cfg.microservices_file) if cfg.microservices_file else "",
            "error": cfg.services_error,
            "count": len(cfg.microservices),
        },
        "statuses": [{"key": k, "label": v} for k, v in migrations.STATUS_LABELS.items()],
        # The employee this caller is, if their address is on the list — the
        # form uses it to fill Change Requestor in without being asked.
        "me_employee": (
            found.label if (found := cfg.employee_for(user.email)) else ""
        ),
        "employees": [
            {"number": e.number, "name": e.name, "label": e.label} for e in cfg.employees
        ],
        "stages": [{"key": k, "label": v} for k, v in migrations.STAGE_TITLES.items()],
        "request_fields": list(migrations.REQUEST_KEYS),
        "freeze": freeze,
        "frozen": freeze is not None,
        "permissions": migrations.permissions(user),
        "sso": {
            "enabled": settings.auth.sso.ready,
            "provider": settings.auth.sso.provider if settings.auth.sso.ready else "",
        },
    }


def _created_range(
    created_from: str, created_to: str, from_epoch: float | None, to_epoch: float | None
) -> tuple[float | None, float | None]:
    """Resolve the created-date range to instants.

    The browser sends epochs computed from the viewer's own midnight, so the day
    a record falls on matches the day shown next to it. Plain dates are accepted
    too, for callers outside the UI, and are read as UTC.
    """

    def day(value: str, end: bool) -> float | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"“{value}” is not a date (use YYYY-MM-DD)."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if end and parsed.timetz() == time(0, 0, tzinfo=parsed.tzinfo):
            # A bare end date means the whole of that day, not its first instant.
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
        return parsed.timestamp()

    return (
        from_epoch if from_epoch is not None else day(created_from, False),
        to_epoch if to_epoch is not None else day(created_to, True),
    )


@app.get("/api/migrations/records")
async def migrations_list(
    request: Request,
    release: str = Query(default=""),
    microservice: str = Query(default=""),
    migration_path: str = Query(default=""),
    status: str = Query(default=""),
    mine: bool = Query(default=False),
    created_from: str = Query(default=""),
    created_to: str = Query(default=""),
    created_from_epoch: float | None = Query(default=None),
    created_to_epoch: float | None = Query(default=None),
    sort: str = Query(default="sl_no"),
    dir: str = Query(default="desc"),
    include_inactive: bool = Query(default=False),
    archived: bool = Query(default=False),
) -> dict:
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)

    since, until = _created_range(created_from, created_to, created_from_epoch, created_to_epoch)
    rows = store_.list(
        release=release,
        microservice=microservice,
        migration_path=migration_path,
        status=status,
        created_by=user.email if mine else "",
        created_from=since,
        created_to=until,
        sort=sort,
        direction=dir,
        include_inactive=include_inactive,
        archived=archived,
    )
    # The client renders from this, but never decides it — every write is
    # re-checked server-side in apply_changes.
    for row in rows:
        row["editable"] = _editable(user, row)
    return {
        "rows": rows,
        "count": len(rows),
        "user": user.as_dict(),
        "sort": {"field": sort if sort in migrations.SORTABLE else "sl_no", "dir": dir},
        "sortable": list(migrations.SORTABLE),
        "permissions": migrations.permissions(user),
        # Kept for anything still reading the old key.
        "can_retire": user.has_any("approver", "admin"),
    }


@app.post("/api/migrations/records", status_code=201)
async def migrations_create(request: Request, payload: dict[str, Any]) -> dict:
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)
    row = migrations.create_record(settings, store_, user, payload or {})
    row["editable"] = _editable(user, row)
    return row


@app.patch("/api/migrations/records/{sl_no}")
async def migrations_update(request: Request, sl_no: int, payload: dict[str, Any]) -> dict:
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)
    row = migrations.apply_changes(settings, store_, user, sl_no, payload or {})
    row["editable"] = _editable(user, row)
    return row


def _bulk_ids(payload: dict[str, Any]) -> list[int]:
    raw = payload.get("sl_nos") or payload.get("sl_no") or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    try:
        return [int(value) for value in raw]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="sl_nos must be record numbers.") from exc


@app.post("/api/migrations/records/approve")
async def migrations_approve(request: Request, payload: dict[str, Any]) -> dict:
    """Approve several records for QA or production at once. Approvers and admins.

    `{"sl_nos": [1, 2, 3], "stage": "qa" | "prod"}`. Records that cannot be
    approved come back under `skipped` with a reason; the rest still go through.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "approver", "admin")

    return migrations.approve_many(
        settings, store_, user, _bulk_ids(payload),
        str(payload.get("stage") or "qa"),
        str(payload.get("planned") or ""),
    )


@app.post("/api/migrations/records/execute")
async def migrations_execute(request: Request, payload: dict[str, Any]) -> dict:
    """Record several records as migrated, to QA or production. DevOps and admins.

    `{"sl_nos": [1, 2, 3], "stage": "qa" | "prod", "remarks": "..."}`. Remarks are
    optional and applied to every record that moves; blank leaves each one's own.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "devops", "admin")

    return migrations.execute_many(
        settings, store_, user, _bulk_ids(payload),
        str(payload.get("stage") or "qa"),
        str(payload.get("remarks") or ""),
    )


@app.post("/api/migrations/records/archive")
async def migrations_archive(request: Request, payload: dict[str, Any]) -> dict:
    """File away records that reached production. Approvers and admins.

    `{"sl_nos": [1, 2, 3]}`. Archiving cannot be undone: an archived record
    leaves the register, can no longer be changed or deleted, and is read only
    through the archive view.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "approver", "admin")
    _refuse_if_frozen(store_)

    result = store_.archive(_bulk_ids(payload), user)
    if not result["archived"] and not result["skipped"]:
        raise HTTPException(status_code=422, detail="No records were selected.")
    return result


@app.post("/api/migrations/records/{sl_no}/active")
async def migrations_set_active(request: Request, sl_no: int, payload: dict[str, Any]) -> dict:
    """Retire a record, or bring it back. Approvers and admins.

    Reversible, and the change is recorded — unlike DELETE below, which is not.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "approver", "admin")
    # Retiring a record changes it, so a freeze stops it as it stops every other
    # change. Deleting was already refused; this was the gap.
    _refuse_if_frozen(store_)

    active = bool(payload.get("active", True))
    row = store_.set_active(sl_no, active, user)
    row["editable"] = _editable(user, row)
    return row


@app.get("/api/migrations/deleted")
async def migrations_deleted(request: Request) -> dict:
    """What has been removed from the register, and by whom. Approvers and admins."""
    store_ = _require_migrations()
    auth.require_role(current_user(request), "approver", "admin")
    return {"deleted": store_.deleted()}


@app.delete("/api/migrations/records/{sl_no}")
async def migrations_delete(request: Request, sl_no: int, reason: str = Query(default="")) -> dict:
    """Remove a record and its history permanently. Approvers and admins.

    A freeze blocks this too: it stops record entry, and erasing one is the most
    final kind of entry there is.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "approver", "admin")

    _refuse_if_frozen(store_)
    if store_.get(sl_no) is None:
        raise HTTPException(status_code=404, detail=f"No record with SL# {sl_no}.")
    store_.delete(sl_no, user, reason)
    return {"deleted": sl_no, "reason": reason}


@app.get("/api/migrations/records/{sl_no}/audit")
async def migrations_audit(request: Request, sl_no: int) -> dict:
    store_ = _require_migrations()
    auth.require_signed_in(current_user(request))
    if store_.get(sl_no) is None:
        raise HTTPException(status_code=404, detail=f"No record with SL# {sl_no}.")
    return {"sl_no": sl_no, "entries": store_.audit(sl_no)}


@app.get("/api/migrations/template")
async def migrations_template(request: Request) -> Response:
    _require_migrations()
    auth.require_signed_in(current_user(request))
    return Response(
        content=migration_sheet.build_template(_lists()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="migration-requests-template.xlsx"'},
    )


@app.post("/api/migrations/upload")
async def migrations_upload(
    request: Request,
    file: UploadFile = File(..., description="A .xlsx or .csv of migration requests"),
    commit: bool = Form(default=False),
) -> dict:
    """Validate an uploaded sheet, and import it only when `commit` is set.

    The default is a dry run so the UI can show exactly what would be created
    before anything is.
    """
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)
    if not user.is_developer and not user.is_approver:
        raise HTTPException(status_code=403, detail="Raising requests needs the developer role.")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="That file is larger than 5 MB.")

    try:
        sheet = migration_sheet.parse(file.filename or "", content)
    except spreadsheet.SheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    results = migration_sheet.check_rows(sheet["rows"], _lists())
    valid = [r for r in results if r["ok"]]

    created: list[dict[str, Any]] = []
    if commit:
        if not valid:
            raise HTTPException(status_code=422, detail="No row in that sheet is valid.")
        # Checked once here rather than per row, so a freeze starting mid-import
        # cannot let half a sheet through.
        if store_.active_freeze():
            raise HTTPException(
                status_code=423,
                detail="Record entry is frozen. An approver can lift the freeze.",
            )
        for result in valid:
            created.append(store_.insert(result["values"], user))

    return {
        "committed": commit,
        "sheet": {
            "filename": file.filename,
            "columns": sheet["columns"],
            "header_row": sheet["header_row"],
            "ignored": sheet["ignored"],
        },
        "total": len(results),
        "valid": len(valid),
        "invalid": len(results) - len(valid),
        "results": results,
        "created": created,
    }


@app.get("/api/migrations/freezes")
async def migrations_freezes(request: Request, include_past: bool = Query(default=False)) -> dict:
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)
    return {
        "freezes": store_.freezes(include_past=include_past),
        "active": store_.active_freeze(),
        "can_manage": user.is_approver,
    }


@app.post("/api/migrations/freezes", status_code=201)
async def migrations_add_freeze(request: Request, payload: dict[str, Any]) -> dict:
    store_ = _require_migrations()
    user = current_user(request)
    auth.require_role(user, "approver")

    def instant(epoch_key: str, iso_key: str) -> float:
        if payload.get(epoch_key) not in (None, ""):
            return float(payload[epoch_key])
        raw = str(payload.get(iso_key) or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail=f"{iso_key} is required.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{iso_key} is not a date and time.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    return store_.add_freeze(
        instant("starts_epoch", "starts_at"),
        instant("ends_epoch", "ends_at"),
        str(payload.get("reason") or ""),
        user,
    )


@app.delete("/api/migrations/freezes/{freeze_id}")
async def migrations_delete_freeze(request: Request, freeze_id: int) -> dict:
    store_ = _require_migrations()
    auth.require_role(current_user(request), "approver")
    if not store_.delete_freeze(freeze_id):
        raise HTTPException(status_code=404, detail="No such freeze.")
    return {"deleted": freeze_id}


@app.get("/api/migrations/export.csv")
async def migrations_export(
    request: Request,
    release: str = Query(default=""),
    microservice: str = Query(default=""),
    migration_path: str = Query(default=""),
    status: str = Query(default=""),
    created_from: str = Query(default=""),
    created_to: str = Query(default=""),
    created_from_epoch: float | None = Query(default=None),
    created_to_epoch: float | None = Query(default=None),
    sort: str = Query(default="sl_no"),
    dir: str = Query(default="desc"),
    include_inactive: bool = Query(default=False),
    archived: bool = Query(default=False),
) -> Response:
    """The full register as CSV — every field, including the role-gated ones."""
    import csv as _csv
    import io as _io

    store_ = _require_migrations()
    auth.require_signed_in(current_user(request))
    since, until = _created_range(created_from, created_to, created_from_epoch, created_to_epoch)
    rows = store_.list(
        release=release, microservice=microservice,
        migration_path=migration_path, status=status,
        created_from=since, created_to=until, sort=sort, direction=dir,
        include_inactive=include_inactive, archived=archived,
    )

    buffer = _io.StringIO()
    writer = _csv.writer(buffer)
    writer.writerow(
        [f.label for f in migrations.FIELDS] + ["Status", "Active", "Archived", "Archived By"]
    )
    for row in rows:
        writer.writerow(
            [row.get(f.key, "") for f in migrations.FIELDS]
            + [row["status_label"], "Yes" if row["active"] else "No",
               row["archived_at"], row["archived_by"]]
        )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="migration-requests.csv"'},
    )


# --------------------------------------------------------------------------- #
# Reference lists (the migration form's dropdowns)
# --------------------------------------------------------------------------- #


@app.exception_handler(reference.ReferenceError)
async def _reference_error(request: Request, exc: reference.ReferenceError) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


def _require_reference(request: Request) -> reference.ReferenceStore:
    """Editing the lists is an administrative act — approvers and admins."""
    _require_migrations()
    auth.require_role(current_user(request), "approver", "admin")
    assert reference_store is not None
    return reference_store


@app.get("/api/migrations/lists")
async def migrations_lists(request: Request) -> dict:
    _require_migrations()
    user = current_user(request)
    auth.require_signed_in(user)
    assert reference_store is not None and database is not None
    return {
        **reference_store.as_payload(),
        "can_edit": user.has_any("approver", "admin"),
        "storage": database.describe,
    }


@app.put("/api/migrations/lists/{name}")
async def migrations_set_list(request: Request, name: str, payload: dict[str, Any]) -> dict:
    """Replace one simple list — releases, migration paths or change requestors."""
    store_ = _require_reference(request)
    values = payload.get("values")
    if not isinstance(values, list):
        raise HTTPException(status_code=422, detail="Send {\"values\": [...]}.")
    store_.set_list(name, [str(v) for v in values])
    return store_.as_payload()


@app.put("/api/migrations/lists/microservices/{name}")
async def migrations_save_service(request: Request, name: str, payload: dict[str, Any]) -> dict:
    """Create or update one microservice, its repos and its track leads."""
    store_ = _require_reference(request)
    store_.save_microservice(
        str(payload.get("name") or name),
        [str(v) for v in (payload.get("repos") or [])],
        [str(v) for v in (payload.get("track_leads") or [])],
        rename_from="" if name == "new" else name,
        allow_full_merge=bool(payload.get("allow_full_merge")),
    )
    return store_.as_payload()


@app.put("/api/migrations/lists/roles/{role}")
async def migrations_set_role(request: Request, role: str, payload: dict[str, Any]) -> dict:
    """Replace the addresses holding one role. Approvers and admins.

    Refuses to leave nobody holding the role the caller is using, so an
    administrator cannot lock themselves — and everyone else — out.
    """
    store_ = _require_reference(request)
    user = current_user(request)
    patterns = payload.get("patterns")
    if not isinstance(patterns, list):
        raise HTTPException(status_code=422, detail="Send {\"patterns\": [...]}.")

    before = list(store_.roles().get(role, []))
    updated = store_.set_role(role, [str(v) for v in patterns])
    settings.auth.live_roles = updated

    if not auth.roles_for(user.email, settings) & {"approver", "admin"}:
        # The caller has just removed their own way back in, and nobody else can
        # undo it for them. Restore exactly what was there and refuse.
        settings.auth.live_roles = store_.set_role(role, before)
        raise HTTPException(
            status_code=409,
            detail="That would leave you without the approver or admin role, and "
                   "nobody able to put it back. Add another holder first.",
        )
    return {"roles": updated}


@app.put("/api/migrations/lists/employees/{number}")
async def migrations_save_employee(request: Request, number: str, payload: dict[str, Any]) -> dict:
    """Create or update one employee (`new` to add)."""
    store_ = _require_reference(request)
    store_.save_employee(
        str(payload.get("number") or number),
        str(payload.get("name") or ""),
        str(payload.get("email") or ""),
        rename_from="" if number == "new" else number,
    )
    return store_.as_payload()


@app.delete("/api/migrations/lists/employees/{number}")
async def migrations_delete_employee(request: Request, number: str) -> dict:
    store_ = _require_reference(request)
    store_.delete_employee(number)
    return store_.as_payload()


@app.delete("/api/migrations/lists/microservices/{name}")
async def migrations_delete_service(request: Request, name: str) -> dict:
    store_ = _require_reference(request)
    used = store_.in_use(name)
    if used:
        raise HTTPException(
            status_code=409,
            detail=f"{used} record{'s' if used != 1 else ''} name “{name}”. "
                   "Existing records keep the value; remove it only if nothing should use it again.",
        )
    store_.delete_microservice(name)
    return store_.as_payload()


# --------------------------------------------------------------------------- #
# Sign-in (OpenID Connect)
# --------------------------------------------------------------------------- #

_sso_client: sso.SsoClient | None = None


def _sso() -> sso.SsoClient:
    global _sso_client
    if not settings.auth.sso.ready:
        raise HTTPException(
            status_code=404,
            detail="Single sign-on is not configured. See the `auth.sso` section of config.yaml.",
        )
    if _sso_client is None:
        _sso_client = sso.SsoClient(settings.auth.sso)
    return _sso_client


@app.exception_handler(sso.SsoError)
async def _sso_error(request: Request, exc: sso.SsoError) -> Response:
    return HTMLResponse(
        "<h1>Sign-in failed</h1>"
        f"<p>{escape(str(exc))}</p>"
        '<p><a href="/auth/login">Try again</a></p>',
        status_code=exc.status,
    )


def _safe_next(value: str) -> str:
    """Only same-site paths, so the login link cannot bounce someone off-site."""
    value = (value or "/").strip()
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _cookie_kwargs() -> dict[str, Any]:
    # Secure whenever the redirect URL is https, which is the only way this
    # should be deployed; http is for a laptop, where Secure would break it.
    secure = settings.auth.sso.redirect_url.lower().startswith("https://")
    return {"httponly": True, "samesite": "lax", "secure": secure, "path": "/"}


@app.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request, next: str = Query(default="/")) -> Response:
    """Start the sign-in, remembering the flow secrets in a short-lived cookie."""
    client = _sso()
    url, flow = client.begin(_safe_next(next))
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        sso.FLOW_COOKIE,
        auth.session_signer(settings).dumps(flow),
        max_age=sso.FLOW_MAX_AGE,
        **_cookie_kwargs(),
    )
    return response


@app.get("/auth/callback", include_in_schema=False)
async def auth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
) -> Response:
    """Finish the sign-in: check the flow, verify the token, set the session."""
    client = _sso()
    if error:
        raise sso.SsoError(f"{error}: {error_description or 'the provider declined the sign-in'}")

    flow = auth.read_session(request.cookies.get(sso.FLOW_COOKIE, ""), settings)
    if not flow:
        raise sso.SsoError(
            "That sign-in has expired or was started in another browser. Start again.", 400
        )
    # The state check is what makes this callback belong to a sign-in this
    # browser began, rather than one somebody else started on its behalf.
    if not state or state != flow.get("state"):
        raise sso.SsoError("The sign-in could not be matched to a request from this browser.", 400)
    if not code:
        raise sso.SsoError("The provider returned no authorization code.")

    claims = client.finish(code, flow)
    email = sso.email_from(claims)
    if not email:
        raise sso.SsoError("The provider did not return an email address to identify you by.", 403)
    if not sso.domain_allowed(email, settings.auth.sso):
        allowed = ", ".join(settings.auth.sso.allowed_domains)
        raise sso.SsoError(f"{email} is not in an allowed domain ({allowed}).", 403)

    session = {"email": email, "name": str(claims.get("name") or "")}
    granted = sso.roles_from_claims(claims, settings.auth.sso)
    if granted:
        session["roles"] = sorted(granted)

    response = RedirectResponse(_safe_next(flow.get("next", "/")), status_code=302)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.session_signer(settings).dumps(session),
        max_age=max(1, settings.auth.sso.session_hours) * 3600,
        **_cookie_kwargs(),
    )
    response.delete_cookie(sso.FLOW_COOKIE, path="/")
    return response


@app.get("/auth/logout", include_in_schema=False)
async def auth_logout(request: Request) -> Response:
    """Drop the session here. The provider's own session is left alone."""
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(sso.FLOW_COOKIE, path="/")
    return response


@app.get("/api/auth/whoami")
async def auth_whoami(request: Request) -> dict:
    """Who the app thinks you are, and how it decided."""
    user = current_user(request)
    configured = settings.auth.sso.ready
    peer = request.client.host if request.client else None
    dev_on, dev_why = auth.dev_mode_available(peer, settings)
    return {
        **user.as_dict(),
        "enforced": settings.auth.enforced,
        # This endpoint stays public precisely so someone who cannot get in can
        # find out why. Diagnosing that from behind the gate would be useless.
        "dev_mode": {
            "available": dev_on,
            "configured": settings.auth.dev_mode,
            "why_not": "" if dev_on else dev_why,
        },
        "sso": {
            "enabled": configured,
            "provider": settings.auth.sso.provider if configured else "",
            "login_url": "/auth/login" if configured else "",
            "logout_url": "/auth/logout" if configured else "",
        },
    }
