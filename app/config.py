"""Configuration loading: secrets from .env, tracked repos from config.yaml."""

from __future__ import annotations

import os
import re
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
class Employee:
    """Someone a migration request can be raised on behalf of.

    `email` is what ties an employee to a signed-in user, so the form can put
    the right person in Change Requestor without being asked.
    """

    number: str
    name: str
    email: str = ""

    @property
    def label(self) -> str:
        # A plain hyphen, not an em dash: this value is typed into spreadsheets.
        return f"{self.number} - {self.name}" if self.name else self.number


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


SERVICE_COLUMNS: dict[str, list[str]] = {
    "microservice": ["micro service name", "microservice name", "micro service",
                     "microservice", "service name", "ms name", "service", "ms"],
    "repo": ["repo name", "repository name", "repository", "repo", "git repo"],
    "track_lead": ["track lead name", "track lead", "lead name", "lead", "owner"],
}


def load_microservices(path: Path) -> tuple[list[Microservice], str]:
    """Read the microservice → repo → track lead map from a .csv or .xlsx.

    Returns (services, problem). A problem is reported rather than raised: a
    typo in this file must not stop the whole app from starting, it must show up
    where the person who can fix it will see it.

    One row per pairing, repeated for each repo or lead a service has. A cell may
    also hold several values separated by commas or semicolons.
    """
    from .spreadsheet import SheetError, parse_columns  # local: avoids a cycle

    try:
        content = path.read_bytes()
    except OSError as exc:
        return [], f"{path} could not be read: {exc.strerror or exc}."

    try:
        sheet = parse_columns(
            path.name, content,
            columns=SERVICE_COLUMNS,
            required="microservice",
            hint="It needs a “Micro Service Name” column, and usually “Repo Name” "
                 "and “Track Lead Name” as well.",
        )
    except SheetError as exc:
        return [], f"{path}: {exc}"

    def cells(value: str) -> list[str]:
        return [part.strip() for part in re.split(r"[;,]", value or "") if part.strip()]

    order: list[str] = []
    repos: dict[str, list[str]] = {}
    leads: dict[str, list[str]] = {}
    for row in sheet["rows"]:
        name = (row.get("microservice") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key not in repos:
            order.append(name)
            repos[key], leads[key] = [], []
        # Preserve first-seen order, drop repeats — the same pairing often
        # appears on several rows of a hand-maintained sheet.
        for repo in cells(row.get("repo", "")):
            if repo not in repos[key]:
                repos[key].append(repo)
        for lead in cells(row.get("track_lead", "")):
            if lead not in leads[key]:
                leads[key].append(lead)

    services = [
        Microservice(name=name, repos=repos[name.casefold()], track_leads=leads[name.casefold()])
        for name in order
    ]
    if not services:
        # A backstop: parse_columns normally rejects an empty sheet first, with a
        # better message. This exists so an empty list can never be returned as
        # though it were fine, which would silently empty the dropdowns.
        return [], f"{path} has a header but no microservice rows."
    return services, ""


@dataclass
class MigrationConfig:
    """Reference lists for the migration request form."""

    enabled: bool = False
    releases: list[str] = field(default_factory=list)
    migration_paths: list[str] = field(default_factory=list)
    microservices: list[Microservice] = field(default_factory=list)
    change_requestors: list[str] = field(default_factory=list)
    employees: list[Employee] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)
    # Optional external source for the microservice map.
    microservices_file: Path | None = None
    inline_microservices: list[Microservice] = field(default_factory=list)
    services_error: str = ""
    _stamp: object = None

    def refresh(self) -> None:
        """Re-read the microservice file if it has changed on disk.

        Cheap enough to call per request: a stat, and a parse only when the file
        has actually moved. It means a track lead changing does not need a
        restart, unlike the rest of config.yaml.
        """
        if not self.microservices_file:
            return
        try:
            info = self.microservices_file.stat()
            stamp: object = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = "missing"
        if stamp == self._stamp:
            return
        self._stamp = stamp

        services, problem = load_microservices(self.microservices_file)
        self.services_error = problem
        # On a bad file, fall back to whatever config.yaml listed inline rather
        # than presenting an empty form with no explanation.
        self.microservices = services or list(self.inline_microservices)

    @property
    def employee_labels(self) -> list[str]:
        return [e.label for e in self.employees]

    def employee_for(self, email: str) -> Employee | None:
        """The employee a signed-in address belongs to, if any."""
        who = (email or "").strip().casefold()
        if not who:
            return None
        return next((e for e in self.employees if e.email.strip().casefold() == who), None)

    def match_employee(self, value: str) -> Employee | None:
        """Resolve a typed or pasted value to an employee.

        Accepts the full label, the number alone, or the name alone, so a
        spreadsheet holding only employee numbers still works.
        """
        text = (value or "").strip()
        if not text:
            return None
        folded = text.casefold()
        for employee in self.employees:
            if folded in (employee.label.casefold(), employee.number.casefold(),
                          employee.name.casefold()):
                return employee
        # "12345 — Name" with a different dash, or odd spacing.
        head = text.split()[0].strip(" -—–")
        return next((e for e in self.employees if e.number.casefold() == head.casefold()), None)

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
    # Where the migration register lives. "sqlite:///path" or a PostgreSQL URL.
    # The git caches stay on SQLite regardless — see the README.
    database_url: str = ""

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


def _clean_list(raw) -> list[str]:
    """YAML values to a list of non-empty strings.

    A blank list item (`-` with nothing after it) parses as None, and `str(None)`
    is the perfectly non-empty string "None" — which then behaves as a real
    value. Left unguarded that invents a role pattern, or a release called None.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    out = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _parse_auth(section: dict | None) -> AuthConfig:
    section = section or {}
    raw_roles = section.get("roles") or {}
    roles = {role: _clean_list(raw_roles.get(role)) for role in ROLES}

    return AuthConfig(
        header=str(section.get("header") or "X-Forwarded-Email").strip(),
        trusted_proxies=_clean_list(section.get("trusted_proxies")),
        dev_mode=bool(section.get("dev_mode", False)),
        dev_allow_remote=bool(section.get("dev_allow_remote", False)),
        dev_user=str(section.get("dev_user") or "").strip(),
        roles=roles,
    )


def _parse_migrations(section: dict | None) -> MigrationConfig:
    section = section or {}

    def as_list(key: str) -> list[str]:
        return _clean_list(section.get(key))

    employees: list[Employee] = []
    for entry in section.get("employees") or []:
        if isinstance(entry, dict):
            number = str(entry.get("number") or entry.get("id") or "").strip()
            ename = str(entry.get("name") or "").strip()
            email = str(entry.get("email") or "").strip()
        else:
            # "E1002 - John Roe" or "E1003 - Amy Poe, amy@example.com"
            parts = [part.strip() for part in re.split(r"[,|]", str(entry)) if part.strip()]
            number, _, ename = (x.strip() for x in parts[0].partition("-"))
            email = next((x for x in parts[1:] if "@" in x), "")
            if not ename and len(parts) > 1 and "@" not in parts[1]:
                ename = parts[1]
        if number or ename:
            employees.append(Employee(number=number or ename, name=ename, email=email))

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
        services.append(
            Microservice(name=name, repos=_clean_list(repos), track_leads=_clean_list(leads))
        )

    raw_path = str(section.get("microservices_file") or "").strip()
    path = None
    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = ROOT / path

    cfg = MigrationConfig(
        # A file counts as configuration too, so the tab can be switched on by
        # pointing at one without also listing services inline.
        enabled=bool(section.get("enabled", bool(services) or bool(path))),
        releases=as_list("releases"),
        migration_paths=as_list("migration_paths"),
        microservices=services,
        change_requestors=as_list("change_requestors"),
        employees=employees,
        approvers=as_list("approvers"),
        microservices_file=path,
        inline_microservices=list(services),
    )
    # Load once now so a broken path is visible at startup, not on first use.
    cfg.refresh()
    return cfg


def _database_url(section: dict | None, cache_root: Path) -> str:
    """Where the migration register lives.

    DATABASE_URL wins, then `database.url` in config.yaml, then the SQLite file
    the app has always used — so an existing deployment keeps working untouched.
    """
    from_env = os.getenv("DATABASE_URL", "").strip()
    if from_env:
        return from_env
    configured = str((section or {}).get("url") or "").strip()
    if configured:
        return configured
    return f"sqlite:///{cache_root / 'migrations.sqlite3'}"


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
        database_url=_database_url(raw.get("database"), cache_root),
    )
