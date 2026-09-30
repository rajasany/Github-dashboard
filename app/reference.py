"""The migration form's reference lists, held in the database.

Releases, migration paths, change requestors and the microservice → repo →
track lead map used to live in config.yaml and an optional CSV. They live here
now, so they can be edited by the people who own them without a file edit and a
restart, and so two app instances see the same lists.

The file-based configuration is still read once: on a database with no lists in
it yet, whatever config.yaml and `microservices_file` hold is seeded in. That
makes the move invisible to an existing deployment, and it means the YAML stays
a perfectly good way to describe a *new* one. After that first seed the database
is the only source of truth, and editing the YAML does nothing.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .config import Employee, MigrationConfig, Microservice
from .db import Database

# The simple single-column lists, by the name the API and UI use for each.
SIMPLE_LISTS = {
    "releases": "release",
    "migration_paths": "migration_path",
    "change_requestors": "change_requestor",
}
LINK_KINDS = ("repo", "track_lead")

# How long a snapshot is reused. Writes invalidate it immediately; the TTL only
# matters for a second process editing the same database.
SNAPSHOT_TTL = 5.0


class ReferenceError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class ReferenceStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cached: MigrationConfig | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

        with db.connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ref_values (
                    id       {db.identity},
                    kind     TEXT NOT NULL,
                    value    TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (kind, value)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ref_microservices (
                    id       {db.identity},
                    name     TEXT NOT NULL UNIQUE,
                    position INTEGER NOT NULL DEFAULT 0,
                    allow_full_merge TEXT NOT NULL DEFAULT 'No'
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ref_microservice_links (
                    id           {db.identity},
                    microservice TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    value        TEXT NOT NULL,
                    position     INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (microservice, kind, value)
                )
                """
            )
        self._init_employees()
        self._init_roles()
        # A database made before full-merge permission existed gains the column.
        with db.connect() as conn:
            if "allow_full_merge" not in db.columns(conn, "ref_microservices"):
                conn.execute(
                    "ALTER TABLE ref_microservices ADD COLUMN "
                    "allow_full_merge TEXT NOT NULL DEFAULT 'No'"
                )

    def _init_roles(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ref_roles (
                    id       {self.db.identity},
                    role     TEXT NOT NULL,
                    pattern  TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (role, pattern)
                )
                """
            )

    def roles(self) -> dict[str, list[str]]:
        """Role assignments held in the database.

        Always carries a key per role, empty or not, so a caller never has to
        guess whether a missing key means "none" or "not asked about". A table
        with nothing in it at all is what makes config.yaml the answer instead.
        """
        from .config import ROLES

        out: dict[str, list[str]] = {role: [] for role in ROLES}
        with self.db.connect() as conn:
            for row in conn.all("SELECT role, pattern FROM ref_roles ORDER BY role, position, id"):
                out.setdefault(row["role"], []).append(row["pattern"])
        return out

    def set_role(self, role: str, patterns: list[str]) -> dict[str, list[str]]:
        """Replace the addresses holding one role."""
        from .config import ROLES

        if role not in ROLES:
            raise ReferenceError(f"“{role}” is not a role.")
        cleaned = self._clean(patterns, "address")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM ref_roles WHERE role = ?", (role,))
            conn.many(
                "INSERT INTO ref_roles (role, pattern, position) VALUES (?,?,?)",
                [(role, pattern, index) for index, pattern in enumerate(cleaned)],
            )
        return self.roles()

    def seed_roles(self, configured: dict[str, list[str]]) -> dict[str, int]:
        """Copy role assignments out of config.yaml, once, into an empty table."""
        # roles() always carries a key per role, so test the patterns.
        if any(self.roles().values()):
            return {}
        rows = [
            (role, pattern, index)
            for role, patterns in (configured or {}).items()
            for index, pattern in enumerate(patterns)
        ]
        if not rows:
            return {}
        with self.db.connect() as conn:
            conn.many("INSERT INTO ref_roles (role, pattern, position) VALUES (?,?,?)", rows)
        return {role: len(patterns) for role, patterns in configured.items() if patterns}

    def _init_employees(self) -> None:
        with self.db.connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ref_employees (
                    id       {self.db.identity},
                    number   TEXT NOT NULL UNIQUE,
                    name     TEXT NOT NULL DEFAULT '',
                    email    TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    # -- reading ------------------------------------------------------------ #

    def _read(self) -> MigrationConfig:
        with self.db.connect() as conn:
            values: dict[str, list[str]] = {name: [] for name in SIMPLE_LISTS}
            by_kind = {kind: name for name, kind in SIMPLE_LISTS.items()}
            for row in conn.all("SELECT kind, value FROM ref_values ORDER BY kind, position, id"):
                name = by_kind.get(row["kind"])
                if name:
                    values[name].append(row["value"])

            service_rows = conn.all(
                "SELECT name, allow_full_merge FROM ref_microservices ORDER BY position, id"
            )
            names = [r["name"] for r in service_rows]
            merges = {r["name"]: (r["allow_full_merge"] or "No") == "Yes" for r in service_rows}
            links: dict[str, dict[str, list[str]]] = {
                name: {kind: [] for kind in LINK_KINDS} for name in names
            }
            for row in conn.all(
                "SELECT microservice, kind, value FROM ref_microservice_links "
                "ORDER BY microservice, kind, position, id"
            ):
                bucket = links.get(row["microservice"])
                if bucket is not None and row["kind"] in bucket:
                    bucket[row["kind"]].append(row["value"])

            staff = [
                Employee(number=r["number"], name=r["name"], email=r["email"])
                for r in conn.all(
                    "SELECT number, name, email FROM ref_employees ORDER BY position, id"
                )
            ]

        return MigrationConfig(
            enabled=True,
            employees=staff,
            releases=values["releases"],
            migration_paths=values["migration_paths"],
            change_requestors=values["change_requestors"],
            microservices=[
                Microservice(name=n, repos=links[n]["repo"], track_leads=links[n]["track_lead"],
                             allow_full_merge=merges.get(n, False))
                for n in names
            ],
        )

    def snapshot(self, *, fresh: bool = False) -> MigrationConfig:
        """The current lists, as the shape the form and validation already take."""
        with self._lock:
            stale = time.monotonic() - self._cached_at > SNAPSHOT_TTL
            if fresh or self._cached is None or stale:
                self._cached = self._read()
                self._cached_at = time.monotonic()
            return self._cached

    def _invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def is_empty(self) -> bool:
        with self.db.connect() as conn:
            return not (
                conn.scalar("SELECT COUNT(*) AS n FROM ref_values")
                or conn.scalar("SELECT COUNT(*) AS n FROM ref_microservices")
                or conn.scalar("SELECT COUNT(*) AS n FROM ref_employees")
            )

    # -- seeding ------------------------------------------------------------ #

    def seed(self, cfg: MigrationConfig) -> dict[str, int]:
        """Fill empty lists from file-based configuration. Does nothing otherwise.

        Only ever runs against a database with no lists in it, so it cannot
        resurrect something an administrator deliberately deleted.
        """
        if not self.is_empty():
            return {}

        counts: dict[str, int] = {}
        with self.db.connect() as conn:
            for name, kind in SIMPLE_LISTS.items():
                values = list(getattr(cfg, name, []) or [])
                conn.many(
                    "INSERT INTO ref_values (kind, value, position) VALUES (?,?,?)",
                    [(kind, value, index) for index, value in enumerate(values)],
                )
                counts[name] = len(values)

            conn.many(
                "INSERT INTO ref_microservices (name, position, allow_full_merge) VALUES (?,?,?)",
                [(m.name, i, "Yes" if m.allow_full_merge else "No")
                 for i, m in enumerate(cfg.microservices)],
            )
            links: list[tuple] = []
            for service in cfg.microservices:
                links += [(service.name, "repo", v, i) for i, v in enumerate(service.repos)]
                links += [(service.name, "track_lead", v, i)
                          for i, v in enumerate(service.track_leads)]
            conn.many(
                "INSERT INTO ref_microservice_links (microservice, kind, value, position) "
                "VALUES (?,?,?,?)",
                links,
            )
            counts["microservices"] = len(cfg.microservices)

            conn.many(
                "INSERT INTO ref_employees (number, name, email, position) VALUES (?,?,?,?)",
                [(e.number, e.name, e.email, i) for i, e in enumerate(cfg.employees)],
            )
            counts["employees"] = len(cfg.employees)

        self._invalidate()
        return counts

    # -- writing ------------------------------------------------------------ #

    @staticmethod
    def _clean(values: list[str], what: str) -> list[str]:
        """Trim, drop blanks, and de-duplicate case-insensitively, keeping order.

        Blanks are dropped rather than rejected: these lists are edited as text,
        a line at a time, and a stray empty line is a typo to ignore rather than
        an error to refuse. An empty result is allowed — clearing a list is a
        legitimate thing for an administrator to do.
        """
        out: list[str] = []
        for value in values:
            text = (value or "").strip()
            if not text:
                continue
            if len(text) > 200:
                raise ReferenceError(f"That {what} is too long (200 characters maximum).")
            if not any(text.casefold() == seen.casefold() for seen in out):
                out.append(text)
        return out

    def set_list(self, name: str, values: list[str]) -> MigrationConfig:
        """Replace one simple list outright, keeping the order given."""
        kind = SIMPLE_LISTS.get(name)
        if kind is None:
            raise ReferenceError(f"“{name}” is not one of the lists.")

        cleaned = self._clean(values, name.rstrip("s").replace("_", " "))

        with self.db.connect() as conn:
            conn.execute("DELETE FROM ref_values WHERE kind = ?", (kind,))
            conn.many(
                "INSERT INTO ref_values (kind, value, position) VALUES (?,?,?)",
                [(kind, value, index) for index, value in enumerate(cleaned)],
            )
        self._invalidate()
        return self.snapshot(fresh=True)

    def save_microservice(
        self, name: str, repos: list[str], leads: list[str], *,
        rename_from: str = "", allow_full_merge: bool = False,
    ) -> MigrationConfig:
        """Create or update one microservice and its repos and leads."""
        named = self._clean([name], "microservice name")
        if not named:
            raise ReferenceError("A microservice needs a name.")
        name = named[0]
        old = (rename_from or "").strip()
        repos = self._clean(repos, "repo")
        leads = self._clean(leads, "track lead")

        with self.db.connect() as conn:
            existing = conn.one(
                "SELECT name, position FROM ref_microservices WHERE name = ?", (old or name,)
            )
            clash = conn.one("SELECT name FROM ref_microservices WHERE name = ?", (name,))
            if clash and old and clash["name"] != old:
                raise ReferenceError(f"There is already a microservice called “{name}”.")

            if existing is None:
                position = conn.scalar(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM ref_microservices"
                )
                conn.execute(
                    "INSERT INTO ref_microservices (name, position, allow_full_merge) "
                    "VALUES (?,?,?)",
                    (name, position, "Yes" if allow_full_merge else "No"),
                )
            else:
                conn.execute(
                    "UPDATE ref_microservices SET name = ?, allow_full_merge = ? WHERE name = ?",
                    (name, "Yes" if allow_full_merge else "No", existing["name"]),
                )

            # Replacing the links wholesale is simpler than diffing them, and
            # the lists are a handful of rows each.
            conn.execute(
                "DELETE FROM ref_microservice_links WHERE microservice = ?",
                (existing["name"] if existing else name,),
            )
            rows = [(name, "repo", v, i) for i, v in enumerate(repos)]
            rows += [(name, "track_lead", v, i) for i, v in enumerate(leads)]
            conn.many(
                "INSERT INTO ref_microservice_links (microservice, kind, value, position) "
                "VALUES (?,?,?,?)",
                rows,
            )
        self._invalidate()
        return self.snapshot(fresh=True)

    def delete_microservice(self, name: str) -> MigrationConfig:
        with self.db.connect() as conn:
            gone = conn.one(
                "DELETE FROM ref_microservices WHERE name = ? RETURNING name", (name,)
            )
            if not gone:
                raise ReferenceError(f"No microservice called “{name}”.", status=404)
            conn.execute("DELETE FROM ref_microservice_links WHERE microservice = ?", (name,))
        self._invalidate()
        return self.snapshot(fresh=True)

    def save_employee(self, number: str, name: str, email: str, *, rename_from: str = "") -> MigrationConfig:
        number = (self._clean([number], "employee number") or [""])[0]
        if not number:
            raise ReferenceError("An employee needs a number.")
        name = (self._clean([name], "employee name") or [""])[0]
        email = (self._clean([email], "email") or [""])[0]
        old = (rename_from or "").strip()

        with self.db.connect() as conn:
            existing = conn.one(
                "SELECT number FROM ref_employees WHERE number = ?", (old or number,)
            )
            clash = conn.one("SELECT number FROM ref_employees WHERE number = ?", (number,))
            if clash and old and clash["number"] != old:
                raise ReferenceError(f"There is already an employee numbered “{number}”.")

            if existing is None:
                position = conn.scalar(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM ref_employees"
                )
                conn.execute(
                    "INSERT INTO ref_employees (number, name, email, position) VALUES (?,?,?,?)",
                    (number, name, email, position),
                )
            else:
                conn.execute(
                    "UPDATE ref_employees SET number = ?, name = ?, email = ? WHERE number = ?",
                    (number, name, email, existing["number"]),
                )
        self._invalidate()
        return self.snapshot(fresh=True)

    def delete_employee(self, number: str) -> MigrationConfig:
        with self.db.connect() as conn:
            gone = conn.one(
                "DELETE FROM ref_employees WHERE number = ? RETURNING number", (number,)
            )
            if not gone:
                raise ReferenceError(f"No employee numbered “{number}”.", status=404)
        self._invalidate()
        return self.snapshot(fresh=True)

    def in_use(self, name: str) -> int:
        """How many records name this microservice — asked before deleting one."""
        with self.db.connect() as conn:
            if not self.db.table_exists(conn, "migrations"):
                return 0
            return int(conn.scalar(
                "SELECT COUNT(*) AS n FROM migrations WHERE microservice = ?", (name,)
            ) or 0)

    # -- for the admin screen ------------------------------------------------ #

    def as_payload(self) -> dict[str, Any]:
        cfg = self.snapshot()
        return {
            "releases": cfg.releases,
            "migration_paths": cfg.migration_paths,
            "change_requestors": cfg.change_requestors,
            "microservices": [
                {"name": m.name, "repos": m.repos, "track_leads": m.track_leads,
                 "allow_full_merge": m.allow_full_merge}
                for m in cfg.microservices
            ],
            "roles": self.roles(),
            "employees": [
                {"number": e.number, "name": e.name, "email": e.email, "label": e.label}
                for e in cfg.employees
            ],
        }
