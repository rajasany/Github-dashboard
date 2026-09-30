"""Tests for migration requests: identity, field-level roles, the workflow gates,
freeze windows, validation and the spreadsheet round trip.

These are the rules the UI cannot be trusted to keep, so they are asserted against
the server-side functions directly.

Run with:  .venv/bin/python tests/test_migrations.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import migration_sheet, migrations  # noqa: E402
from app.auth import User, dev_mode_available, resolve_user, roles_for  # noqa: E402
from app.config import (  # noqa: E402
    ROLES, AuthConfig, Employee, Microservice, MigrationConfig, load_settings,
)
from app.db import Database  # noqa: E402
from app.migrations import MigrationError, MigrationStore  # noqa: E402

# Set TEST_DATABASE_URL to run this whole suite against PostgreSQL instead.
TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "").strip()
APP_TABLES = ("migration_audit", "migrations", "deleted_migrations", "freezes",
              "ref_microservice_links", "ref_microservices", "ref_values",
              "ref_employees", "ref_roles")


_SHARED_PG: Database | None = None


def database_for(root: Path, name: str) -> Database:
    """A clean database for one test.

    SQLite gets a fresh file. PostgreSQL has one namespace, so the app's tables
    are dropped first — each test still starts from nothing — and one pool is
    shared across the suite rather than opened and abandoned per test.
    """
    if not TEST_DB_URL:
        return Database(f"sqlite:///{root / name}")

    global _SHARED_PG
    if _SHARED_PG is None:
        # This suite drops and recreates the app's tables. Pointing it at the
        # database the app is configured to use would wipe real records, so
        # that is refused outright rather than warned about.
        from app.config import load_settings

        live = load_settings().database_url.strip()
        if live and Database(live).kind != "sqlite" and live == TEST_DB_URL:
            raise SystemExit(
                "TEST_DATABASE_URL is the same database the app is configured to use.\n"
                "This suite drops its tables. Point it at a throwaway database, e.g.\n"
                "  createdb repo_dashboard_test"
            )
        _SHARED_PG = Database(TEST_DB_URL)
    with _SHARED_PG.connect() as conn:
        for table in APP_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    return _SHARED_PG


def store_for(root: Path, name: str) -> MigrationStore:
    return MigrationStore(database_for(root, name))

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def refused(name: str, fn, *, contains: str = "") -> None:
    """Assert a call is rejected, and optionally that it says why."""
    from app.reference import ReferenceError

    try:
        fn()
    except (MigrationError, ReferenceError, PermissionError) as exc:
        if contains and contains.casefold() not in str(exc).casefold():
            check(name, f"rejected but said {str(exc)!r}", f"a message mentioning {contains!r}")
        else:
            check(name, "rejected", "rejected")
        return
    check(name, "allowed", "rejected")


def build_settings():
    settings = load_settings()
    settings.migrations = MigrationConfig(
        enabled=True,
        releases=["R2026.09", "R2026.10"],
        migration_paths=["SIT to QA", "QA to PROD"],
        microservices=[
            Microservice("payments", ["demo/payments"], ["A. Kumar"]),
            Microservice("cart", ["demo/cart", "demo/cart-ui"], ["R. Iyer", "S. Rao"]),
        ],
        change_requestors=["Business Ops", "Release Mgmt"],
        employees=[
            Employee("E1001", "Jane Doe", "dev@example.com"),
            Employee("E1002", "John Roe", "lead@example.com"),
            Employee("E1003", "Amy Poe"),
        ],
        approvers=["lead@example.com", "head@example.com"],
    )
    settings.auth = AuthConfig(
        header="X-Forwarded-Email",
        trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
        roles={
            "developer": ["*@example.com"],
            "approver": ["lead@example.com", "head@example.com"],
            "devops": ["ops@example.com"],
        },
    )
    return settings


DEV = User("dev@example.com", frozenset({"developer"}))
DEV2 = User("other@example.com", frozenset({"developer"}))
APPROVER = User("lead@example.com", frozenset({"developer", "approver"}))
DEVOPS = User("ops@example.com", frozenset({"devops"}))
ADMIN_USER = User("boss@elsewhere.com", frozenset({"admin"}))

GOOD_REQUEST = {
    "release": "R2026.09",
    "migration_path": "SIT to QA",
    "microservice": "payments",
    "repo_name": "demo/payments",
    "track_lead": "A. Kumar",
    "change_requestor": "E1001 - Jane Doe",
    "merge_type": "Cherry Pick",
    "reason": "Defect fix",
    "change_description": "Rounding correction",
    "code_image_change": "Yes",
    "commit_hash": "a1b2c3d",
}


# --------------------------------------------------------------------------- #


def test_identity() -> None:
    print("=== identity and roles ===")
    settings = build_settings()

    check("an exact address gets its role", "approver" in roles_for("lead@example.com", settings), True)
    check("a glob gets the developer role", "developer" in roles_for("someone@example.com", settings), True)
    check("case is ignored", "approver" in roles_for("LEAD@Example.com", settings), True)
    check("an outsider gets nothing", roles_for("nobody@elsewhere.com", settings), frozenset())
    check("a blank address gets nothing", roles_for("", settings), frozenset())
    check("devops is not implied by the domain glob",
          "devops" in roles_for("someone@example.com", settings), False)

    header = {"X-Forwarded-Email": "lead@example.com"}

    user = resolve_user(header, "127.0.0.1", settings)
    check("the proxy header is honoured from a trusted peer", user.email, "lead@example.com")
    check("and carries the roles", sorted(user.roles), ["approver", "developer"])
    check("and is not flagged insecure", user.insecure, False)

    user = resolve_user(header, "10.1.2.3", settings)
    check("a CIDR entry matches", user.email, "lead@example.com")

    # The whole point of the allowlist.
    user = resolve_user(header, "203.0.113.9", settings)
    check("the same header from an untrusted peer is ignored", user.email, "")
    check("and that user holds no roles", user.roles, frozenset())

    user = resolve_user(header, None, settings)
    check("an unknown peer is not trusted", user.email, "")

    settings.auth.trusted_proxies = ["not-an-ip"]
    check("an unparseable allowlist entry does not widen it",
          resolve_user(header, "127.0.0.1", settings).email, "")

    settings.auth.trusted_proxies = []
    user = resolve_user(header, "203.0.113.9", settings)
    check("with no allowlist the header is taken", user.email, "lead@example.com")
    check("but the request is flagged insecure", user.insecure, True)

    settings.auth.dev_user = "dev@example.com"
    check("dev_user is ignored while dev mode is off",
          resolve_user({}, "127.0.0.1", settings).signed_in, False)

    settings.auth.dev_user = ""
    check("with no header and no dev mode, nobody is signed in",
          resolve_user({}, "127.0.0.1", settings).signed_in, False)


def test_dev_mode() -> None:
    """The escape hatch for testing without a proxy, and the guards on it."""
    print("\n=== dev mode ===")
    settings = build_settings()
    settings.auth.trusted_proxies = []
    settings.auth.dev_user = "dev@example.com"

    allowed, why = dev_mode_available("127.0.0.1", settings)
    check("it is off unless asked for", allowed, False)
    check("and says so", "off" in why, True)

    settings.auth.dev_mode = True
    check("switched on, loopback is allowed", dev_mode_available("127.0.0.1", settings)[0], True)
    check("so is IPv6 loopback", dev_mode_available("::1", settings)[0], True)

    allowed, why = dev_mode_available("10.1.2.3", settings)
    check("but not the network", allowed, False)
    check("and it names the setting that would widen it", "dev_allow_remote" in why, True)

    settings.auth.dev_allow_remote = True
    check("which does widen it", dev_mode_available("10.1.2.3", settings)[0], True)
    settings.auth.dev_allow_remote = False

    # Configuring SSO must rule dev mode out: dev mode resolves first, so a real
    # sign-in would otherwise be answered as dev_user and SSO would look broken.
    from app.config import SsoConfig

    settings.auth.sso = SsoConfig(enabled=True, provider="google", client_id="x",
                                  redirect_url="https://app/auth/callback")
    allowed, why = dev_mode_available("127.0.0.1", settings)
    check("configured SSO turns dev mode off", allowed, False)
    check("and says to sign in instead", "sign in" in why, True)
    settings.auth.sso = SsoConfig()
    check("with SSO unconfigured it is back",
          dev_mode_available("127.0.0.1", settings)[0], True)

    # The guard that matters: a proxy on the same host is also loopback, so a
    # configured allowlist has to rule dev mode out entirely.
    settings.auth.trusted_proxies = ["127.0.0.1"]
    allowed, why = dev_mode_available("127.0.0.1", settings)
    check("a configured proxy allowlist disables dev mode", allowed, False)
    check("even from loopback, and it says why", "trusted_proxies" in why, True)
    check("the SSO header still works there",
          resolve_user({"X-Forwarded-Email": "lead@example.com"}, "127.0.0.1", settings).email,
          "lead@example.com")
    settings.auth.trusted_proxies = []

    # Choosing who to be.
    user = resolve_user({}, "127.0.0.1", settings)
    check("with nothing chosen it falls back to dev_user", user.email, "dev@example.com")
    check("and is marked dev mode", user.dev_mode, True)
    check("and insecure", user.insecure, True)
    check("with the roles that address really has", sorted(user.roles), ["developer"])

    user = resolve_user({}, "127.0.0.1", settings, {"dev_user": "lead@example.com"})
    check("a cookie chooses the identity", user.email, "lead@example.com")
    check("and brings that address's roles", sorted(user.roles), ["approver", "developer"])

    user = resolve_user({"X-Dev-User": "ops@example.com"}, "127.0.0.1", settings,
                        {"dev_user": "lead@example.com"})
    check("a header beats the cookie", user.email, "ops@example.com")
    # The domain glob grants developer too — roles accumulate across patterns.
    check("with its own roles", sorted(user.roles), ["developer", "devops"])

    user = resolve_user({"X-Dev-User": "nobody@elsewhere.com"}, "127.0.0.1", settings)
    check("an unmapped address gets no roles", user.roles, frozenset())
    check("but is still identified", user.email, "nobody@elsewhere.com")

    settings.auth.dev_user = ""
    check("with no default and no choice, nobody is signed in",
          resolve_user({}, "127.0.0.1", settings).signed_in, False)

    # Dev mode takes precedence where it applies — that is the point of it.
    settings.auth.dev_user = "dev@example.com"
    check("dev mode wins over a stray SSO header",
          resolve_user({"X-Forwarded-Email": "lead@example.com"}, "127.0.0.1", settings).email,
          "dev@example.com")


def test_config_parsing() -> None:
    """Blank and odd YAML entries must not become values."""
    print("\n=== reading config.yaml ===")
    from app.config import _parse_auth, _parse_migrations

    # A list item written as a bare "-" parses as None, and str(None) is the
    # non-empty string "None" — which would become a role pattern nobody spots.
    auth = _parse_auth({"roles": {"approver": [None], "developer": ["", "  "], "devops": None}})
    check("a blank list item is dropped, not turned into \"None\"", auth.roles["approver"], [])
    check("so are empty strings", auth.roles["developer"], [])
    check("and a null section", auth.roles["devops"], [])
    check("every role is still present as a key", sorted(auth.roles), sorted(ROLES))
    check("an all-blank roles map counts as unconfigured", auth.configured, False)
    check("a real entry still lands",
          _parse_auth({"roles": {"approver": ["a@b.com"]}}).roles["approver"], ["a@b.com"])
    check("a bare string is accepted for a list",
          _parse_auth({"roles": {"approver": "a@b.com"}}).roles["approver"], ["a@b.com"])
    check("blank proxies are dropped too",
          _parse_auth({"trusted_proxies": [None, "", "127.0.0.1"]}).trusted_proxies, ["127.0.0.1"])

    cfg = _parse_migrations({
        "releases": [None, "R1", "  ", "R2"],
        "microservices": [{"name": "svc", "repos": [None, "a/b"], "track_leads": [None]}],
    })
    check("blank releases are dropped", cfg.releases, ["R1", "R2"])
    check("blank repos are dropped", cfg.microservices[0].repos, ["a/b"])
    check("a service with no lead gets an empty list", cfg.microservices[0].track_leads, [])


def test_validation() -> None:
    print("\n=== validation ===")
    settings = build_settings()
    cfg = settings.migrations

    check("a good request validates", migrations.validate(dict(GOOD_REQUEST), cfg), {})

    missing = migrations.validate({"release": "R2026.09"}, cfg)
    check("required fields are reported", missing.get("microservice"), "Required.")
    check("optional free text is not", "reason" in missing, False)

    bad = migrations.validate({**GOOD_REQUEST, "release": "R1999.01"}, cfg)
    check("a value outside the list is rejected", "release" in bad, True)

    # The dependent lists are the point of the microservice selection.
    bad = migrations.validate({**GOOD_REQUEST, "repo_name": "demo/cart"}, cfg)
    check("a repo from another microservice is rejected", "repo_name" in bad, True)
    bad = migrations.validate({**GOOD_REQUEST, "track_lead": "R. Iyer"}, cfg)
    check("a track lead from another microservice is rejected", "track_lead" in bad, True)
    ok = migrations.validate(
        {**GOOD_REQUEST, "microservice": "cart", "repo_name": "demo/cart-ui", "track_lead": "S. Rao"}, cfg
    )
    check("the same values are fine under their own microservice", ok, {})

    no_hash = migrations.validate({**GOOD_REQUEST, "commit_hash": ""}, cfg)
    check("commit hash is required when code changed", "commit_hash" in no_hash, True)
    not_needed = migrations.validate({**GOOD_REQUEST, "code_image_change": "No", "commit_hash": ""}, cfg)
    check("and not required when it did not", "commit_hash" in not_needed, False)

    ddl = migrations.validate({**GOOD_REQUEST, "ddl_dml": "Yes"}, cfg)
    check("db script path is required when DDL/DML is Yes", "db_script_path" in ddl, True)

    check("a malformed commit hash is caught",
          "commit_hash" in migrations.validate({**GOOD_REQUEST, "commit_hash": "zzz"}, cfg), True)
    check("a bad yes/no is caught",
          "env_change" in migrations.validate({**GOOD_REQUEST, "env_change": "maybe"}, cfg), True)
    check("a bad date is caught",
          "qa_date_planned" in migrations.validate({**GOOD_REQUEST, "qa_date_planned": "31/12/26"}, cfg), True)

    # Spreadsheets hand over all sorts of spellings for a boolean.
    field = migrations.BY_KEY["env_change"]
    check("'y' becomes Yes", migrations._clean(field, "y"), "Yes")
    check("'TRUE' becomes Yes", migrations._clean(field, "TRUE"), "Yes")
    check("0 becomes No", migrations._clean(field, 0), "No")
    check("an unknown word is left alone to be rejected", migrations._clean(field, "later"), "later")


def test_workflow(root: Path) -> None:
    print("\n=== roles and the workflow ===")
    settings = build_settings()
    store = store_for(root, "migrations.sqlite3")

    record = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    sl = record["sl_no"]
    check("a request is created", sl >= 1, True)
    check("created_by is taken from the login", record["created_by"], "dev@example.com")
    check("date created is stamped", bool(record["date_created"]), True)
    check("unspecified risk flags default to No", record["env_change"], "No")
    check("it starts awaiting approval", record["status"], "submitted")
    check("SL# is the sequence", record["sl_no"], sl)

    refused("a developer cannot approve",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"ready_for_qa": "Yes"}),
            contains="approver role")
    refused("devops cannot approve",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"ready_for_qa": "Yes"}),
            contains="approver role")
    refused("nor can anyone type in who approved it",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl,
                                             {"approved_by": "someone@example.com"}),
            contains="automatically")
    refused("nobody can write an auto field",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"date_approved": "2026-01-01"}),
            contains="automatically")
    refused("a developer cannot record a QA migration",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"executed_in_qa": "Yes"}),
            contains="devops role")
    refused("devops cannot record QA before approval",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_qa": "Yes"}),
            contains="not approved for qa")
    refused("another developer cannot edit someone else's request",
            lambda: migrations.apply_changes(settings, store, DEV2, sl, {"reason": "mine now"}),
            contains="raised the request")

    edited = migrations.apply_changes(settings, store, DEV, sl, {"reason": "Defect fix, revised"})
    check("the author can edit their own request", edited["reason"], "Defect fix, revised")

    approved = migrations.apply_changes(
        settings, store, APPROVER, sl, {"ready_for_qa": "Yes", "qa_date_planned": "2026-10-01"}
    )
    check("an approver marks it ready for QA", approved["ready_for_qa"], "Yes")
    check("which fills in who approved it", approved["approved_by"], "lead@example.com")
    check("and when", bool(approved["date_approved"]), True)
    check("the planned QA migration date is kept", approved["qa_date_planned"], "2026-10-01")
    check("status moves to approved", approved["status"], "approved")

    refused("the request is locked once approved",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"reason": "changed my mind"}),
            contains="Ready for QA back to No")
    refused("devops cannot jump straight to prod",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_prod": "Yes"}),
            contains="ready for prod")
    refused("an approver cannot mark ready for prod before QA",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_prod": "Yes"}),
            contains="migrated to QA")

    qa = migrations.apply_changes(
        settings, store, DEVOPS, sl, {"executed_in_qa": "Yes", "qa_migration_remarks": "Clean run"}
    )
    check("devops can record the QA migration", qa["executed_in_qa"], "Yes")
    check("QA migrated by is filled from the login", qa["qa_migrated_by"], "ops@example.com")
    check("and the QA migration date is stamped", bool(qa["qa_migration_date"]), True)
    check("status moves to in QA", qa["status"], "in_qa")

    refused("devops cannot declare it ready for prod",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"ready_for_prod": "Yes"}),
            contains="approver role")

    ready = migrations.apply_changes(
        settings, store, APPROVER, sl,
        {"ready_for_prod": "Yes", "prod_date_planned": "2026-11-03"},
    )
    check("the approver marks it ready after testing", ready["status"], "ready_for_prod")
    check("the prod approval records who", ready["prod_approved_by"], "lead@example.com")
    check("and when", bool(ready["prod_date_approved"]), True)
    check("and keeps the planned prod date", ready["prod_date_planned"], "2026-11-03")

    prod = migrations.apply_changes(
        settings, store, DEVOPS, sl, {"executed_in_prod": "Yes", "prod_migration_remarks": "Done 02:10"}
    )
    check("devops can record the prod migration", prod["executed_in_prod"], "Yes")
    check("the prod date is stamped", bool(prod["prod_migration_date"]), True)
    check("and who did it is recorded", prod["prod_migrated_by"], "ops@example.com")
    check("status reaches in prod", prod["status"], "in_prod")

    # Undoing an approval after the migration has happened would reopen the
    # request for editing — of a change already running in production.
    refused("the QA approval cannot be withdrawn once QA has run",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_qa": "No"}),
            contains="already been migrated to QA")
    refused("nor the prod approval once prod has run",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_prod": "No"}),
            contains="already been migrated to production")

    # On a record where nothing has happened yet, withdrawing is fine.
    undo = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    migrations.apply_changes(settings, store, APPROVER, undo["sl_no"], {"ready_for_qa": "Yes"})
    withdrawn = migrations.apply_changes(settings, store, APPROVER, undo["sl_no"], {"ready_for_qa": "No"})
    check("withdrawing the QA approval clears who approved it", withdrawn["approved_by"], "")
    check("and its date", withdrawn["date_approved"], "")
    check("and reopens the request", withdrawn["status"], "submitted")
    check("so it can be edited again",
          migrations.apply_changes(settings, store, DEV, undo["sl_no"],
                                   {"reason": "revised"})["reason"], "revised")

    # Conditional requirements apply to edits too, not just creation.
    fresh = migrations.create_record(
        settings, store, DEV, {**GOOD_REQUEST, "code_image_change": "No", "commit_hash": ""}
    )
    refused("turning on a code change demands the commit hash",
            lambda: migrations.apply_changes(
                settings, store, DEV, fresh["sl_no"], {"code_image_change": "Yes"}),
            contains="Commit Hash")
    both = migrations.apply_changes(
        settings, store, DEV, fresh["sl_no"], {"code_image_change": "Yes", "commit_hash": "deadbee"}
    )
    check("supplying both together is accepted", both["commit_hash"], "deadbee")
    check("the amber flags are listed for the UI", both["amber"], ["code_image_change"])

    refused("workflow fields cannot be smuggled in at creation",
            lambda: migrations.create_record(
                settings, store, DEV, {**GOOD_REQUEST, "approved_by": "lead@example.com"}),
            contains="set later")
    refused("someone with no role cannot raise a request",
            lambda: migrations.create_record(settings, store, User("x@y.com"), dict(GOOD_REQUEST)),
            contains="developer role")

    # What the UI is allowed to render as editable.
    check("a developer sees only their request fields",
          migrations.editable_fields(DEV, store.get(fresh["sl_no"])),
          list(migrations.REQUEST_KEYS))
    check("an approver sees the QA approval fields",
          migrations.editable_fields(APPROVER, store.get(fresh["sl_no"])),
          list(migrations.REQUEST_KEYS) + ["ready_for_qa", "qa_date_planned"])
    check("devops sees nothing on an unapproved record",
          migrations.editable_fields(DEVOPS, store.get(fresh["sl_no"])), [])

    entries = store.audit(sl)
    check("the audit trail records the approval",
          any(e["field"] == "approved_by" and e["new_value"] == "lead@example.com" for e in entries), True)
    check("and the switch that caused it",
          any(e["field"] == "ready_for_qa" and e["new_value"] == "Yes" for e in entries), True)
    check("it records who did it",
          any(e["field"] == "executed_in_prod" and e["who"] == "ops@example.com" for e in entries), True)
    check("it keeps the previous value",
          any(e["field"] == "reason" and e["old_value"] == "Defect fix" for e in entries), True)
    # Timestamps are stored as epoch strings; the trail must not show them raw.
    stamps = [e["new_value"] for e in entries
              if e["field"] in migrations.TIMESTAMP_FIELDS and e["new_value"]]
    check("timestamps read as times, not epochs", all(s.endswith("Z") for s in stamps), True)
    check("and there is at least one to check", len(stamps) >= 1, True)


def test_freeze(root: Path) -> None:
    print("\n=== freeze windows ===")
    settings = build_settings()
    store = store_for(root, "freeze.sqlite3")
    record = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    sl = record["sl_no"]

    now = time.time()
    past = store.add_freeze(now - 7200, now - 3600, "last night", APPROVER)
    check("a finished freeze is not active", past["active"], False)
    check("and does not block anything", store.active_freeze(), None)

    future = store.add_freeze(now + 3600, now + 7200, "tonight", APPROVER)
    check("a future freeze is not yet active", future["active"], False)
    check("and does not block anything either", store.active_freeze(), None)

    live = store.add_freeze(now - 60, now + 3600, "release window", APPROVER)
    check("a current freeze is active", live["active"], True)
    check("it names who set it", live["created_by"], "lead@example.com")

    refused("no new requests during a freeze",
            lambda: migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST)),
            contains="frozen")
    refused("no edits during a freeze",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"reason": "nope"}),
            contains="frozen")
    refused("not even an approver can edit during a freeze",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"approved_by": "lead@example.com"}),
            contains="frozen")
    refused("nor devops",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_qa": "Yes"}),
            contains="frozen")

    # An approver must always be able to get out of a freeze they set.
    check("the freeze can still be lifted", store.delete_freeze(live["id"]), True)
    check("after which entry works again",
          migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["status"], "submitted")

    refused("a freeze must end after it starts",
            lambda: store.add_freeze(now + 7200, now + 3600, "backwards", APPROVER),
            contains="end after")

    check("past windows are hidden by default",
          any(f["id"] == past["id"] for f in store.freezes()), False)
    check("but can be asked for",
          any(f["id"] == past["id"] for f in store.freezes(include_past=True)), True)


def test_sheet() -> None:
    print("\n=== the spreadsheet ===")
    settings = build_settings()
    cfg = settings.migrations

    sheet = migration_sheet.parse("t.xlsx", migration_sheet.build_template(cfg))
    check("the template reads back", len(sheet["rows"]), 1)
    check("every request column is recognised",
          sorted(sheet["columns"]), sorted(migrations.REQUEST_KEYS))
    check("its example row is valid", migration_sheet.check_rows(sheet["rows"], cfg)[0]["ok"], True)

    from openpyxl import Workbook

    book = Workbook()
    page = book.active
    page.append(["Migration requests for the October release"])  # a title above the header
    page.append([])
    page.append(["Rel#", "Migration Path", "Micro Service Name", "Repo Name",
                 "Track Lead Name", "Change Requestor", "Merge Type",
                 "Code & Image Change?", "Commit Hash"])
    page.append(["R2026.09", "SIT to QA", "payments", "demo/payments", "A. Kumar",
                 "E1001", "Cherry Pick", "Yes", "a1b2c3d"])
    page.append(["R2026.09", "SIT to QA", "cart", "demo/cart", "R. Iyer",
                 "E1001", "Cherry Pick", "yes", ""])   # missing the required hash
    page.append(["R1999.01", "SIT to QA", "payments", "demo/payments", "A. Kumar",
                 "E1001", "Cherry Pick", "No", ""])    # release not in the list
    page.append(["R2026.09", "SIT to QA", "payments", "demo/cart", "A. Kumar",
                 "E1001", "Cherry Pick", "No", ""])    # repo of another microservice
    page.append([None, None, None])                    # blank row, skipped

    import io

    buffer = io.BytesIO()
    book.save(buffer)

    sheet = migration_sheet.parse("messy.xlsx", buffer.getvalue())
    check("the header is found below a title row", sheet["header_row"], 3)
    check("the blank row is skipped", len(sheet["rows"]), 4)

    results = migration_sheet.check_rows(sheet["rows"], cfg)
    check("every row is reported", len(results), 4)
    check("the good row passes", results[0]["ok"], True)
    check("a lower-case yes is understood", results[1]["values"]["code_image_change"], "Yes")
    check("the missing hash is caught", "Commit Hash" in results[1]["problems"], True)
    check("the unknown release is caught", "Rel#" in results[2]["problems"], True)
    check("the mismatched repo is caught", "Repo Name" in results[3]["problems"], True)
    check("row numbers point back at the sheet", [r["row"] for r in results], [4, 5, 6, 7])

    refused("an oversized sheet is rejected",
            lambda: migration_sheet.check_rows(
                sheet["rows"] * (migration_sheet.MAX_UPLOAD_ROWS), cfg),
            contains="more than")


def test_permissions() -> None:
    """What the UI is told it may do — decided here, not in JavaScript."""
    print("\n=== permissions ===")
    from app.migrations import permissions

    anon = permissions(User(""))
    check("anonymous cannot raise", anon["can_raise"], False)
    check("and is told why", anon["why_not_raise"], "You are not signed in.")

    stranger = permissions(User("x@y.com", frozenset()))
    check("an unmapped address cannot raise", stranger["can_raise"], False)
    check("and is pointed at config.yaml", "config.yaml" in stranger["why_not_raise"], True)

    check("a developer can raise", permissions(DEV)["can_raise"], True)
    check("but cannot retire", permissions(DEV)["can_retire"], False)
    check("an approver can do both",
          (permissions(APPROVER)["can_raise"], permissions(APPROVER)["can_retire"]), (True, True))

    devops = permissions(DEVOPS)
    check("devops alone cannot raise", devops["can_raise"], False)
    check("and the message names what they hold", "devops" in devops["why_not_raise"], True)

    # The bug this replaced: the UI looked for "developer" in the role list, so
    # an admin — who holds none of the three — saw the button greyed out.
    admin = permissions(User("boss@y.com", frozenset({"admin"})))
    check("an admin can raise without holding developer", admin["can_raise"], True)
    check("and can retire", admin["can_retire"], True)
    check("and freeze", admin["can_freeze"], True)
    check("with nothing to explain", admin["why_not_raise"], "")


def test_merge_type(root: Path) -> None:
    """Cherry-pick or full merge, and where a full merge is refused."""
    print("\n=== merge type ===")
    settings = build_settings()
    cfg = settings.migrations
    cfg.microservices = [
        Microservice("payments", ["demo/payments"], ["A. Kumar"], allow_full_merge=False),
        Microservice("cart", ["demo/cart"], ["R. Iyer"], allow_full_merge=True),
    ]

    check("both are offered", migrations.option_lists(cfg)["merge_types"],
          ["Cherry Pick", "Full Merge"])
    check("it must be answered",
          migrations.validate({}, cfg).get("merge_type"), "Required.")
    check("and only with one of the two",
          "merge_type" in migrations.validate({"merge_type": "Rebase"}, cfg, partial=True), True)

    base = {"microservice": "payments", "repo_name": "demo/payments"}
    check("a cherry-pick is accepted",
          "merge_type" in migrations.validate({**base, "merge_type": "Cherry Pick"}, cfg, partial=True),
          False)

    refused = migrations.validate({**base, "merge_type": "Full Merge"}, cfg, partial=True)
    check("a full merge is refused where it is not allowed", "merge_type" in refused, True)
    check("naming the repository", "demo/payments" in refused["merge_type"], True)
    check("and saying what to do instead",
          "cherry-pick the change" in refused["merge_type"], True)

    allowed = {"microservice": "cart", "repo_name": "demo/cart", "merge_type": "Full Merge"}
    check("but permitted where the service allows it",
          "merge_type" in migrations.validate(allowed, cfg, partial=True), False)

    unknown = migrations.validate({"microservice": "", "merge_type": "Full Merge"}, cfg, partial=True)
    check("with no microservice chosen it is still refused", "merge_type" in unknown, True)

    # End to end, so the rule holds on the way in and not only in validate().
    store = store_for(root, "merge.sqlite3")
    settings.migrations = cfg
    row = migrations.create_record(settings, store, DEV, {
        **GOOD_REQUEST, "microservice": "cart", "repo_name": "demo/cart",
        "track_lead": "R. Iyer", "merge_type": "Full Merge",
    })
    check("a full merge is stored where allowed", row["merge_type"], "Full Merge")

    refused_call = lambda: migrations.create_record(settings, store, DEV, {
        **GOOD_REQUEST, "merge_type": "Full Merge",
    })
    try:
        refused_call()
        check("and refused where not", "accepted", "refused")
    except MigrationError as exc:
        check("and refused where not", "refused", "refused")
        check("with the field named for the form", list(exc.fields), ["merge_type"])

    # No default: how the change reaches the branch is not something to be
    # answered by leaving it alone.
    check("it has no default to fall back on", migrations.BY_KEY["merge_type"].default, "")
    omitted = dict(GOOD_REQUEST)
    omitted.pop("merge_type")
    try:
        migrations.create_record(settings, store, DEV, omitted)
        check("omitting it is refused", "accepted", "refused")
    except MigrationError as exc:
        check("omitting it is refused", "refused", "refused")
        check("naming it as the gap", list(exc.fields), ["merge_type"])

    # A spreadsheet is held to the same rule: a sheet written before this field
    # existed no longer imports silently, it names the column it is missing.
    sheet_row = {"_row": 2, "release": "R2026.09", "migration_path": "SIT to QA",
                 "microservice": "payments", "repo_name": "demo/payments",
                 "track_lead": "A. Kumar", "change_requestor": "E1001"}
    result = migration_sheet.check_rows([sheet_row], cfg)[0]
    check("a sheet row without it is invalid", result["ok"], False)
    check("and says which column to add", "Merge Type" in result["problems"], True)
    check("but passes once it carries one",
          migration_sheet.check_rows([{**sheet_row, "merge_type": "Cherry Pick"}], cfg)[0]["ok"],
          True)

    # Turning the permission off later must refuse new requests, not old ones.
    cfg.microservices = [Microservice("cart", ["demo/cart"], ["R. Iyer"], allow_full_merge=False)]
    check("revoking it refuses the next request",
          "merge_type" in migrations.validate(allowed, cfg, partial=True), True)
    check("while the record already raised is untouched",
          store.get(row["sl_no"])["merge_type"], "Full Merge")


def test_employees(root: Path) -> None:
    """Change Requestor comes from the employee list, and defaults to you."""
    print("\n=== employees as change requestors ===")
    from app.reference import ReferenceStore

    settings = build_settings()
    cfg = settings.migrations

    check("the requestor list is the employees",
          migrations.option_lists(cfg)["change_requestors"],
          ["E1001 - Jane Doe", "E1002 - John Roe", "E1003 - Amy Poe"])
    check("a signed-in address maps to its employee",
          cfg.employee_for("dev@example.com").label, "E1001 - Jane Doe")
    check("case does not matter", cfg.employee_for("DEV@Example.com").number, "E1001")
    check("an unmapped address maps to nobody", cfg.employee_for("ops@example.com"), None)
    check("nor does a blank one", cfg.employee_for(""), None)

    # A sheet may hold the number alone, or a different dash.
    check("a number resolves to the label",
          migrations.clean_requestor("E1002", cfg), "E1002 - John Roe")
    check("so does a name", migrations.clean_requestor("amy poe", cfg), "E1003 - Amy Poe")
    check("an em dash is tolerated",
          migrations.clean_requestor("E1001 — Jane Doe", cfg), "E1001 - Jane Doe")
    check("the canonical label passes through",
          migrations.clean_requestor("E1001 - Jane Doe", cfg), "E1001 - Jane Doe")
    check("something unknown is left alone, to be rejected",
          migrations.clean_requestor("Somebody", cfg), "Somebody")
    check("and is rejected",
          "change_requestor" in migrations.validate({"change_requestor": "Somebody"}, cfg, partial=True),
          True)

    # A record stores the resolved label whatever was typed.
    store = store_for(root, "employees.sqlite3")
    row = migrations.create_record(settings, store, DEV,
                                   {**GOOD_REQUEST, "change_requestor": "E1003"})
    check("a record stores the canonical label", row["change_requestor"], "E1003 - Amy Poe")

    # With no employees configured the old plain list still works.
    plain = build_settings()
    plain.migrations.employees = []
    check("without employees the old list is used",
          migrations.option_lists(plain.migrations)["change_requestors"],
          ["Business Ops", "Release Mgmt"])
    check("and a value from it is left as typed",
          migrations.clean_requestor("Business Ops", plain.migrations), "Business Ops")

    # The reference store keeps them.
    ref = ReferenceStore(database_for(root, "empref.sqlite3"))
    ref.seed(cfg)
    check("employees are seeded", [e.number for e in ref.snapshot(fresh=True).employees],
          ["E1001", "E1002", "E1003"])
    live = ref.save_employee("E1004", "Kay Singh", "kay@example.com")
    check("one can be added", live.employees[-1].label, "E1004 - Kay Singh")
    live = ref.save_employee("E1004", "Kay Singh-Patel", "kay@example.com")
    check("and edited in place", live.employees[-1].name, "Kay Singh-Patel")
    check("without duplicating", len([e for e in live.employees if e.number == "E1004"]), 1)
    live = ref.save_employee("E9004", "Kay Singh-Patel", "kay@example.com", rename_from="E1004")
    check("renumbering keeps one entry", [e.number for e in live.employees][-1], "E9004")
    refused("renumbering onto an existing number is refused",
            lambda: ref.save_employee("E1001", "x", "", rename_from="E9004"),
            contains="already an employee")
    live = ref.delete_employee("E9004")
    check("and one can be removed", "E9004" in [e.number for e in live.employees], False)
    refused("an employee needs a number",
            lambda: ref.save_employee("  ", "No Number", ""), contains="needs a number")
    refused("deleting an unknown one is refused",
            lambda: ref.delete_employee("E0000"), contains="No employee")


def test_spec() -> None:
    print("\n=== the field contract ===")
    spec = migrations.field_spec()
    check("every field is published", len(spec), len(migrations.FIELDS))
    check("every field is accounted for", len(migrations.FIELDS), 34)
    check("the stage headings cover every stage",
          sorted(migrations.STAGE_TITLES), sorted({f.stage for f in migrations.FIELDS}))
    check("QA approval is named as such", migrations.STAGE_TITLES["approval"], "QA Approval")
    check("and prod approval too", migrations.STAGE_TITLES["prod_gate"], "Prod Approval")
    check("approved by is filled in, not chosen", migrations.BY_KEY["approved_by"].kind, "auto")
    check("so is the prod one", migrations.BY_KEY["prod_approved_by"].kind, "auto")
    check("the planned QA date says what it plans",
          migrations.BY_KEY["qa_date_planned"].label, "QA Migration Date Planned")
    check("and the prod one is enterable",
          migrations.BY_KEY["prod_date_planned"].roles, ("approver",))
    check("four fields carry the amber flag", len(migrations.AMBER_KEYS), 4)
    check("the amber fields are the risk ones", sorted(migrations.AMBER_KEYS),
          ["code_image_change", "ddl_dml", "env_change", "env_secret"])
    check("auto fields name no role",
          all(not f["roles"] for f in spec if f["kind"] == "auto"), True)
    check("no field is writable by two different stages' roles",
          all(len(set(f["roles"])) == len(f["roles"]) for f in spec), True)
    check("SL# is auto", migrations.BY_KEY["sl_no"].kind, "auto")
    check("created by is auto", migrations.BY_KEY["created_by"].kind, "auto")
    check("date approved is auto", migrations.BY_KEY["date_approved"].kind, "auto")
    check("QA migrated by is auto", migrations.BY_KEY["qa_migrated_by"].kind, "auto")
    check("QA migration date is auto", migrations.BY_KEY["qa_migration_date"].kind, "auto")
    check("it comes before QA migrated by",
          [f.key for f in migrations.FIELDS].index("qa_migration_date")
          < [f.key for f in migrations.FIELDS].index("qa_migrated_by"), True)
    check("prod date approved is auto", migrations.BY_KEY["prod_date_approved"].kind, "auto")
    check("prod migration date is auto", migrations.BY_KEY["prod_migration_date"].kind, "auto")


async def test_http(root: Path) -> None:
    """The same rules, asserted over HTTP — headers, status codes and all."""
    print("\n=== over HTTP ===")
    import httpx

    from app import main as M

    template = build_settings()
    M.settings.migrations = template.migrations
    M.settings.auth = template.auth
    M.settings.database_url = database_for(root, "http.sqlite3").url

    def client(email: str | None, peer: str = "127.0.0.1") -> httpx.AsyncClient:
        headers = {"X-Forwarded-Email": email} if email else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=M.app, client=(peer, 50000)),
            base_url="http://t",
            headers=headers,
            timeout=30,
        )

    async with M.lifespan(M.app):
        # --- identity over the wire ---
        async with client(None) as c:
            r = await c.get("/api/migrations/records")
            check("no header means 401", r.status_code, 401)

        async with client("lead@example.com", peer="203.0.113.9") as c:
            r = await c.get("/api/migrations/records")
            check("a header from an untrusted peer is refused", r.status_code, 401)

        async with client("dev@example.com") as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("meta names the caller", meta["user"]["email"], "dev@example.com")
            check("and their roles", meta["user"]["roles"], ["developer"])
            check("and publishes every field", len(meta["fields"]), len(migrations.FIELDS))
            check("and the dependent service map", len(meta["services"]), 2)

            r = await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))
            check("a developer can raise a request", r.status_code, 201)
            sl = r.json()["sl_no"]
            check("the response says what they may edit next",
                  sorted(r.json()["editable"]), sorted(migrations.REQUEST_KEYS))

            r = await c.patch(f"/api/migrations/records/{sl}", json={"ready_for_qa": "Yes"})
            check("but cannot approve it", r.status_code, 403)
            check("and is told which role that needs",
                  "approver" in r.json()["detail"], True)
            check("naming the field, so the form can mark it",
                  list(r.json()["fields"]), ["ready_for_qa"])

            # A 28-field form has to say which fields are wrong, not just that
            # something is — the client marks the controls from this.
            r = await c.post("/api/migrations/records", json={"microservice": "payments"})
            check("an incomplete request is refused", r.status_code, 422)
            check("with every missing field named",
                  sorted(r.json()["fields"]),
                  ["change_requestor", "merge_type", "migration_path", "release",
                   "repo_name", "track_lead"])
            check("each with a reason", set(r.json()["fields"].values()), {"Required."})
            check("and a sentence for the summary line",
                  "Rel#: Required." in r.json()["detail"], True)

            r = await c.post("/api/migrations/records",
                             json={**GOOD_REQUEST, "commit_hash": ""})
            check("a conditional requirement names its own field",
                  list(r.json()["fields"]), ["commit_hash"])
            check("and explains the condition",
                  "Code & Image Change" in r.json()["fields"]["commit_hash"], True)

            r = await c.post("/api/migrations/freezes",
                             json={"starts_epoch": 0, "ends_epoch": 1})
            check("nor set a freeze", r.status_code, 403)

        async with client("ops@example.com") as c:
            r = await c.patch(f"/api/migrations/records/{sl}", json={"executed_in_qa": "Yes"})
            # 409, not 403: devops *may* set this field, just not yet.
            check("devops cannot record QA before approval", r.status_code, 409)

        async with client("lead@example.com") as c:
            r = await c.patch(f"/api/migrations/records/{sl}",
                              json={"ready_for_qa": "Yes", "qa_date_planned": "2026-10-02"})
            check("an approver can approve", r.status_code, 200)
            check("who approved it is filled in", r.json()["approved_by"], "lead@example.com")
            check("the approval is dated automatically", bool(r.json()["date_approved"]), True)

        async with client("ops@example.com") as c:
            r = await c.patch(f"/api/migrations/records/{sl}", json={"executed_in_qa": "Yes"})
            check("devops can then record QA", r.status_code, 200)
            check("and is recorded as having done it", r.json()["qa_migrated_by"], "ops@example.com")
            check("with the migration dated", bool(r.json()["qa_migration_date"]), True)

            # The headings the form renders come from the server.
            stages = {s["key"]: s["label"] for s in (await c.get("/api/migrations/meta")).json()["stages"]}
            check("the approval stage is named QA Approval", stages["approval"], "QA Approval")
            check("and the prod gate is named Prod Approval", stages["prod_gate"], "Prod Approval")

        # --- the spreadsheet round trip ---
        async with client("dev@example.com") as c:
            r = await c.get("/api/migrations/template")
            check("the template downloads", r.status_code, 200)
            check("as a workbook", r.headers["content-type"].startswith(
                "application/vnd.openxmlformats"), True)

            import io

            from openpyxl import Workbook

            book = Workbook()
            page = book.active
            page.append(["Rel#", "Migration Path", "Micro Service Name", "Repo Name",
                         "Track Lead Name", "Change Requestor", "Merge Type"])
            # The requestor column carries just the employee number, as a real
            # sheet usually would; the server resolves it to the full label.
            page.append(["R2026.09", "SIT to QA", "cart", "demo/cart", "R. Iyer", "E1001",
                         "Cherry Pick"])
            page.append(["R2026.09", "SIT to QA", "cart", "demo/payments", "R. Iyer", "E1001",
                         "Cherry Pick"])
            buffer = io.BytesIO()
            book.save(buffer)
            upload = {"file": ("plan.xlsx", buffer.getvalue(),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

            r = await c.post("/api/migrations/upload", files=upload, data={"commit": "false"})
            body = r.json()
            check("a dry run reports without writing", body["committed"], False)
            check("it counts the good rows", body["valid"], 1)
            check("and the bad ones", body["invalid"], 1)
            check("nothing was created", body["created"], [])

            r = await c.post("/api/migrations/upload", files=upload, data={"commit": "true"})
            created = r.json()["created"]
            check("committing imports only the valid rows", len(created), 1)
            check("and the employee number became the full label",
                  created[0]["change_requestor"], "E1001 - Jane Doe")

            # Sorting and the date range, as the tab sends them.
            r = await c.get("/api/migrations/records?sort=date_created&dir=asc")
            check("the response echoes what it sorted by", r.json()["sort"],
                  {"field": "date_created", "dir": "asc"})
            check("and advertises the sortable columns",
                  "date_created" in r.json()["sortable"], True)

            r = await c.get("/api/migrations/records?sort=made_up")
            check("an unknown sort column is corrected, not rejected",
                  r.json()["sort"]["field"], "sl_no")

            everything = (await c.get("/api/migrations/records")).json()["count"]
            r = await c.get("/api/migrations/records?created_from=2099-01-01")
            check("a from-date in the future matches nothing", r.json()["count"], 0)
            r = await c.get("/api/migrations/records?created_from=2000-01-01")
            check("one in the past matches everything", r.json()["count"], everything)

            # A bare end date means the whole of that day, not 00:00 on it.
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            r = await c.get(f"/api/migrations/records?created_to={today}")
            check("a bare end date includes that whole day", r.json()["count"], everything)

            r = await c.get("/api/migrations/records?created_from=not-a-date")
            check("an unparseable date is refused clearly", r.status_code, 422)
            check("naming the offending value", "not-a-date" in r.json()["detail"], True)

            r = await c.get("/api/migrations/export.csv")
            check("the register exports as CSV", r.status_code, 200)
            # Every field, plus Status, Active, Archived and Archived By.
            check("with a header naming every field",
                  r.text.splitlines()[0].count(",") + 1, len(migrations.FIELDS) + 4)

            # The export must agree with the screen, or the two disagree silently.
            query = "sort=date_created&dir=asc&created_from=2000-01-01"
            screen = (await c.get(f"/api/migrations/records?{query}")).json()["rows"]
            export = (await c.get(f"/api/migrations/export.csv?{query}")).text.splitlines()[1:]
            check("the CSV honours the same filters", len(export), len(screen))
            check("and the same order",
                  [line.split(",")[0] for line in export],
                  [str(row["sl_no"]) for row in screen])

        # --- freezing ---
        async with client("lead@example.com") as c:
            now = time.time()
            r = await c.post("/api/migrations/freezes",
                             json={"starts_epoch": now - 60, "ends_epoch": now + 600,
                                   "reason": "release window"})
            check("an approver can freeze entry", r.status_code, 201)
            freeze_id = r.json()["id"]

        async with client("dev@example.com") as c:
            r = await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))
            check("creation is refused while frozen", r.status_code, 423)
            r = await c.post("/api/migrations/upload", files=upload, data={"commit": "true"})
            check("so is an import", r.status_code, 423)

        async with client("ops@example.com") as c:
            r = await c.patch(f"/api/migrations/records/{sl}", json={"qa_migration_remarks": "x"})
            check("and so is an edit, whatever the role", r.status_code, 423)
            r = await c.post("/api/migrations/records/execute",
                             json={"sl_nos": [sl], "stage": "qa"})
            # The role is checked before the freeze: telling someone it is frozen
            # when they could never do it anyway would be misleading.
            check("bulk execution is refused for its own role's reason", r.status_code, 423)

        async with client("lead@example.com") as c:
            # Retiring a record is a change to it, so the freeze must cover it —
            # deleting already was refused, this was the gap.
            r = await c.post(f"/api/migrations/records/{sl}/active", json={"active": False})
            check("deactivating is refused while frozen", r.status_code, 423)
            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": [sl], "stage": "qa"})
            check("so is bulk approval", r.status_code, 423)

            # The register still reads, and reports that nothing may be changed.
            listing = (await c.get("/api/migrations/records")).json()
            check("the register is still readable", listing["count"] > 0, True)
            check("but no field is offered as editable",
                  all(not r_["editable"] for r_ in listing["rows"]), True)
            meta = (await c.get("/api/migrations/meta")).json()
            check("and meta says it is frozen", meta["frozen"], True)
            check("while the lists remain readable",
                  (await c.get("/api/migrations/lists")).status_code, 200)

        async with client("dev@example.com") as c:
            r = await c.delete(f"/api/migrations/freezes/{freeze_id}")
            check("a developer cannot lift the freeze", r.status_code, 403)

        async with client("lead@example.com") as c:
            r = await c.delete(f"/api/migrations/freezes/{freeze_id}")
            check("an approver can", r.status_code, 200)

        async with client("dev@example.com") as c:
            r = await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))
            check("after which entry works again", r.status_code, 201)

        # --- someone with no role at all ---
        async with client("stranger@elsewhere.com") as c:
            r = await c.get("/api/migrations/records")
            check("an unmapped address can still read", r.status_code, 200)
            r = await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))
            check("but cannot write", r.status_code, 403)

        # --- bulk approval, over the wire ---
        async with client("dev@example.com") as c:
            batch = [(await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))).json()["sl_no"]
                     for _ in range(3)]
            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": batch, "stage": "qa"})
            check("a developer cannot bulk approve", r.status_code, 403)

        async with client("lead@example.com") as c:
            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": batch, "stage": "qa"})
            check("an approver can", r.status_code, 200)
            check("and all three move", len(r.json()["approved"]), 3)
            check("with nothing skipped", r.json()["skipped"], [])

            # "approve" must not be read as a record number by the {sl_no} route.
            check("the route is not mistaken for a record id",
                  (await c.get("/api/migrations/records/approve/audit")).status_code, 422)

            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": batch, "stage": "prod"})
            check("prod approval waits for the QA migration", len(r.json()["approved"]), 0)
            check("and says so for each", len(r.json()["skipped"]), 3)

            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": ["x"], "stage": "qa"})
            check("a non-numeric selection is refused", r.status_code, 422)
            r = await c.post("/api/migrations/records/approve", json={"sl_nos": [], "stage": "qa"})
            check("an empty selection is refused", r.status_code, 422)

            meta = (await c.get("/api/migrations/meta")).json()
            check("an approver is offered the bulk controls",
                  meta["permissions"]["can_approve"], True)

        async with client("ops@example.com") as c:
            for sl in batch:
                await c.patch(f"/api/migrations/records/{sl}", json={"executed_in_qa": "Yes"})
        async with client("lead@example.com") as c:
            r = await c.post("/api/migrations/records/approve",
                             json={"sl_nos": batch, "stage": "prod"})
            check("once QA has run they can all go to prod", len(r.json()["approved"]), 3)
            check("attributed to the approver",
                  {x["prod_approved_by"] for x in r.json()["approved"]}, {"lead@example.com"})

        async with client("dev@example.com") as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("a developer is not offered them", meta["permissions"]["can_approve"], False)

        # --- archiving, over the wire ---
        async with client("dev@example.com") as c:
            keep_sl = (await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))).json()["sl_no"]
            r = await c.post("/api/migrations/records/archive", json={"sl_nos": [keep_sl]})
            check("a developer cannot archive", r.status_code, 403)
        async with client("ops@example.com") as c:
            r = await c.post("/api/migrations/records/archive", json={"sl_nos": [keep_sl]})
            check("nor devops", r.status_code, 403)

        async with client("lead@example.com") as c:
            r = await c.post("/api/migrations/records/archive", json={"sl_nos": [keep_sl]})
            check("an approver can, but only finished work", r.json()["archived"], [])
            check("and is told why", r.json()["skipped"][0]["reason"],
                  "Not migrated to production yet.")
            check("archiving is advertised in permissions",
                  (await c.get("/api/migrations/meta")).json()["permissions"]["can_archive"], True)

            await c.patch(f"/api/migrations/records/{keep_sl}", json={"ready_for_qa": "Yes"})
        async with client("ops@example.com") as c:
            await c.patch(f"/api/migrations/records/{keep_sl}", json={"executed_in_qa": "Yes"})
        async with client("lead@example.com") as c:
            await c.patch(f"/api/migrations/records/{keep_sl}", json={"ready_for_prod": "Yes"})
        async with client("ops@example.com") as c:
            await c.patch(f"/api/migrations/records/{keep_sl}", json={"executed_in_prod": "Yes"})

        async with client("lead@example.com") as c:
            before = (await c.get("/api/migrations/records")).json()["count"]
            r = await c.post("/api/migrations/records/archive", json={"sl_nos": [keep_sl]})
            check("once in production it archives", len(r.json()["archived"]), 1)
            check("the register loses it",
                  (await c.get("/api/migrations/records")).json()["count"], before - 1)
            archive = (await c.get("/api/migrations/records?archived=true")).json()
            check("and the archive gains it",
                  keep_sl in [x["sl_no"] for x in archive["rows"]], True)
            check("with nothing editable on it",
                  all(not x["editable"] for x in archive["rows"]), True)

            r = await c.patch(f"/api/migrations/records/{keep_sl}", json={"ready_for_qa": "No"})
            check("an archived record refuses edits over HTTP", r.status_code, 409)
            r = await c.delete(f"/api/migrations/records/{keep_sl}")
            check("and refuses deletion", r.status_code, 409)
            r = await c.post(f"/api/migrations/records/{keep_sl}/active", json={"active": False})
            check("and refuses deactivation", r.status_code, 409)

            csv_archived = (await c.get("/api/migrations/export.csv?archived=true")).text
            check("the archive exports too", len(csv_archived.splitlines()), 2)
            r = await c.post("/api/migrations/records/archive", json={"sl_nos": []})
            check("an empty selection is refused", r.status_code, 422)

        # --- retiring and deleting, over the wire ---
        async with client("dev@example.com") as c:
            keep = (await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))).json()["sl_no"]
            drop = (await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))).json()["sl_no"]
            listing = (await c.get("/api/migrations/records")).json()
            check("a developer is not offered the retire controls", listing["can_retire"], False)
            r = await c.post(f"/api/migrations/records/{drop}/active", json={"active": False})
            check("nor allowed to retire", r.status_code, 403)
            r = await c.delete(f"/api/migrations/records/{drop}")
            check("nor to delete", r.status_code, 403)

        async with client("ops@example.com") as c:
            r = await c.delete(f"/api/migrations/records/{drop}")
            check("devops cannot delete either", r.status_code, 403)

        async with client("lead@example.com") as c:
            listing = (await c.get("/api/migrations/records")).json()
            check("an approver is", listing["can_retire"], True)
            before = listing["count"]

            r = await c.post(f"/api/migrations/records/{drop}/active", json={"active": False})
            check("who can retire a record", r.status_code, 200)
            check("and it reports as inactive", r.json()["active"], False)
            check("it leaves the default listing",
                  (await c.get("/api/migrations/records")).json()["count"], before - 1)
            check("unless asked for",
                  (await c.get("/api/migrations/records?include_inactive=true")).json()["count"], before)

            r = await c.patch(f"/api/migrations/records/{drop}", json={"reason": "nope"})
            check("an inactive record refuses edits", r.status_code, 409)

            r = await c.post(f"/api/migrations/records/{drop}/active", json={"active": True})
            check("and can be brought back", r.json()["active"], True)

            csv_text = (await c.get("/api/migrations/export.csv")).text
            check("the CSV gains an Active column", ",Active," in csv_text.splitlines()[0], True)
            check("and an Archived one", csv_text.splitlines()[0].endswith(",Archived By"), True)

            r = await c.delete(f"/api/migrations/records/{drop}")
            check("an approver can delete", r.status_code, 200)
            check("after which it is gone",
                  (await c.get(f"/api/migrations/records/{drop}/audit")).status_code, 404)
            check("deleting it twice is a 404", (await c.delete(f"/api/migrations/records/{drop}")).status_code, 404)
            check("the other record survives",
                  any(r_["sl_no"] == keep for r_ in (await c.get("/api/migrations/records")).json()["rows"]),
                  True)

            # A freeze stops entry, and erasing a record is the most final entry.
            now = time.time()
            fid = (await c.post("/api/migrations/freezes",
                                json={"starts_epoch": now - 60, "ends_epoch": now + 600})).json()["id"]
            check("deleting is refused while frozen",
                  (await c.delete(f"/api/migrations/records/{keep}")).status_code, 423)
            await c.delete(f"/api/migrations/freezes/{fid}")

        # An admin holds every permission without being given every role.
        # Roles now live in the database, so config.yaml is no longer the place
        # to add one — that is the whole point of the change.
        M.reference_store.set_role("admin", ["boss@elsewhere.com"])
        M.settings.auth.live_roles = M.reference_store.roles()
        async with client("boss@elsewhere.com") as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("an admin is listed as admin alone", meta["user"]["roles"], ["admin"])
            r = await c.post(f"/api/migrations/records/{keep}/active", json={"active": False})
            check("and may retire records", r.status_code, 200)
            r = await c.delete(f"/api/migrations/records/{keep}")
            check("and delete them", r.status_code, 200)


async def test_http_dev(root: Path) -> None:
    """Dev mode over HTTP: the cookie, the picker payload, and the guards."""
    print("\n=== dev mode over HTTP ===")
    import httpx

    from app import main as M

    template = build_settings()
    M.settings.migrations = template.migrations
    M.settings.auth = template.auth
    M.settings.auth.trusted_proxies = []
    M.settings.auth.dev_mode = True
    M.settings.auth.dev_user = "dev@example.com"
    M.settings.database_url = database_for(root, "dev.sqlite3").url

    def client(peer: str = "127.0.0.1", cookies=None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=M.app, client=(peer, 50000)),
            base_url="http://t",
            cookies=cookies or {},
            timeout=30,
        )

    async with M.lifespan(M.app):
        async with client() as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("dev mode is reported as on", meta["dev"]["enabled"], True)
            check("with no default chosen it uses dev_user", meta["user"]["email"], "dev@example.com")
            check("the picker lists the configured people",
                  [i["email"] for i in meta["dev"]["identities"]],
                  ["dev@example.com", "head@example.com", "lead@example.com", "ops@example.com"])
            check("each with the roles they would hold",
                  next(i["roles"] for i in meta["dev"]["identities"] if i["email"] == "ops@example.com"),
                  ["developer", "devops"])
            check("and the cookie name to set", meta["dev"]["cookie"], "dev_user")

            r = await c.post("/api/migrations/records", json=dict(GOOD_REQUEST))
            check("the chosen identity can raise a request", r.status_code, 201)
            sl = r.json()["sl_no"]
            check("recorded against them", r.json()["created_by"], "dev@example.com")

            r = await c.patch(f"/api/migrations/records/{sl}", json={"ready_for_qa": "Yes"})
            check("and cannot approve, being only a developer", r.status_code, 403)

        # Switching identity is a cookie away — no restart, no config edit.
        async with client(cookies={"dev_user": "lead@example.com"}) as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("the cookie switches who you are", meta["user"]["email"], "lead@example.com")
            check("with the approver role", "approver" in meta["user"]["roles"], True)
            r = await c.patch(f"/api/migrations/records/{sl}", json={"ready_for_qa": "Yes"})
            check("who can then approve", r.status_code, 200)
            check("and is recorded as the approver", r.json()["approved_by"], "lead@example.com")

            # A download is a plain navigation, so it must ride on the cookie too.
            r = await c.get("/api/migrations/export.csv")
            check("a download is authorised by the same cookie", r.status_code, 200)

        async with client(cookies={"dev_user": "ops@example.com"}) as c:
            r = await c.patch(f"/api/migrations/records/{sl}", json={"executed_in_qa": "Yes"})
            check("and devops can record the QA migration", r.status_code, 200)
            check("attributed to them", r.json()["qa_migrated_by"], "ops@example.com")

        # The guards, over the wire.
        async with client(peer="10.1.2.3") as c:
            r = await c.get("/api/migrations/records")
            check("dev mode is refused from off-box", r.status_code, 401)
            # meta is behind the gate now, so the explanation has to come from
            # somewhere a locked-out caller can still reach.
            who = (await c.get("/api/auth/whoami")).json()
            check("whoami explains why", "loopback" in who["dev_mode"]["why_not"], True)
            check("while still admitting it is configured", who["dev_mode"]["configured"], True)
            check("and says the gate is on", who["enforced"], True)

        M.settings.auth.dev_allow_remote = True
        async with client(peer="10.1.2.3") as c:
            check("unless explicitly widened",
                  (await c.get("/api/migrations/records")).status_code, 200)
        M.settings.auth.dev_allow_remote = False

        M.settings.auth.trusted_proxies = ["127.0.0.1"]
        async with client() as c:
            # With dev mode off and no header, the gate refuses before meta —
            # so whoami, which stays public, is what answers.
            check("the register is closed", (await c.get("/api/migrations/meta")).status_code, 401)
            who = (await c.get("/api/auth/whoami")).json()
            check("a configured proxy switches dev mode off", who["dev_mode"]["available"], False)
            check("and nobody is signed in without a header", who["signed_in"], False)


def test_sorting_and_dates(root: Path) -> None:
    """The created date, the orderings it enables, and the range filter."""
    print("\n=== sorting and the created date ===")
    settings = build_settings()
    store = store_for(root, "sorting.sqlite3")

    from datetime import datetime as dt, timezone as tz

    def at(iso: str) -> float:
        return dt.fromisoformat(iso).replace(tzinfo=tz.utc).timestamp()

    made = []
    for service, repo, lead, when in [
        ("payments", "demo/payments", "A. Kumar", "2026-09-01T10:00:00"),
        ("cart", "demo/cart", "R. Iyer", "2026-09-05T10:00:00"),
        ("cart", "demo/cart-ui", "S. Rao", "2026-09-03T10:00:00"),
    ]:
        row = migrations.create_record(settings, store, DEV, {
            **GOOD_REQUEST, "microservice": service, "repo_name": repo, "track_lead": lead,
        })
        made.append((row["sl_no"], when))

    # create_record stamps "now"; rewrite the stamps so the order is knowable.
    def restamp(pairs):
        with store.db.connect() as conn:
            for sl, value in pairs:
                conn.execute("UPDATE migrations SET date_created = ? WHERE sl_no = ?", (value, sl))

    restamp([(sl, str(at(when))) for sl, when in made])

    check("the epoch is published for sorting",
          isinstance(store.get(1)["date_created_epoch"], float), True)
    check("alongside the rendered date", store.get(1)["date_created"], "2026-09-01T10:00:00Z")

    order = lambda **kw: [r["sl_no"] for r in store.list(**kw)]

    check("the default is newest SL# first", order(), [3, 2, 1])
    check("oldest created first", order(sort="date_created", direction="asc"), [1, 3, 2])
    check("newest created first", order(sort="date_created", direction="desc"), [2, 3, 1])
    check("by repo, ascending", order(sort="repo_name", direction="asc"), [2, 3, 1])
    check("by microservice, ascending", order(sort="microservice", direction="asc"), [2, 3, 1])
    check("an unknown column falls back to SL#", order(sort="nonsense"), [3, 2, 1])

    # Status ordering should follow the workflow, not the alphabet: "Approved"
    # would otherwise sort before "Awaiting approval".
    migrations.apply_changes(settings, store, APPROVER, 1, {"ready_for_qa": "Yes"})
    check("status sorts along the workflow",
          [r["status"] for r in store.list(sort="status", direction="asc")],
          ["submitted", "submitted", "approved"])

    # A row with no date must not float to the top of a newest-first sort.
    restamp([(2, "")])
    check("an undated row sorts as oldest", order(sort="date_created", direction="desc")[-1], 2)
    check("and is not dropped", len(order(sort="date_created")), 3)

    restamp([(2, str(at("2026-09-05T10:00:00")))])

    check("a from-date excludes earlier rows",
          order(created_from=at("2026-09-03T00:00:00")), [3, 2])
    check("a to-date excludes later rows",
          order(created_to=at("2026-09-03T23:59:59")), [3, 1])
    check("both together bracket the range",
          order(created_from=at("2026-09-02T00:00:00"), created_to=at("2026-09-04T00:00:00")), [3])
    check("the bounds are inclusive",
          order(created_from=at("2026-09-01T10:00:00"), created_to=at("2026-09-01T10:00:00")), [1])
    check("a range matching nothing returns nothing",
          order(created_from=at("2027-01-01T00:00:00")), [])
    check("filtering and sorting compose",
          order(created_from=at("2026-09-02T00:00:00"), sort="date_created", direction="asc"), [3, 2])


def test_retire_and_delete(root: Path) -> None:
    """Retiring a record, restoring it, and removing it outright."""
    print("\n=== inactive records and deletion ===")
    settings = build_settings()
    settings.auth.roles["admin"] = ["boss@example.com"]
    store = store_for(root, "retire.sqlite3")

    ADMIN = User("boss@example.com", frozenset({"admin"}))

    check("admin satisfies every role check", ADMIN.has_any("devops"), True)
    check("without pretending to hold them", sorted(ADMIN.roles), ["admin"])
    check("a developer still does not", DEV.has_any("approver"), False)

    first = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    second = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    check("new records are active", first["active"], True)
    check("and both are listed", len(store.list()), 2)

    retired = store.set_active(first["sl_no"], False, APPROVER)
    check("an approver can retire one", retired["active"], False)
    check("it drops out of the default listing",
          [r["sl_no"] for r in store.list()], [second["sl_no"]])
    check("but can be asked for",
          len(store.list(include_inactive=True)), 2)
    check("the retiring is recorded",
          [(e["label"], e["old_value"], e["new_value"]) for e in store.audit(first["sl_no"])
           if e["field"] == "active"],
          [("Record", "active", "inactive")])

    refused("an inactive record cannot be edited",
            lambda: migrations.apply_changes(settings, store, APPROVER, first["sl_no"],
                                             {"ready_for_qa": "Yes"}),
            contains="inactive")
    check("and offers nothing to edit",
          migrations.editable_fields(APPROVER, store.get(first["sl_no"])), [])

    restored = store.set_active(first["sl_no"], True, ADMIN)
    check("an admin can bring it back", restored["active"], True)
    check("after which it is editable again",
          len(migrations.editable_fields(APPROVER, store.get(first["sl_no"]))) > 0, True)
    check("and listed again", len(store.list()), 2)

    # Retiring twice should not pile up identical audit entries.
    store.set_active(first["sl_no"], False, APPROVER)
    before = len(store.audit(first["sl_no"]))
    store.set_active(first["sl_no"], False, APPROVER)
    check("retiring an already-retired record records nothing new",
          len(store.audit(first["sl_no"])), before)

    check("deleting removes it", store.delete(first["sl_no"], APPROVER), True)
    check("it is gone from the store", store.get(first["sl_no"]), None)
    check("its history goes with it", store.audit(first["sl_no"]), [])
    check("even when inactive rows are asked for",
          [r["sl_no"] for r in store.list(include_inactive=True)], [second["sl_no"]])
    check("deleting something absent says so", store.delete(9999, APPROVER), False)
    check("the other record is untouched", store.get(second["sl_no"])["active"], True)

    # A database written before `active` existed must still open. Simulate one
    # by dropping the column from a fresh file.
    import sqlite3

    legacy = root / "legacy.sqlite3"
    keep = [f.key for f in migrations.FIELDS if f.key != "sl_no"]
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE migrations (sl_no INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{k} TEXT NOT NULL DEFAULT ''" for k in keep)
        + ", updated_at REAL NOT NULL DEFAULT 0)"
    )
    conn.execute(
        f"INSERT INTO migrations (microservice, date_created) VALUES ('payments', '1788000000')"
    )
    conn.commit()
    conn.close()

    reopened = MigrationStore(Database(f"sqlite:///{legacy}"))
    check("an older database gains the column on open",
          "active" in {r[1] for r in sqlite3.connect(legacy).execute("PRAGMA table_info(migrations)")},
          True)
    check("and its existing rows count as active", reopened.get(1)["active"], True)
    check("so they are still listed", len(reopened.list()), 1)


def test_microservice_file(root: Path) -> None:
    """Reading the microservice → repo → track lead map from a file."""
    print("\n=== the microservice file ===")
    from app.config import MigrationConfig, load_microservices

    path = root / "services.csv"
    path.write_text(
        "Micro Service Name,Repo Name,Track Lead Name\n"
        "payments,demo/payments,A. Kumar\n"
        "payments,demo/payments-ui,A. Kumar\n"
        "cart,demo/cart,R. Iyer\n"
        "cart,demo/cart,S. Rao\n"
    )
    services, problem = load_microservices(path)
    check("a clean file reads without complaint", problem, "")
    check("one entry per microservice", [s.name for s in services], ["payments", "cart"])
    check("repos accumulate across rows", services[0].repos, ["demo/payments", "demo/payments-ui"])
    check("so do track leads", services[1].track_leads, ["R. Iyer", "S. Rao"])
    check("a repeated repo is not listed twice", services[1].repos, ["demo/cart"])

    # Real files are not tidy.
    messy = root / "messy.csv"
    messy.write_text(
        "Microservice map — October\n"
        "\n"
        "MS,Repository,Lead\n"
        "  payments  ,demo/payments ; demo/payments-api,  A. Kumar \n"
        ",,\n"
        "Cart,demo/cart,R. Iyer\n"
    )
    services, problem = load_microservices(messy)
    check("a title row above the header is tolerated", problem, "")
    check("and shorter column names are recognised", [s.name for s in services], ["payments", "Cart"])
    check("whitespace is trimmed", services[0].track_leads, ["A. Kumar"])
    check("several repos in one cell are split", services[0].repos, ["demo/payments", "demo/payments-api"])
    check("a blank row is skipped", len(services), 2)

    # Case differences should not create a second service.
    dupes = root / "dupes.csv"
    dupes.write_text("MS,Repo,Lead\npayments,demo/a,A\nPayments,demo/b,B\n")
    services, _ = load_microservices(dupes)
    check("case does not split a service in two", len(services), 1)
    check("its rows are merged", services[0].repos, ["demo/a", "demo/b"])
    check("keeping the first spelling seen", services[0].name, "payments")

    # Failures are reported, never raised.
    services, problem = load_microservices(root / "absent.csv")
    check("a missing file is reported", bool(problem), True)
    check("and names the path", "absent.csv" in problem, True)
    check("returning no services rather than throwing", services, [])

    headerless = root / "headerless.csv"
    headerless.write_text("just,some,values\n1,2,3\n")
    services, problem = load_microservices(headerless)
    check("a file with no usable header is reported", bool(problem), True)
    check("and says what column it wanted", "Micro Service Name" in problem, True)

    empty = root / "empty.csv"
    empty.write_text("Micro Service Name,Repo Name,Track Lead Name\n")
    services, problem = load_microservices(empty)
    check("a header with no rows is reported", "no row had a microservice" in problem, True)
    check("and no services come back problem-free", (services, bool(problem)), ([], True))

    # xlsx works too, since it is the same reader as the upload tab.
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Micro Service Name", "Repo Name", "Track Lead Name"])
    book.active.append(["billing", "demo/billing", "T. Nair"])
    xlsx = root / "services.xlsx"
    book.save(xlsx)
    services, problem = load_microservices(xlsx)
    check("a workbook is accepted as well", [s.name for s in services], ["billing"])

    # --- hot reload -----------------------------------------------------------
    cfg = MigrationConfig(
        enabled=True,
        microservices_file=path,
        inline_microservices=[Microservice("fallback", ["demo/fb"], ["Nobody"])],
    )
    cfg.refresh()
    check("the file wins over the inline list", [s.name for s in cfg.microservices],
          ["payments", "cart"])

    path.write_text("MS,Repo,Lead\nledger,demo/ledger,P. Das\n")
    cfg.refresh()
    check("editing the file is picked up without a restart",
          [s.name for s in cfg.microservices], ["ledger"])
    check("and no error is left behind", cfg.services_error, "")

    path.unlink()
    cfg.refresh()
    check("if it disappears the problem is reported", bool(cfg.services_error), True)
    check("and the inline list is used rather than nothing",
          [s.name for s in cfg.microservices], ["fallback"])

    # With no file configured, refresh must leave the inline list alone.
    plain = MigrationConfig(enabled=True, microservices=[Microservice("only", [], [])])
    plain.refresh()
    check("no file means nothing changes", [s.name for s in plain.microservices], ["only"])
    check("and no error is invented", plain.services_error, "")


def test_bulk_approval(root: Path) -> None:
    """Approving many records at once, through the same rules as approving one."""
    print("\n=== bulk approval ===")
    settings = build_settings()
    store = store_for(root, "bulk.sqlite3")

    made = [migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
            for _ in range(5)]
    a, b, c, d, e = made

    refused("a developer cannot bulk approve",
            lambda: migrations.approve_many(settings, store, DEV, [a, b], "qa"),
            contains="approver or admin role")
    refused("nor devops",
            lambda: migrations.approve_many(settings, store, DEVOPS, [a, b], "qa"),
            contains="approver or admin role")
    refused("an unknown stage is rejected",
            lambda: migrations.approve_many(settings, store, APPROVER, [a], "staging"),
            contains="not something that can be approved")
    refused("an empty selection is rejected",
            lambda: migrations.approve_many(settings, store, APPROVER, [], "qa"),
            contains="No records were selected")
    refused("more than the cap is rejected",
            lambda: migrations.approve_many(settings, store, APPROVER,
                                            list(range(migrations.MAX_BULK + 2)), "qa"),
            contains="more than")

    # The planned migration date is part of the approval, not a follow-up edit.
    refused("a malformed planned date fails the batch, not each record",
            lambda: migrations.approve_many(settings, store, APPROVER, [a, b], "qa", "02/10/2026"),
            contains="YYYY-MM-DD")

    out = migrations.approve_many(settings, store, APPROVER, [a, b, c], "qa", "2026-10-02")
    check("the planned date is echoed back", out["planned"], "2026-10-02")
    check("and named", out["planned_label"], "QA Migration Date Planned")
    check("it is set on every approved record",
          {r["qa_date_planned"] for r in out["approved"]}, {"2026-10-02"})
    check("three are approved at once", len(out["approved"]), 3)
    check("none is skipped", out["skipped"], [])
    check("the count of what was asked is kept", out["requested"], 3)
    check("each is now approved", {r["status"] for r in out["approved"]}, {"approved"})
    check("and attributed to the approver",
          {r["approved_by"] for r in out["approved"]}, {"lead@example.com"})
    check("with a date on each", all(r["date_approved"] for r in out["approved"]), True)
    check("the others are untouched", store.get(d)["status"], "submitted")

    # The same call again should do nothing, and say so rather than silently.
    again = migrations.approve_many(settings, store, APPROVER, [a, b], "qa")
    check("re-approving does nothing", again["approved"], [])
    check("and reports why", {s["reason"] for s in again["skipped"]}, {"Already approved."})

    # Prod approval needs the QA migration to have happened.
    out = migrations.approve_many(settings, store, APPROVER, [a, b], "prod")
    check("prod approval is refused before QA has run", out["approved"], [])
    check("naming the gate", {s["reason"] for s in out["skipped"]}, {"Not migrated to QA yet."})

    migrations.apply_changes(settings, store, DEVOPS, a, {"executed_in_qa": "Yes"})
    migrations.apply_changes(settings, store, DEVOPS, b, {"executed_in_qa": "Yes"})

    # `a` has a planned date of its own, `b` has none. A blank bulk date must
    # leave both as they are rather than clearing or inventing one.
    migrations.apply_changes(settings, store, APPROVER, a, {"prod_date_planned": "2026-11-01"})

    out = migrations.approve_many(settings, store, APPROVER, [a, b, c], "prod")
    check("the ones that are ready go through",
          sorted(r["sl_no"] for r in out["approved"]), sorted([a, b]))
    check("a partial run does not stop at the first refusal", len(out["skipped"]), 1)
    check("and the one that is not is named",
          out["skipped"][0]["reason"], "Not migrated to QA yet.")
    check("prod approval is attributed too",
          {r["prod_approved_by"] for r in out["approved"]}, {"lead@example.com"})
    check("a blank date leaves an existing one alone",
          next(r["prod_date_planned"] for r in out["approved"] if r["sl_no"] == a), "2026-11-01")
    check("and does not invent one for the others",
          next(r["prod_date_planned"] for r in out["approved"] if r["sl_no"] == b), "")

    # Mixed input: a missing record and an inactive one.
    store.set_active(d, False, APPROVER)
    out = migrations.approve_many(settings, store, APPROVER, [d, 9999, e], "qa")
    check("an inactive record is skipped", 
          next(s["reason"] for s in out["skipped"] if s["sl_no"] == d), "Inactive.")
    check("a missing one is skipped", 
          next(s["reason"] for s in out["skipped"] if s["sl_no"] == 9999), "No such record.")
    check("while the good one still goes through", [r["sl_no"] for r in out["approved"]], [e])

    # Duplicates in the selection must not double-apply.
    f = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
    out = migrations.approve_many(settings, store, APPROVER, [f, f, f], "qa")
    check("a repeated selection is collapsed", out["requested"], 1)
    check("and approved once", len(out["approved"]), 1)
    check("leaving one audit entry for the switch",
          len([x for x in store.audit(f) if x["field"] == "ready_for_qa"]), 1)

    # A freeze stops the lot, not one at a time.
    now = time.time()
    freeze = store.add_freeze(now - 60, now + 600, "window", APPROVER)
    refused("a freeze stops the whole batch",
            lambda: migrations.approve_many(settings, store, APPROVER, [e], "prod"),
            contains="frozen")
    store.delete_freeze(freeze["id"])

    # Admin holds none of the three roles, but satisfies every check.
    admin = User("boss@example.com", frozenset({"admin"}))
    h = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
    out = migrations.approve_many(settings, store, admin, [h], "qa")
    check("an admin can bulk approve as well", [r["sl_no"] for r in out["approved"]], [h])
    check("and is recorded as the approver", out["approved"][0]["approved_by"], "boss@example.com")


def test_bulk_execution(root: Path) -> None:
    """DevOps recording several migrations at once — the same machinery."""
    print("\n=== bulk execution ===")
    settings = build_settings()
    settings.auth.roles["admin"] = ["boss@elsewhere.com"]
    store = store_for(root, "exec.sqlite3")
    ADMIN = User("boss@elsewhere.com", frozenset({"admin"}))

    made = [migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
            for _ in range(4)]
    a, b, c, d = made

    # A stage that has not been reached is a skip with a reason, not an error:
    # that is what makes a mixed batch usable.
    early = migrations.execute_many(settings, store, DEVOPS, [a], "qa")
    check("devops cannot record QA before approval", early["moved"], [])
    check("and is told why", early["skipped"][0]["reason"], "Not approved for QA yet.")
    refused("an approver cannot record a migration",
            lambda: migrations.execute_many(settings, store, APPROVER, [a], "qa"),
            contains="devops or admin role")
    refused("nor a developer",
            lambda: migrations.execute_many(settings, store, DEV, [a], "qa"),
            contains="devops or admin role")

    migrations.approve_many(settings, store, APPROVER, [a, b, c], "qa", "2026-10-02")

    out = migrations.execute_many(settings, store, DEVOPS, [a, b, c, d], "qa", "Window 02:00-03:00")
    check("the approved ones are recorded", sorted(r["sl_no"] for r in out["moved"]), sorted([a, b, c]))
    check("the unapproved one is left", [s["sl_no"] for s in out["skipped"]], [d])
    check("with the reason", out["skipped"][0]["reason"], "Not approved for QA yet.")
    check("each is attributed to the operator",
          {r["qa_migrated_by"] for r in out["moved"]}, {"ops@example.com"})
    check("each is dated", all(r["qa_migration_date"] for r in out["moved"]), True)
    check("the shared remarks land on all of them",
          {r["qa_migration_remarks"] for r in out["moved"]}, {"Window 02:00-03:00"})
    check("and are echoed back", out["remarks"], "Window 02:00-03:00")
    check("the verb describes what happened", out["verb"], "record as migrated to QA")

    again = migrations.execute_many(settings, store, DEVOPS, [a], "qa")
    check("recording it twice does nothing", again["moved"], [])
    check("and says it is already done", again["skipped"][0]["reason"], "Already recorded.")

    # Prod execution waits for the prod approval, not just the QA migration.
    refused_out = migrations.execute_many(settings, store, DEVOPS, [a, b], "prod")
    check("prod execution waits for the prod approval", refused_out["moved"], [])
    check("naming the gate",
          {s["reason"] for s in refused_out["skipped"]}, {"Not marked ready for prod yet."})

    migrations.approve_many(settings, store, APPROVER, [a, b], "prod", "2026-11-14")
    out = migrations.execute_many(settings, store, DEVOPS, [a, b], "prod", "Prod window")
    check("then both go through", len(out["moved"]), 2)
    check("dated and attributed",
          all(r["prod_migration_date"] and r["prod_migrated_by"] == "ops@example.com"
              for r in out["moved"]), True)
    check("with the shared remarks",
          {r["prod_migration_remarks"] for r in out["moved"]}, {"Prod window"})
    check("and they reach the end of the workflow",
          {r["status"] for r in out["moved"]}, {"in_prod"})

    # Blank remarks must not wipe what was written per record.
    e = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
    migrations.approve_many(settings, store, APPROVER, [e], "qa")
    migrations.execute_many(settings, store, DEVOPS, [e], "qa", "first note")
    migrations.apply_changes(settings, store, DEVOPS, e, {"executed_in_qa": "No"})
    out = migrations.execute_many(settings, store, DEVOPS, [e], "qa")
    check("blank remarks leave an existing note alone",
          out["moved"][0]["qa_migration_remarks"], "first note")

    # An admin can do the devops actions too.
    f = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
    migrations.approve_many(settings, store, ADMIN, [f], "qa")
    out = migrations.execute_many(settings, store, ADMIN, [f], "qa")
    check("an admin can record migrations", [r["sl_no"] for r in out["moved"]], [f])
    check("attributed to them", out["moved"][0]["qa_migrated_by"], "boss@elsewhere.com")

    now = time.time()
    freeze = store.add_freeze(now - 60, now + 600, "window", APPROVER)
    refused("a freeze stops bulk execution too",
            lambda: migrations.execute_many(settings, store, DEVOPS, [d], "qa"),
            contains="frozen")
    store.delete_freeze(freeze["id"])

    check("the roles each action needs are published",
          (migrations.permissions(DEVOPS)["can_execute"],
           migrations.permissions(DEVOPS)["can_approve"]), (True, False))
    check("and the approver's are the other way round",
          (migrations.permissions(APPROVER)["can_execute"],
           migrations.permissions(APPROVER)["can_approve"]), (False, True))
    check("an admin gets both",
          (migrations.permissions(ADMIN)["can_execute"],
           migrations.permissions(ADMIN)["can_approve"]), (True, True))


def test_table_list_is_complete() -> None:
    """The suite's table list must not drift behind the schema.

    It is used to reset PostgreSQL between tests, and a table missing from it
    leaks rows into the next test — which is how this check came to exist.
    """
    print("\n=== the schema the tests reset ===")
    import tempfile as _tf

    from app.reference import ReferenceStore

    with _tf.TemporaryDirectory() as tmp:
        db = Database(f"sqlite:///{Path(tmp) / 'schema.sqlite3'}")
        MigrationStore(db)
        ReferenceStore(db)
        with db.connect() as conn:
            real = {
                r["name"]
                for r in conn.all("SELECT name FROM sqlite_master WHERE type = ?", ("table",))
                if not r["name"].startswith("sqlite_")
            }
    check("every table the app creates is in APP_TABLES",
          sorted(real - set(APP_TABLES)), [])
    check("and nothing in APP_TABLES is imaginary",
          sorted(set(APP_TABLES) - real), [])


def test_database_reachability(root: Path) -> None:
    """A database that cannot be reached must say so clearly, and quickly."""
    print("\n=== reaching the database ===")
    from app.db import Database, DatabaseError

    ok = Database(f"sqlite:///{root / 'fine.sqlite3'}")
    ok.verify()
    check("a good SQLite path verifies", True, True)

    try:
        Database("sqlite:////proc/nope/cannot-create.sqlite3").verify()
        check("an impossible SQLite path is reported", "allowed", "rejected")
    except (DatabaseError, OSError):
        check("an impossible SQLite path is reported", "rejected", "rejected")

    if TEST_DB_URL:
        Database(TEST_DB_URL).verify()
        check("a good PostgreSQL URL verifies", True, True)

        import time as _t

        bad = Database(TEST_DB_URL.rsplit("/", 1)[0] + "/definitely_not_here")
        start = _t.monotonic()
        try:
            bad.verify()
            check("a missing database is reported", "allowed", "rejected")
        except DatabaseError as exc:
            message = str(exc)
            check("a missing database is reported", "rejected", "rejected")
            check("naming the database", "definitely_not_here" in message, True)
            check("quoting what the server said", "does not exist" in message, True)
            check("and suggesting what to check", "pg_hba.conf" in message, True)
        check("without waiting on a long timeout", _t.monotonic() - start < 5, True)


def test_delete_audit(root: Path) -> None:
    """Deleting a record must not delete the fact that it existed."""
    print("\n=== deletion leaves an account ===")
    settings = build_settings()
    store = store_for(root, "delaudit.sqlite3")

    row = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    sl = row["sl_no"]
    migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_qa": "Yes"})
    check("nothing deleted yet", store.deleted(), [])

    check("it deletes", store.delete(sl, APPROVER, "raised against the wrong release"), True)
    check("and is gone from the register", store.get(sl), None)
    check("its live history is gone too", store.audit(sl), [])

    kept = store.deleted()
    check("but the deletion is recorded", len(kept), 1)
    entry = kept[0]
    check("under its own SL#", entry["sl_no"], sl)
    check("naming who removed it", entry["deleted_by"], "lead@example.com")
    check("when", entry["deleted_at"].endswith("Z"), True)
    check("and why", entry["reason"], "raised against the wrong release")
    check("the record is kept as it stood", entry["record"]["microservice"], "payments")
    check("with the status it had reached", entry["status_label"], "Approved")
    check("and its whole history",
          [h["field"] for h in entry["history"]],
          ["created", "ready_for_qa", "approved_by", "date_approved"])
    check("readably", entry["history"][1]["label"], "Ready for QA")

    check("deleting something absent does nothing",
          store.delete(9999, APPROVER), False)
    check("and records nothing", len(store.deleted()), 1)

    # A reason is not required, and its absence is visible rather than implied.
    second = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
    store.delete(second, ADMIN_USER, "")
    check("a deletion without a reason is still recorded", len(store.deleted()), 2)
    check("with the reason blank", store.deleted()[0]["reason"], "")
    check("newest first", store.deleted()[0]["sl_no"], second)


def test_db_migration(root: Path) -> None:
    """Copying the register between databases, keeping SL# and the sequence."""
    print("\n=== moving the database ===")
    import subprocess

    from app.reference import ReferenceStore

    source = Database(f"sqlite:///{root / 'from.sqlite3'}")
    store, ref = MigrationStore(source), ReferenceStore(source)
    settings = build_settings()
    ref.seed(settings.migrations)
    settings.migrations = ref.snapshot(fresh=True)
    settings.migrations.enabled = True

    for _ in range(3):
        migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    migrations.apply_changes(settings, store, APPROVER, 2, {"ready_for_qa": "Yes"})
    store.add_freeze(time.time() - 10, time.time() - 5, "past", APPROVER)

    target_url = TEST_DB_URL or f"sqlite:///{root / 'to.sqlite3'}"
    if TEST_DB_URL:
        # The shared PostgreSQL is the source's counterpart here; clear it first.
        with Database(TEST_DB_URL).connect() as conn:
            for table in APP_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    def run(*extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "tools" / "migrate_db.py"),
             "--from", source.url, "--to", target_url, *extra],
            capture_output=True, text=True,
        )

    dry = run("--dry-run")
    check("a dry run succeeds", dry.returncode, 0)
    check("and writes nothing", "nothing written" in dry.stdout, True)

    done = run()
    check("the copy succeeds", done.returncode, 0)
    check("reporting a verified destination", "destination verified" in done.stdout, True)

    target = Database(target_url)
    moved = MigrationStore(target)
    check("every record came across", len(moved.list()), 3)
    check("SL# is preserved, not renumbered",
          sorted(r["sl_no"] for r in moved.list()), [1, 2, 3])
    check("workflow state came with them",
          moved.get(2)["status"], "approved")
    check("so did who approved it", moved.get(2)["approved_by"], "lead@example.com")
    check("the audit trail came too", len(moved.audit(2)) >= 3, True)
    check("and the freeze window", len(moved.freezes(include_past=True)), 1)
    check("with the reference lists",
          [e.number for e in ReferenceStore(target).snapshot(fresh=True).employees],
          ["E1001", "E1002", "E1003"])

    # The trap this tool exists for: inserting explicit ids leaves a PostgreSQL
    # identity sequence at 1, so the next record would collide.
    settings.migrations = ReferenceStore(target).snapshot(fresh=True)
    settings.migrations.enabled = True
    fresh = migrations.create_record(settings, moved, DEV, dict(GOOD_REQUEST))
    check("the next new record continues the numbering", fresh["sl_no"], 4)

    again = run()
    check("a populated destination is refused", again.returncode, 1)
    check("saying what is in the way", "destination is not empty" in again.stderr, True)
    check("and that merging is not the answer", "cannot be merged" in again.stderr, True)

    # --replace is for a second attempt after a half-finished one.
    replaced = run("--replace")
    check("replacing an existing destination works", replaced.returncode, 0)
    check("it says what it discarded", "discarding" in replaced.stdout, True)
    check("and the result is the source, not the source twice",
          len(MigrationStore(Database(target_url)).list()), 3)

    same = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "tools" / "migrate_db.py"),
         "--from", source.url, "--to", source.url],
        capture_output=True, text=True,
    )
    check("copying a database onto itself is refused", same.returncode, 2)

    if not TEST_DB_URL:
        target.close()
    source.close()


def test_roles_in_database(root: Path) -> None:
    """Role assignments come from the database first, config.yaml second."""
    print("\n=== where roles come from ===")
    from app.auth import role_patterns, roles_for
    from app.reference import ReferenceError, ReferenceStore

    settings = build_settings()
    ref = ReferenceStore(database_for(root, "roles.sqlite3"))

    settings.auth.live_roles = {}
    check("with an empty table, config decides", role_patterns(settings), settings.auth.roles)
    check("so lead is an approver", "approver" in roles_for("lead@example.com", settings), True)

    seeded = ref.seed_roles(settings.auth.roles)
    check("config seeds the table once", seeded["approver"], 2)
    settings.auth.live_roles = ref.roles()
    check("after which the database decides",
          sorted(role_patterns(settings)["approver"]), ["head@example.com", "lead@example.com"])
    check("seeding again does nothing", ref.seed_roles(settings.auth.roles), {})

    # Editing the database takes effect; editing config.yaml no longer does.
    ref.set_role("approver", ["someone.new@example.com"])
    settings.auth.live_roles = ref.roles()
    check("a change in the database is honoured",
          "approver" in roles_for("someone.new@example.com", settings), True)
    check("and the old holder loses it",
          "approver" in roles_for("lead@example.com", settings), False)
    settings.auth.roles["approver"] = ["lead@example.com", "another@example.com"]
    check("while config.yaml is now ignored",
          "approver" in roles_for("another@example.com", settings), False)

    check("globs still work from the database",
          "developer" in roles_for("anyone@example.com", settings), True)
    check("blank entries are dropped",
          ref.set_role("devops", ["  ", "ops@example.com", ""])["devops"], ["ops@example.com"])
    check("a list can be cleared", ref.set_role("devops", [])["devops"], [])
    refused("an unknown role is refused",
            lambda: ref.set_role("wizard", ["x@y.com"]), contains="not a role")

    # Clearing every role leaves nobody able to do anything — the API guards
    # against the caller doing that to themselves; the store itself allows it.
    settings.auth.live_roles = ref.roles()
    # Still a developer: that comes from the *@example.com glob, not from devops.
    check("clearing devops removes only devops",
          sorted(roles_for("ops@example.com", settings)), ["developer"])


def test_reference_store(root: Path) -> None:
    """The dropdown lists, now kept in the database rather than in files."""
    print("\n=== reference lists in the database ===")
    from app.reference import ReferenceError, ReferenceStore

    db = database_for(root, "ref.sqlite3")
    store = ReferenceStore(db)
    seed = build_settings().migrations

    check("a new database has no lists", store.is_empty(), True)
    counts = store.seed(seed)
    check("seeding takes the releases from config", counts["releases"], 2)
    check("and the microservices", counts["microservices"], 2)
    check("after which it is not empty", store.is_empty(), False)

    live = store.snapshot(fresh=True)
    check("the lists read back", live.releases, ["R2026.09", "R2026.10"])
    check("in the order given", live.migration_paths, ["SIT to QA", "QA to PROD"])
    check("with the microservices", [m.name for m in live.microservices], ["payments", "cart"])
    check("and their repos", live.service("cart").repos, ["demo/cart", "demo/cart-ui"])
    check("and their track leads", live.service("cart").track_leads, ["R. Iyer", "S. Rao"])

    # Seeding is once-only: it must never resurrect a deliberate deletion.
    store.set_list("releases", ["R2027.01"])
    check("a list can be replaced", store.snapshot(fresh=True).releases, ["R2027.01"])
    check("seeding again does nothing", store.seed(seed), {})
    check("so the edit stands", store.snapshot(fresh=True).releases, ["R2027.01"])

    check("blanks are dropped and order kept",
          store.set_list("releases", ["  R1  ", "", "R2"]).releases, ["R1", "R2"])
    check("duplicates collapse case-insensitively",
          store.set_list("releases", ["R1", "r1", "R2"]).releases, ["R1", "R2"])
    refused_list = lambda: store.set_list("nonsense", ["x"])
    try:
        refused_list()
        check("an unknown list is refused", "allowed", "rejected")
    except ReferenceError:
        check("an unknown list is refused", "rejected", "rejected")
    check("an all-blank list clears it, deliberately",
          store.set_list("releases", ["   ", ""]).releases, [])
    store.set_list("releases", ["R2026.09", "R2026.10"])

    try:
        store.save_microservice("   ", [], [])
        check("a nameless microservice is refused", "allowed", "rejected")
    except ReferenceError:
        check("a nameless microservice is refused", "rejected", "rejected")
    try:
        store.set_list("releases", ["x" * 201])
        check("an absurdly long value is refused", "allowed", "rejected")
    except ReferenceError:
        check("an absurdly long value is refused", "rejected", "rejected")

    # Microservices.
    live = store.save_microservice("billing", ["demo/billing", "demo/billing "], ["T. Nair"])
    check("a microservice can be added", [m.name for m in live.microservices][-1], "billing")
    check("with repeated repos collapsed", live.service("billing").repos, ["demo/billing"])

    live = store.save_microservice("billing", ["demo/billing", "demo/billing-ui"], ["T. Nair", "P. Das"])
    check("and edited in place", live.service("billing").repos, ["demo/billing", "demo/billing-ui"])
    check("without duplicating the entry", len([m for m in live.microservices if m.name == "billing"]), 1)
    check("leads replaced wholesale", live.service("billing").track_leads, ["T. Nair", "P. Das"])

    live = store.save_microservice("invoicing", ["demo/billing"], ["T. Nair"], rename_from="billing")
    check("renaming keeps one entry", [m.name for m in live.microservices].count("invoicing"), 1)
    check("and drops the old name", "billing" in [m.name for m in live.microservices], False)
    check("carrying its links", live.service("invoicing").repos, ["demo/billing"])

    try:
        store.save_microservice("payments", [], [], rename_from="invoicing")
        check("renaming onto an existing name is refused", "allowed", "rejected")
    except ReferenceError:
        check("renaming onto an existing name is refused", "rejected", "rejected")

    live = store.delete_microservice("invoicing")
    check("a microservice can be deleted", "invoicing" in [m.name for m in live.microservices], False)
    try:
        store.delete_microservice("invoicing")
        check("deleting it twice is refused", "allowed", "rejected")
    except ReferenceError:
        check("deleting it twice is refused", "rejected", "rejected")

    # Records referencing a microservice are counted before it is removed.
    settings = build_settings()
    settings.migrations = store.snapshot(fresh=True)
    settings.migrations.enabled = True
    records = MigrationStore(db)
    migrations.create_record(settings, records, DEV, {
        **GOOD_REQUEST, "release": settings.migrations.releases[0],
    })
    check("records using a microservice are counted", store.in_use("payments"), 1)
    check("and an unused one counts zero", store.in_use("cart"), 0)

    # The snapshot is cached, but a write must be visible at once.
    store.set_list("change_requestors", ["Ops"])
    check("a write is visible immediately", store.snapshot().change_requestors, ["Ops"])


def test_archive(root: Path) -> None:
    """Filing away finished records, and the immutability that follows."""
    print("\n=== archiving ===")
    settings = build_settings()
    settings.auth.roles["admin"] = ["boss@elsewhere.com"]
    store = store_for(root, "archive.sqlite3")
    ADMIN = User("boss@elsewhere.com", frozenset({"admin"}))

    def to_prod(sl: int) -> None:
        migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_qa": "Yes"})
        migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_qa": "Yes"})
        migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_prod": "Yes"})
        migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_prod": "Yes"})

    made = [migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))["sl_no"]
            for _ in range(4)]
    done, part, fresh, retired = made
    to_prod(done)
    to_prod(part)
    migrations.apply_changes(settings, store, APPROVER, retired, {"ready_for_qa": "Yes"})
    store.set_active(retired, False, APPROVER)

    # Only production-migrated records qualify.
    out = store.archive([fresh], APPROVER)
    check("a record still in flight cannot be archived", out["archived"], [])
    check("and is told why", out["skipped"][0]["reason"], "Not migrated to production yet.")
    out = store.archive([retired], APPROVER)
    check("nor an inactive one", out["skipped"][0]["reason"], "Inactive.")
    out = store.archive([9999], APPROVER)
    check("a missing record is reported", out["skipped"][0]["reason"], "No such record.")

    out = store.archive([done, fresh], APPROVER)
    check("a mixed selection archives what it can", [r["sl_no"] for r in out["archived"]], [done])
    check("and reports the rest", len(out["skipped"]), 1)
    check("the archived record says so", out["archived"][0]["archived"], True)
    check("naming who filed it", out["archived"][0]["archived_by"], "lead@example.com")
    check("and when", out["archived"][0]["archived_at"].endswith("Z"), True)

    check("archiving twice is reported", store.archive([done], APPROVER)["skipped"][0]["reason"],
          "Already archived.")

    # It leaves the register, and can be found only in the archive.
    check("it is gone from the register", done in [r["sl_no"] for r in store.list()], False)
    check("even asking for inactive ones",
          done in [r["sl_no"] for r in store.list(include_inactive=True)], False)
    check("the archive holds it", [r["sl_no"] for r in store.list(archived=True)], [done])
    check("and holds only archived records",
          all(r["archived"] for r in store.list(archived=True)), True)
    check("the register still has the others", sorted(r["sl_no"] for r in store.list()),
          sorted([part, fresh]))

    # Nothing about it may change again.
    archived_row = store.get(done)
    check("no field is editable", migrations.editable_fields(APPROVER, archived_row), [])
    check("not even for an admin", migrations.editable_fields(ADMIN, archived_row), [])
    # Asked by someone who *would* hold the field, so it is archived-ness that
    # refuses it and not the role — the role is checked first, as with freezes.
    refused("an archived record cannot be edited",
            lambda: migrations.apply_changes(settings, store, DEVOPS, done,
                                             {"prod_migration_remarks": "late note"}),
            contains="archived")
    refused("nor deactivated",
            lambda: store.set_active(done, False, APPROVER),
            contains="archived")
    refused("nor deleted",
            lambda: store.delete(done, APPROVER),
            contains="archived")
    # A bulk action reports it rather than raising, as it does for anything
    # else it cannot move.
    bulk = migrations.approve_many(settings, store, APPROVER, [done], "qa")
    check("a bulk action will not move it", bulk["approved"], [])
    check("and says it is archived", "archived" in bulk["skipped"][0]["reason"].casefold(), True)

    check("the filing is in its history",
          any(e["field"] == "archived" and e["new_value"] == "archived"
              for e in store.audit(done)), True)
    check("attributed to whoever did it",
          next(e["who"] for e in store.audit(done) if e["field"] == "archived"),
          "lead@example.com")

    # An admin may archive too; a developer and devops may not — checked at the API.
    out = store.archive([part], ADMIN)
    check("an admin can archive", [r["sl_no"] for r in out["archived"]], [part])
    check("the archive now holds both",
          sorted(r["sl_no"] for r in store.list(archived=True)), sorted([done, part]))
    check("and the register neither", sorted(r["sl_no"] for r in store.list()), [fresh])

    check("archiving is offered to approvers and admins",
          (migrations.permissions(APPROVER)["can_archive"],
           migrations.permissions(ADMIN)["can_archive"]), (True, True))
    check("and not to developers or devops",
          (migrations.permissions(DEV)["can_archive"],
           migrations.permissions(DEVOPS)["can_archive"]), (False, False))


def main() -> int:
    test_config_parsing()
    test_table_list_is_complete()
    test_identity()
    test_dev_mode()
    test_validation()
    with tempfile.TemporaryDirectory() as tmp:
        test_workflow(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_freeze(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_sorting_and_dates(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_retire_and_delete(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_microservice_file(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_bulk_approval(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_bulk_execution(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_database_reachability(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_delete_audit(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_db_migration(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_roles_in_database(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_reference_store(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_archive(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_merge_type(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_employees(Path(tmp))
    test_sheet()
    test_permissions()
    test_spec()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_http(Path(tmp)))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_http_dev(Path(tmp)))
    if _SHARED_PG is not None:
        _SHARED_PG.close()
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
