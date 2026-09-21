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


# "admin" is a superset: every permission check any other role satisfies, it
# satisfies too (see User.has_any). It exists so that clearing up the register —
# deactivating and deleting records — can be separated from approving them.
ROLES = ("developer", "approver", "devops", "admin")


@dataclass(frozen=True)
class Microservice:
    """One microservice, with the repos and track leads that depend on it."""

    name: str
    repos: list[str] = field(default_factory=list)
    track_leads: list[str] = field(default_factory=list)


@dataclass
class AuthConfig:
    """How a request's user is identified, and which roles they hold.

    Identity comes from a header set by an SSO proxy in front of this app. That
    is only trustworthy if the app cannot be reached except through that proxy —
    anyone able to connect directly can set the header themselves. `trusted_proxies`
    is the guard: when set, the header is honoured only from those addresses.
    """

    header: str = "X-Forwarded-Email"
    trusted_proxies: list[str] = field(default_factory=list)
    # Testing without an SSO proxy: the caller says who they are. Off unless
    # switched on explicitly, and honoured only from the loopback interface
    # unless dev_allow_remote is also set — so leaving it on by mistake in a
    # deployed environment does not hand everyone the approver role.
    dev_mode: bool = False
    dev_allow_remote: bool = False
    # The identity assumed when dev mode is on and nothing has been chosen.
    dev_user: str = ""
    # role -> email patterns ("someone@x.com" or "*@x.com"), matched case-insensitively.
    roles: dict[str, list[str]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        # self.roles always holds a key per role, so test the patterns, not the dict.
        return any(self.roles.values())


@dataclass
class MigrationConfig:
    """Reference lists for the migration request form."""

    enabled: bool = False
    releases: list[str] = field(default_factory=list)
    migration_paths: list[str] = field(default_factory=list)
    microservices: list[Microservice] = field(default_factory=list)
    change_requestors: list[str] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)

    def service(self, name: str) -> Microservice | None:
        want = (name or "").strip().casefold()
        return next((m for m in self.microservices if m.name.casefold() == want), None)

    @property
    def service_names(self) -> list[str]:
        return [m.name for m in self.microservices]


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
    # Migration requests
    auth: AuthConfig = field(default_factory=AuthConfig)
    migrations: MigrationConfig = field(default_factory=MigrationConfig)
    migration_store_path: Path = ROOT / ".cache" / "migrations.sqlite3"

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


def _parse_auth(section: dict | None) -> AuthConfig:
    section = section or {}
    raw_roles = section.get("roles") or {}
    roles: dict[str, list[str]] = {}
    for role in ROLES:
        entries = raw_roles.get(role) or []
        if isinstance(entries, str):
            entries = [entries]
        roles[role] = [str(e).strip() for e in entries if str(e).strip()]

    proxies = section.get("trusted_proxies") or []
    if isinstance(proxies, str):
        proxies = [proxies]

    return AuthConfig(
        header=str(section.get("header") or "X-Forwarded-Email").strip(),
        trusted_proxies=[str(p).strip() for p in proxies if str(p).strip()],
        dev_mode=bool(section.get("dev_mode", False)),
        dev_allow_remote=bool(section.get("dev_allow_remote", False)),
        dev_user=str(section.get("dev_user") or "").strip(),
        roles=roles,
    )


def _parse_migrations(section: dict | None) -> MigrationConfig:
    section = section or {}

    def as_list(key: str) -> list[str]:
        raw = section.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(v).strip() for v in raw if str(v).strip()]

    services: list[Microservice] = []
    for entry in section.get("microservices") or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            repos = entry.get("repos") or ([entry["repo"]] if entry.get("repo") else [])
            leads = entry.get("track_leads") or (
                [entry["track_lead"]] if entry.get("track_lead") else []
            )
        else:
            # A bare name is allowed; its repo and lead lists are then empty.
            name, repos, leads = str(entry).strip(), [], []
        if not name:
            continue
        if isinstance(repos, str):
            repos = [repos]
        if isinstance(leads, str):
            leads = [leads]
        services.append(
            Microservice(
                name=name,
                repos=[str(r).strip() for r in repos if str(r).strip()],
                track_leads=[str(l).strip() for l in leads if str(l).strip()],
            )
        )

    return MigrationConfig(
        enabled=bool(section.get("enabled", bool(services))),
        releases=as_list("releases"),
        migration_paths=as_list("migration_paths"),
        microservices=services,
        change_requestors=as_list("change_requestors"),
        approvers=as_list("approvers"),
    )


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
        auth=_parse_auth(raw.get("auth")),
        migrations=_parse_migrations(raw.get("migrations")),
        migration_store_path=cache_root / "migrations.sqlite3",
    )
