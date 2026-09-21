"""Migration requests: the SIT → QA → PROD movement workflow.

A developer raises a request; an approver approves it and plans a QA date; DevOps
records the QA migration; the approver marks it ready for production after testing;
DevOps records the production migration. Each of those steps is owned by a role,
and the fields belonging to a step are writable only by that role and only once the
preceding step is done.

Two rules are enforced here rather than in the UI, because the UI is not a security
boundary:

  * **field-level roles** — every write goes through `apply_changes`, which rejects
    any field the caller's role does not own at the record's current stage;
  * **freeze windows** — an approver can close record entry for a time span, during
    which every create and edit is refused.

The record's status is derived from its own fields rather than stored, so there is
no second source of truth to fall out of step with them.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import User
from .config import MigrationConfig, Settings

YES, NO = "Yes", "No"
YESNO = (YES, NO)

# Stages, in order. A field belongs to exactly one.
STAGES = ("request", "approval", "qa", "prod_gate", "prod")


class MigrationError(Exception):
    """A rejected write. `status` is the HTTP code the API should answer with.

    `fields` maps field key -> what is wrong with it, so a client can mark the
    offending controls instead of only printing a sentence. A form with 28 fields
    needs to say *which* one, not just that something is missing.
    """

    def __init__(self, message: str, status: int = 422, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields or {}


class FrozenError(MigrationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=423)  # 423 Locked


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # auto | text | longtext | enum | yesno | date
    stage: str
    # Roles permitted to write it. Empty means the system sets it, nobody types it.
    roles: tuple[str, ...] = ()
    options: str = ""  # name of a list in the meta payload
    required: bool = False
    required_if: tuple[str, str] | None = None
    amber_when: str = ""  # a value that marks the row as carrying risk
    depends_on: str = ""
    default: str = ""
    help: str = ""

    @property
    def system(self) -> bool:
        return self.kind == "auto"


# The whole contract in one place: the table, the API payload, the form, the
# spreadsheet template and the role checks are all derived from this list.
FIELDS: tuple[Field, ...] = (
    Field("sl_no", "SL#", "auto", "request", help="Sequence, assigned on creation."),
    Field("release", "Rel#", "enum", "request", ("developer",), options="releases", required=True),
    Field("migration_path", "Migration Path", "enum", "request", ("developer",),
          options="migration_paths", required=True),
    Field("microservice", "Micro Service Name", "enum", "request", ("developer",),
          options="microservices", required=True),
    Field("repo_name", "Repo Name", "enum", "request", ("developer",),
          depends_on="microservice", required=True,
          help="Narrowed to the repos of the selected microservice."),
    Field("track_lead", "Track Lead Name", "enum", "request", ("developer",),
          depends_on="microservice", required=True,
          help="Narrowed to the track leads of the selected microservice."),
    Field("created_by", "Created By", "auto", "request", help="Taken from the signed-in user."),
    Field("change_requestor", "Change Requestor", "enum", "request", ("developer",),
          options="change_requestors", required=True),
    Field("reason", "Reason for Movement", "longtext", "request", ("developer",)),
    Field("change_description", "Change Description", "longtext", "request", ("developer",)),
    Field("code_image_change", "Code & Image Change?", "yesno", "request", ("developer",),
          amber_when=YES, default=NO),
    Field("commit_hash", "Commit Hash", "text", "request", ("developer",),
          required_if=("code_image_change", YES),
          help="Required when there is a code or image change."),
    Field("env_change", "Environment Change", "yesno", "request", ("developer",),
          amber_when=YES, default=NO),
    Field("env_secret", "Env Secret Details", "yesno", "request", ("developer",),
          amber_when=YES, default=NO),
    Field("ddl_dml", "DDL/DML", "yesno", "request", ("developer",), amber_when=YES, default=NO),
    Field("db_script_path", "DB Script Path", "text", "request", ("developer",),
          required_if=("ddl_dml", YES), help="Required when there is a DDL/DML change."),
    Field("date_created", "Date Created", "auto", "request"),

    Field("approved_by", "Approved By", "enum", "approval", ("approver",), options="approvers",
          help="Approvers only. Clearing it withdraws the approval and reopens the request."),
    Field("date_approved", "Date Approved", "auto", "approval"),
    Field("qa_date_planned", "QA Date Planned", "date", "approval", ("approver",)),

    Field("executed_in_qa", "Executed in QA", "yesno", "qa", ("devops",), default=NO,
          help="DevOps only, once the request is approved."),
    Field("qa_migrated_by", "QA Migrated By", "auto", "qa"),
    Field("qa_migration_remarks", "QA Migration Remarks", "longtext", "qa", ("devops",)),

    Field("ready_for_prod", "Ready for Prod", "yesno", "prod_gate", ("approver",), default=NO,
          help="Approvers only, once the QA migration is done."),

    Field("executed_in_prod", "Executed in PROD", "yesno", "prod", ("devops",), default=NO,
          help="DevOps only, once the request is marked ready for prod."),
    Field("prod_migration_date", "Prod Migration Date", "auto", "prod"),
    Field("prod_migrated_by", "Prod Migrated By", "auto", "prod"),
    Field("prod_migration_remarks", "Prod Migration Remarks", "longtext", "prod", ("devops",)),
)

# A mistyped stage would silently make a field unreachable — stage_open() would
# fall through to its permissive default and the field would never lock.
_bad_stages = sorted({f.stage for f in FIELDS} - set(STAGES))
assert not _bad_stages, f"unknown stage(s) in FIELDS: {_bad_stages}"

BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}
# Fields a developer fills in when raising a request — also the spreadsheet columns.
REQUEST_KEYS: tuple[str, ...] = tuple(
    f.key for f in FIELDS if f.stage == "request" and not f.system
)
AMBER_KEYS: tuple[str, ...] = tuple(f.key for f in FIELDS if f.amber_when)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

STATUS_LABELS = {
    "submitted": "Awaiting approval",
    "approved": "Approved",
    "in_qa": "Migrated to QA",
    "ready_for_prod": "Ready for prod",
    "in_prod": "Migrated to prod",
}


# Sorting by status follows the workflow rather than the alphabet, which is why
# STATUS_LABELS is ordered and used as the key.
STATUS_ORDER = list(STATUS_LABELS)

SORTABLE = (
    "sl_no", "release", "migration_path", "microservice", "repo_name",
    "track_lead", "created_by", "date_created", "status",
)


def sort_rows(rows: list[dict[str, Any]], sort: str, direction: str) -> list[dict[str, Any]]:
    """Order the register. An unknown column falls back to SL#, never an error."""
    field = sort if sort in SORTABLE else "sl_no"
    reverse = str(direction).lower() != "asc"

    def key(row: dict[str, Any]):
        if field == "sl_no":
            return row["sl_no"]
        if field == "date_created":
            # Undated rows sort as oldest rather than jumping to the top.
            return row.get("date_created_epoch") or 0.0
        if field == "status":
            return STATUS_ORDER.index(row["status"])
        # Case-insensitive, with SL# as a stable tie-break.
        return (str(row.get(field, "")).casefold(), row["sl_no"])

    return sorted(rows, key=key, reverse=reverse)


def status_of(row: dict[str, Any]) -> str:
    """Derived from the record's own fields — never stored, so never stale."""
    if row.get("executed_in_prod") == YES:
        return "in_prod"
    if row.get("ready_for_prod") == YES:
        return "ready_for_prod"
    if row.get("executed_in_qa") == YES:
        return "in_qa"
    if (row.get("approved_by") or "").strip():
        return "approved"
    return "submitted"


# The fields stored as epoch seconds. Named explicitly rather than matched on a
# "date_" prefix, which missed prod_migration_date and caught nothing useful.
TIMESTAMP_FIELDS = frozenset({"date_created", "date_approved", "prod_migration_date"})


def _iso(value: Any) -> str:
    """Epoch seconds to an ISO-8601 UTC string; anything else through unchanged."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _stamp(key: str, value: Any) -> str:
    """Render a stored value for display, turning epochs into readable times.

    They are stored as strings, so `_iso` alone would hand them straight back.
    """
    if key in TIMESTAMP_FIELDS and value not in (None, ""):
        try:
            return _iso(float(value))
        except (TypeError, ValueError):
            return str(value)
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #


def stage_open(key: str, row: dict[str, Any]) -> tuple[bool, str]:
    """Has the workflow reached the point where this field may be written?

    Returns (open, why-not). The ordering is the substance of the workflow: QA
    cannot be recorded before approval, prod cannot be recorded before the
    approver has signed off on QA testing.
    """
    spec = BY_KEY[key]
    approved = bool((row.get("approved_by") or "").strip())

    if spec.stage == "request":
        # Editing the request after approval would invalidate what was approved.
        if approved:
            return False, "The request is approved; withdraw the approval to change it."
        return True, ""

    if spec.stage == "approval":
        return True, ""

    if spec.stage == "qa":
        if not approved:
            return False, "Not approved yet."
        return True, ""

    if spec.stage == "prod_gate":
        if row.get("executed_in_qa") != YES:
            return False, "Not migrated to QA yet."
        return True, ""

    if spec.stage == "prod":
        if row.get("ready_for_prod") != YES:
            return False, "Not marked ready for prod yet."
        return True, ""

    return True, ""


def can_write(
    key: str, user: User, row: dict[str, Any], *, creating: bool = False
) -> tuple[bool, str, str]:
    """(allowed, why not, kind).

    `kind` separates a refusal that will never change — the caller lacks the role,
    or the field is not theirs — from one that is merely premature, because the
    workflow has not reached that stage. The API maps the first to 403 and the
    second to 409, so a client can tell "never" from "not yet".
    """
    spec = BY_KEY.get(key)
    if spec is None:
        return False, "Not a field of this form.", "unknown"
    if spec.system:
        return False, "Set automatically; it cannot be typed.", "system"
    if not user.has_any(*spec.roles):
        return False, f"Needs the {' or '.join(spec.roles)} role.", "role"

    if creating:
        if spec.stage == "request":
            return True, "", ""
        return False, "Set later in the workflow.", "stage"

    # A retired record is read-only until someone brings it back, so that what
    # it said when it was retired is what it still says.
    if row.get("active") is False:
        return False, "This record is inactive. Reactivate it to make changes.", "inactive"

    # A request belongs to the person who raised it; approvers may also correct it.
    if spec.stage == "request" and user.is_developer and not user.is_approver:
        if (row.get("created_by") or "").casefold() != user.email.casefold():
            return False, "Only the person who raised the request can edit it.", "owner"

    allowed, why = stage_open(key, row)
    return allowed, why, "" if allowed else "stage"


def editable_fields(user: User, row: dict[str, Any]) -> list[str]:
    return [f.key for f in FIELDS if can_write(f.key, user, row)[0]]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def option_lists(cfg: MigrationConfig, microservice: str = "") -> dict[str, list[str]]:
    """The values each enum field accepts, for a given microservice selection."""
    service = cfg.service(microservice) if microservice else None
    return {
        "releases": list(cfg.releases),
        "migration_paths": list(cfg.migration_paths),
        "microservices": cfg.service_names,
        "repo_name": list(service.repos) if service else [],
        "track_lead": list(service.track_leads) if service else [],
        "change_requestors": list(cfg.change_requestors),
        "approvers": list(cfg.approvers),
        "yesno": list(YESNO),
    }


def _clean(spec: Field, value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if spec.kind == "yesno":
        low = text.casefold()
        if low in ("yes", "y", "true", "1"):
            return YES
        if low in ("no", "n", "false", "0"):
            return NO
        return text  # kept as-is so validation can report it
    return text


def validate(values: dict[str, str], cfg: MigrationConfig, *, partial: bool = False) -> dict[str, str]:
    """Check a set of already-cleaned values. Returns {field: problem}."""
    problems: dict[str, str] = {}
    lists = option_lists(cfg, values.get("microservice", ""))

    for key, value in values.items():
        spec = BY_KEY.get(key)
        if spec is None:
            problems[key] = "Not a field of this form."
            continue

        if spec.kind == "yesno" and value and value not in YESNO:
            problems[key] = f"Must be {YES} or {NO}."
        elif spec.kind == "date" and value and not _DATE.match(value):
            problems[key] = "Must be a date as YYYY-MM-DD."
        elif spec.kind == "enum" and value:
            allowed = lists.get(spec.options or key, [])
            if allowed and not any(value.casefold() == a.casefold() for a in allowed):
                shown = ", ".join(allowed[:6]) + ("…" if len(allowed) > 6 else "")
                problems[key] = f"Not one of the permitted values ({shown})."
            elif not allowed and spec.depends_on:
                chosen = values.get(spec.depends_on, "")
                problems[key] = (
                    f"Select a {BY_KEY[spec.depends_on].label} first."
                    if not chosen
                    else f"“{chosen}” has no {spec.label.lower()} configured."
                )

    if values.get("commit_hash") and not _SHA.match(values["commit_hash"]):
        problems["commit_hash"] = "Not a commit hash (7–40 hex characters)."

    # Requiredness is checked against the merged record, so a partial edit that
    # leaves a required field untouched is not reported as missing.
    if not partial:
        for spec in FIELDS:
            if spec.required and not values.get(spec.key):
                problems.setdefault(spec.key, "Required.")

    for spec in FIELDS:
        if spec.required_if:
            trigger, expected = spec.required_if
            if values.get(trigger) == expected and not values.get(spec.key):
                problems[spec.key] = f"Required when {BY_KEY[trigger].label} is {expected}."

    return problems


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #

_COLUMNS = ", ".join(f"{f.key} TEXT NOT NULL DEFAULT ''" for f in FIELDS if f.key != "sl_no")


class MigrationStore:
    """Migration requests, their audit trail, and the freeze windows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # sl_no is the sequence the form calls SL#, so it is the row id itself.
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS migrations (
                    sl_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    {_COLUMNS},
                    active TEXT NOT NULL DEFAULT 'Yes',
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            # Databases created before `active` existed are brought forward here
            # rather than being left to fail on the first query that mentions it.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(migrations)")}
            if "active" not in columns:
                conn.execute(
                    "ALTER TABLE migrations ADD COLUMN active TEXT NOT NULL DEFAULT 'Yes'"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_audit (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    sl_no     INTEGER NOT NULL,
                    at        REAL NOT NULL,
                    who       TEXT NOT NULL,
                    field     TEXT NOT NULL,
                    old_value TEXT NOT NULL DEFAULT '',
                    new_value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS freezes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    starts_at  REAL NOT NULL,
                    ends_at    REAL NOT NULL,
                    reason     TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_sl ON migration_audit(sl_no)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- records ----------------------------------------------------------- #

    @staticmethod
    def _shape(row: sqlite3.Row) -> dict[str, Any]:
        data = {k: row[k] for k in row.keys()}
        # The raw epoch is kept for sorting and range filtering; comparing the
        # rendered strings would sort blanks oddly and cost a reparse per row.
        raw_created = data.get("date_created")
        for key in ("date_created", "date_approved", "prod_migration_date"):
            data[key] = _iso(float(data[key])) if data.get(key) else ""
        try:
            data["date_created_epoch"] = float(raw_created) if raw_created else None
        except (TypeError, ValueError):
            data["date_created_epoch"] = None
        data["status"] = status_of(data)
        data["status_label"] = STATUS_LABELS[data["status"]]
        data["active"] = data.get("active", YES) != NO
        data["amber"] = [k for k in AMBER_KEYS if data.get(k) == BY_KEY[k].amber_when]
        return data

    def get(self, sl_no: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)).fetchone()
            return self._shape(row) if row else None

    def _raw(self, conn: sqlite3.Connection, sl_no: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)).fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list(
        self,
        *,
        sort: str = "sl_no",
        direction: str = "desc",
        created_from: float | None = None,
        created_to: float | None = None,
        include_inactive: bool = False,
        **filters: str,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        for key in ("release", "migration_path", "microservice", "repo_name", "created_by"):
            value = (filters.get(key) or "").strip()
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)

        if not include_inactive:
            clauses.append("active != ?")
            params.append(NO)

        sql = "SELECT * FROM migrations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        with self._connect() as conn:
            rows = [self._shape(r) for r in conn.execute(sql, params).fetchall()]

        # Status is derived rather than stored, so it cannot be a SQL clause.
        status = (filters.get("status") or "").strip()
        if status:
            rows = [r for r in rows if r["status"] == status]

        # The range comes in as instants, so the caller's timezone — not the
        # server's — decides which calendar day a record belongs to.
        if created_from is not None:
            rows = [r for r in rows if (r["date_created_epoch"] or 0) >= created_from]
        if created_to is not None:
            rows = [r for r in rows if (r["date_created_epoch"] or 0) <= created_to]

        return sort_rows(rows, sort, direction)

    def insert(self, values: dict[str, str], user: User) -> dict[str, Any]:
        now = time.time()
        payload = {f.key: "" for f in FIELDS if f.key != "sl_no"}
        payload.update({k: v for k, v in values.items() if k in payload})
        payload["created_by"] = user.email
        payload["date_created"] = str(now)
        for spec in FIELDS:
            if spec.default and not payload.get(spec.key):
                payload[spec.key] = spec.default

        keys = list(payload)
        with self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO migrations ({', '.join(keys)}, updated_at) "
                f"VALUES ({', '.join('?' * len(keys))}, ?)",
                [*[payload[k] for k in keys], now],
            )
            sl_no = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO migration_audit (sl_no, at, who, field, old_value, new_value) "
                "VALUES (?,?,?,?,?,?)",
                [(sl_no, now, user.email, "created", "", str(sl_no))],
            )
            return self._shape(conn.execute(
                "SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)
            ).fetchone())

    def update(self, sl_no: int, changes: dict[str, str], user: User) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            before = self._raw(conn, sl_no)
            if before is None:
                raise MigrationError(f"No record with SL# {sl_no}.", status=404)

            changed = {k: v for k, v in changes.items() if str(before.get(k, "")) != str(v)}
            if not changed:
                return self._shape(conn.execute(
                    "SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)
                ).fetchone())

            assignments = ", ".join(f"{k} = ?" for k in changed)
            conn.execute(
                f"UPDATE migrations SET {assignments}, updated_at = ? WHERE sl_no = ?",
                [*changed.values(), now, sl_no],
            )
            conn.executemany(
                "INSERT INTO migration_audit (sl_no, at, who, field, old_value, new_value) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (sl_no, now, user.email, k, str(before.get(k, "")), str(v))
                    for k, v in changed.items()
                ],
            )
            return self._shape(conn.execute(
                "SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)
            ).fetchone())

    def set_active(self, sl_no: int, active: bool, user: User) -> dict[str, Any]:
        """Retire a record, or bring it back. Reversible, and it leaves a trail."""
        now = time.time()
        with self._connect() as conn:
            before = self._raw(conn, sl_no)
            if before is None:
                raise MigrationError(f"No record with SL# {sl_no}.", status=404)
            was = before.get("active", YES)
            want = YES if active else NO
            if was != want:
                conn.execute(
                    "UPDATE migrations SET active = ?, updated_at = ? WHERE sl_no = ?",
                    (want, now, sl_no),
                )
                conn.execute(
                    "INSERT INTO migration_audit (sl_no, at, who, field, old_value, new_value) "
                    "VALUES (?,?,?,?,?,?)",
                    (sl_no, now, user.email, "active", was, want),
                )
            return self._shape(conn.execute(
                "SELECT * FROM migrations WHERE sl_no = ?", (sl_no,)
            ).fetchone())

    def delete(self, sl_no: int) -> bool:
        """Remove a record and its history. Irreversible — deactivate is not.

        The audit rows go with it: leaving them behind would keep a trail nobody
        can reach, for a record that no longer exists.
        """
        with self._connect() as conn:
            gone = conn.execute("DELETE FROM migrations WHERE sl_no = ?", (sl_no,)).rowcount > 0
            if gone:
                conn.execute("DELETE FROM migration_audit WHERE sl_no = ?", (sl_no,))
            return gone

    def audit(self, sl_no: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_audit WHERE sl_no = ? ORDER BY id", (sl_no,)
            ).fetchall()
        out = []
        for row in rows:
            spec = BY_KEY.get(row["field"])
            if row["field"] == "active":
                out.append({
                    "at": _iso(row["at"]),
                    "who": row["who"],
                    "field": "active",
                    "label": "Record",
                    "old_value": "active" if row["old_value"] != NO else "inactive",
                    "new_value": "active" if row["new_value"] != NO else "inactive",
                })
                continue
            out.append({
                "at": _iso(row["at"]),
                "who": row["who"],
                "field": row["field"],
                "label": spec.label if spec else row["field"].replace("_", " ").title(),
                "old_value": _stamp(row["field"], row["old_value"]),
                "new_value": _stamp(row["field"], row["new_value"]),
            })
        return out

    # -- freezes ------------------------------------------------------------ #

    def add_freeze(self, starts_at: float, ends_at: float, reason: str, user: User) -> dict[str, Any]:
        if ends_at <= starts_at:
            raise MigrationError("The freeze must end after it starts.")
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO freezes (starts_at, ends_at, reason, created_by, created_at) "
                "VALUES (?,?,?,?,?)",
                (starts_at, ends_at, reason.strip(), user.email, now),
            )
            row = conn.execute("SELECT * FROM freezes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._freeze(row)

    def delete_freeze(self, freeze_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM freezes WHERE id = ?", (freeze_id,)).rowcount > 0

    @staticmethod
    def _freeze(row: sqlite3.Row) -> dict[str, Any]:
        now = time.time()
        return {
            "id": row["id"],
            "starts_at": _iso(row["starts_at"]),
            "ends_at": _iso(row["ends_at"]),
            "starts_epoch": row["starts_at"],
            "ends_epoch": row["ends_at"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "active": row["starts_at"] <= now < row["ends_at"],
            "past": row["ends_at"] <= now,
        }

    def freezes(self, include_past: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM freezes ORDER BY starts_at DESC").fetchall()
        out = [self._freeze(r) for r in rows]
        return out if include_past else [f for f in out if not f["past"]]

    def active_freeze(self) -> dict[str, Any] | None:
        return next((f for f in self.freezes() if f["active"]), None)


# --------------------------------------------------------------------------- #
# the operations the API exposes
# --------------------------------------------------------------------------- #


def _guard_freeze(store: MigrationStore) -> None:
    freeze = store.active_freeze()
    if freeze:
        reason = f" ({freeze['reason']})" if freeze["reason"] else ""
        raise FrozenError(
            f"Record entry is frozen until {freeze['ends_at']}{reason}. "
            "An approver can lift the freeze."
        )


def create_record(
    settings: Settings, store: MigrationStore, user: User, values: dict[str, Any]
) -> dict[str, Any]:
    if not user.is_developer and not user.is_approver:
        raise MigrationError("Raising a request needs the developer role.", status=403)
    _guard_freeze(store)

    cleaned: dict[str, str] = {}
    for key in REQUEST_KEYS:
        if key in values:
            cleaned[key] = _clean(BY_KEY[key], values[key])
    for spec in FIELDS:
        if spec.default and not cleaned.get(spec.key):
            cleaned[spec.key] = spec.default

    rejected = [k for k in values if k not in REQUEST_KEYS and k in BY_KEY]
    if rejected:
        labels = ", ".join(BY_KEY[k].label for k in rejected)
        raise MigrationError(f"These are set later in the workflow, not when raising: {labels}.")

    problems = validate(cleaned, settings.migrations)
    if problems:
        raise MigrationError(
            "; ".join(f"{BY_KEY[k].label}: {v}" for k, v in problems.items()),
            fields=problems,
        )

    return store.insert(cleaned, user)


def apply_changes(
    settings: Settings, store: MigrationStore, user: User, sl_no: int, changes: dict[str, Any]
) -> dict[str, Any]:
    """Update one record, enforcing role, stage and freeze before anything is written."""
    row = store.get(sl_no)
    if row is None:
        raise MigrationError(f"No record with SL# {sl_no}.", status=404)
    _guard_freeze(store)

    cleaned: dict[str, str] = {}
    for key, value in changes.items():
        spec = BY_KEY.get(key)
        if spec is None:
            raise MigrationError(f"Unknown field “{key}”.")
        allowed, why, kind = can_write(key, user, row)
        if not allowed:
            # 409 for "the workflow is not there yet", 403 for "not yours to set".
            raise MigrationError(
                f"{spec.label}: {why}",
                status=409 if kind in ("stage", "inactive") else 403,
                fields={key: why},
            )
        cleaned[key] = _clean(spec, value)

    merged = {**{f.key: row.get(f.key, "") for f in FIELDS}, **cleaned}
    problems = validate(merged, settings.migrations)
    # Only report problems on fields this edit actually touched, plus any
    # conditional requirement this edit has just triggered.
    relevant = {
        k: v for k, v in problems.items()
        if k in cleaned or (BY_KEY[k].required_if and BY_KEY[k].required_if[0] in cleaned)
    }
    if relevant:
        raise MigrationError(
            "; ".join(f"{BY_KEY[k].label}: {v}" for k, v in relevant.items()),
            fields=relevant,
        )

    cleaned.update(_auto_fields(row, cleaned, user))
    return store.update(sl_no, cleaned, user)


def _auto_fields(row: dict[str, Any], changes: dict[str, str], user: User) -> dict[str, str]:
    """The fields the workflow fills in by itself as a result of this edit."""
    auto: dict[str, str] = {}
    now = str(time.time())

    if "approved_by" in changes:
        was = (row.get("approved_by") or "").strip()
        now_set = changes["approved_by"].strip()
        if now_set and not was:
            auto["date_approved"] = now
        elif not now_set and was:
            # Withdrawing an approval must not leave its date behind.
            auto["date_approved"] = ""

    if changes.get("executed_in_qa") == YES and row.get("executed_in_qa") != YES:
        auto["qa_migrated_by"] = user.email
    if changes.get("executed_in_qa") == NO and row.get("executed_in_qa") == YES:
        auto["qa_migrated_by"] = ""

    if changes.get("executed_in_prod") == YES and row.get("executed_in_prod") != YES:
        auto["prod_migration_date"] = now
        auto["prod_migrated_by"] = user.email
    if changes.get("executed_in_prod") == NO and row.get("executed_in_prod") == YES:
        auto["prod_migration_date"] = ""
        auto["prod_migrated_by"] = ""

    return auto


def field_spec() -> list[dict[str, Any]]:
    """The form contract, for the UI to render from."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "kind": f.kind,
            "stage": f.stage,
            "roles": list(f.roles),
            "options": f.options,
            "required": f.required,
            "required_if": list(f.required_if) if f.required_if else None,
            "amber_when": f.amber_when,
            "depends_on": f.depends_on,
            "default": f.default,
            "help": f.help,
        }
        for f in FIELDS
    ]
