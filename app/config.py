"""Configuration loading: secrets from .env, tracked repos from config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class CsrRepo:
    """A Google Cloud Source Repositories repo."""

    project: str
    repo: str

    @property
    def key(self) -> str:
        return f"csr:{self.project}/{self.repo}"

    @property
    def name(self) -> str:
        return f"{self.project}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://source.developers.google.com/p/{self.project}/r/{self.repo}"

    @property
    def web_url(self) -> str:
        return f"https://source.cloud.google.com/{self.project}/{self.repo}"

    def commit_url(self, sha: str) -> str:
        return f"{self.web_url}/+/{sha}"


@dataclass
class Settings:
    token: str
    api_base: str
    cache_ttl: int
    max_concurrency: int
    repos: list[str] = field(default_factory=list)
    csr_repos: list[CsrRepo] = field(default_factory=list)
    branch_include: list[str] = field(default_factory=list)
    branch_exclude: list[str] = field(default_factory=list)
    days: int = 14
    commits_per_branch: int = 30
    # Folder / microservice tracking
    folders_enabled: bool = True
    folder_depth: int = 1
    folder_paths: list[str] = field(default_factory=list)
    folder_exclude: list[str] = field(default_factory=list)
    # Google Cloud
    gcloud_account: str = ""
    gcloud_token: str = ""
    mirror_dir: Path = ROOT / ".cache" / "mirrors"
    store_path: Path = ROOT / ".cache" / "commit-files.sqlite3"
    tag_store_path: Path = ROOT / ".cache" / "staged-tags.sqlite3"
    tagger_name: str = ""
    tagger_email: str = ""
    git_timeout: int = 240

    @property
    def has_github(self) -> bool:
        return bool(self.repos)

    @property
    def has_csr(self) -> bool:
        return bool(self.csr_repos)

    @property
    def configured(self) -> bool:
        return self.has_github or self.has_csr

    def all_keys(self) -> list[str]:
        return [f"github:{r}" for r in self.repos] + [r.key for r in self.csr_repos]


def _load_yaml() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _parse_github_repos(raw: list) -> list[str]:
    repos: list[str] = []
    for entry in raw or []:
        entry = str(entry).strip().strip("/")
        # Tolerate a full URL being pasted in instead of owner/repo.
        if entry.startswith("http"):
            entry = "/".join(entry.split("/")[-2:])
        if entry.endswith(".git"):
            entry = entry[: -len(".git")]
        if entry.count("/") == 1:
            repos.append(entry)
    return repos


def _parse_csr_repos(section: dict | None) -> list[CsrRepo]:
    """Accepts either bare repo names (using `project:`) or explicit dicts.

    gcloud:
      project: my-project
      repos:
        - my-repo                       # inherits project above
        - project: other-proj           # explicit
          repo: nested/repo
    """
    if not section:
        return []

    default_project = str(section.get("project") or "").strip()
    out: list[CsrRepo] = []

    for entry in section.get("repos") or []:
        if isinstance(entry, dict):
            project = str(entry.get("project") or default_project).strip()
            repo = str(entry.get("repo") or "").strip().strip("/")
        else:
            text = str(entry).strip().strip("/")
            # Also accept a pasted clone URL: .../p/PROJECT/r/REPO
            if "/p/" in text and "/r/" in text:
                project = text.split("/p/", 1)[1].split("/r/", 1)[0]
                repo = text.split("/r/", 1)[1]
            else:
                project, repo = default_project, text

        if project and repo:
            out.append(CsrRepo(project=project, repo=repo))

    return out


def load_settings() -> Settings:
    raw = _load_yaml()
    defaults = raw.get("defaults") or {}
    folders = raw.get("folders") or {}

    mirror_dir = os.getenv("MIRROR_DIR", "").strip()
    cache_root = Path(mirror_dir).parent if mirror_dir else ROOT / ".cache"

    return Settings(
        token=os.getenv("GITHUB_TOKEN", "").strip(),
        api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        cache_ttl=int(os.getenv("CACHE_TTL_SECONDS", "120")),
        max_concurrency=int(os.getenv("MAX_CONCURRENCY", "8")),
        repos=_parse_github_repos(raw.get("repos")),
        csr_repos=_parse_csr_repos(raw.get("gcloud")),
        branch_include=[str(p) for p in (raw.get("branch_include") or [])],
        branch_exclude=[str(p) for p in (raw.get("branch_exclude") or [])],
        days=int(defaults.get("days", 14)),
        commits_per_branch=int(defaults.get("commits_per_branch", 30)),
        folders_enabled=bool(folders.get("enabled", True)),
        folder_depth=max(1, int(folders.get("depth", 1))),
        folder_paths=[str(p) for p in (folders.get("paths") or [])],
        folder_exclude=[str(p) for p in (folders.get("exclude") or [])],
        gcloud_account=os.getenv("GCLOUD_ACCOUNT", "").strip(),
        gcloud_token=os.getenv("GCLOUD_ACCESS_TOKEN", "").strip(),
        mirror_dir=Path(mirror_dir) if mirror_dir else ROOT / ".cache" / "mirrors",
        store_path=cache_root / "commit-files.sqlite3",
        tag_store_path=cache_root / "staged-tags.sqlite3",
        tagger_name=os.getenv("TAGGER_NAME", "").strip(),
        tagger_email=os.getenv("TAGGER_EMAIL", "").strip(),
        git_timeout=int(os.getenv("GIT_TIMEOUT_SECONDS", "240")),
    )
