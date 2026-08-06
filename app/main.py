"""FastAPI app: serves the dashboard and proxies each provider so credentials stay server-side."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import report
from .config import load_settings
from .csr import GitMirror
from .feed import build_feed
from .github import GitHubClient, GitHubError
from .store import FileStore

STATIC_DIR = Path(__file__).parent / "static"

settings = load_settings()
gh_client: GitHubClient | None = None
mirror: GitMirror | None = None
store: FileStore | None = None

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
    global gh_client, mirror, store
    gh_client = GitHubClient(settings)
    mirror = GitMirror(settings)
    store = FileStore(settings.store_path)
    yield
    await gh_client.aclose()


app = FastAPI(title="Repo Change Dashboard", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def get_config() -> dict:
    """What the UI needs to render before any provider call happens."""
    return {
        "configured": settings.configured,
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
async def start_gcloud_login() -> dict:
    global _gcloud_login_task
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


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "configured": settings.configured,
        "github_repos": len(settings.repos),
        "csr_repos": len(settings.csr_repos),
    }
