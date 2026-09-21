"""Tests for migration requests: identity, field-level roles, the workflow gates,
freeze windows, validation and the spreadsheet round trip.

These are the rules the UI cannot be trusted to keep, so they are asserted against
the server-side functions directly.

Run with:  .venv/bin/python tests/test_migrations.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import migration_sheet, migrations  # noqa: E402
from app.auth import User, dev_mode_available, resolve_user, roles_for  # noqa: E402
from app.config import AuthConfig, Microservice, MigrationConfig, load_settings  # noqa: E402
from app.migrations import MigrationError, MigrationStore  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def refused(name: str, fn, *, contains: str = "") -> None:
    """Assert a call is rejected, and optionally that it says why."""
    try:
        fn()
    except (MigrationError, PermissionError) as exc:
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

GOOD_REQUEST = {
    "release": "R2026.09",
    "migration_path": "SIT to QA",
    "microservice": "payments",
    "repo_name": "demo/payments",
    "track_lead": "A. Kumar",
    "change_requestor": "Business Ops",
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
    store = MigrationStore(root / "migrations.sqlite3")

    record = migrations.create_record(settings, store, DEV, dict(GOOD_REQUEST))
    sl = record["sl_no"]
    check("a request is created", sl >= 1, True)
    check("created_by is taken from the login", record["created_by"], "dev@example.com")
    check("date created is stamped", bool(record["date_created"]), True)
    check("unspecified risk flags default to No", record["env_change"], "No")
    check("it starts awaiting approval", record["status"], "submitted")
    check("SL# is the sequence", record["sl_no"], sl)

    refused("a developer cannot approve",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"approved_by": "lead@example.com"}),
            contains="approver role")
    refused("devops cannot approve",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"approved_by": "lead@example.com"}),
            contains="approver role")
    refused("nobody can write an auto field",
            lambda: migrations.apply_changes(settings, store, APPROVER, sl, {"date_approved": "2026-01-01"}),
            contains="automatically")
    refused("a developer cannot record a QA migration",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"executed_in_qa": "Yes"}),
            contains="devops role")
    refused("devops cannot record QA before approval",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"executed_in_qa": "Yes"}),
            contains="not approved")
    refused("another developer cannot edit someone else's request",
            lambda: migrations.apply_changes(settings, store, DEV2, sl, {"reason": "mine now"}),
            contains="raised the request")

    edited = migrations.apply_changes(settings, store, DEV, sl, {"reason": "Defect fix, revised"})
    check("the author can edit their own request", edited["reason"], "Defect fix, revised")

    approved = migrations.apply_changes(
        settings, store, APPROVER, sl, {"approved_by": "lead@example.com", "qa_date_planned": "2026-10-01"}
    )
    check("an approver can approve", approved["approved_by"], "lead@example.com")
    check("the approval date is filled in automatically", bool(approved["date_approved"]), True)
    check("the planned QA date is kept", approved["qa_date_planned"], "2026-10-01")
    check("status moves to approved", approved["status"], "approved")

    refused("the request is locked once approved",
            lambda: migrations.apply_changes(settings, store, DEV, sl, {"reason": "changed my mind"}),
            contains="withdraw the approval")
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
    check("status moves to in QA", qa["status"], "in_qa")

    refused("devops cannot declare it ready for prod",
            lambda: migrations.apply_changes(settings, store, DEVOPS, sl, {"ready_for_prod": "Yes"}),
            contains="approver role")

    ready = migrations.apply_changes(settings, store, APPROVER, sl, {"ready_for_prod": "Yes"})
    check("the approver marks it ready after testing", ready["status"], "ready_for_prod")

    prod = migrations.apply_changes(
        settings, store, DEVOPS, sl, {"executed_in_prod": "Yes", "prod_migration_remarks": "Done 02:10"}
    )
    check("devops can record the prod migration", prod["executed_in_prod"], "Yes")
    check("the prod date is stamped", bool(prod["prod_migration_date"]), True)
    check("and who did it is recorded", prod["prod_migrated_by"], "ops@example.com")
    check("status reaches in prod", prod["status"], "in_prod")

    # Withdrawing an approval must not leave its evidence behind.
    withdrawn = migrations.apply_changes(settings, store, APPROVER, sl, {"approved_by": ""})
    check("withdrawing an approval clears its date", withdrawn["date_approved"], "")

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
    check("devops sees nothing on an unapproved record",
          migrations.editable_fields(DEVOPS, store.get(fresh["sl_no"])), [])

    entries = store.audit(sl)
    check("the audit trail records the approval",
          any(e["field"] == "approved_by" and e["new_value"] == "lead@example.com" for e in entries), True)
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
    store = MigrationStore(root / "freeze.sqlite3")
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
                 "Track Lead Name", "Change Requestor", "Code & Image Change?", "Commit Hash"])
    page.append(["R2026.09", "SIT to QA", "payments", "demo/payments", "A. Kumar",
                 "Business Ops", "Yes", "a1b2c3d"])
    page.append(["R2026.09", "SIT to QA", "cart", "demo/cart", "R. Iyer",
                 "Business Ops", "yes", ""])           # missing the required hash
    page.append(["R1999.01", "SIT to QA", "payments", "demo/payments", "A. Kumar",
                 "Business Ops", "No", ""])            # release not in the list
    page.append(["R2026.09", "SIT to QA", "payments", "demo/cart", "A. Kumar",
                 "Business Ops", "No", ""])            # repo of another microservice
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


def test_spec() -> None:
    print("\n=== the field contract ===")
    spec = migrations.field_spec()
    check("every field is published", len(spec), len(migrations.FIELDS))
    # 27 from the specification, plus prod_migrated_by for symmetry with QA Migrated By.
    check("all 28 fields are present", len(migrations.FIELDS), 28)
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
    check("prod migration date is auto", migrations.BY_KEY["prod_migration_date"].kind, "auto")


async def test_http(root: Path) -> None:
    """The same rules, asserted over HTTP — headers, status codes and all."""
    print("\n=== over HTTP ===")
    import httpx

    from app import main as M

    template = build_settings()
    M.settings.migrations = template.migrations
    M.settings.auth = template.auth
    M.settings.migration_store_path = root / "http.sqlite3"

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

            r = await c.patch(f"/api/migrations/records/{sl}",
                              json={"approved_by": "lead@example.com"})
            check("but cannot approve it", r.status_code, 403)
            check("and is told which role that needs",
                  "approver" in r.json()["detail"], True)
            check("naming the field, so the form can mark it",
                  list(r.json()["fields"]), ["approved_by"])

            # A 28-field form has to say which fields are wrong, not just that
            # something is — the client marks the controls from this.
            r = await c.post("/api/migrations/records", json={"microservice": "payments"})
            check("an incomplete request is refused", r.status_code, 422)
            check("with every missing field named",
                  sorted(r.json()["fields"]),
                  ["change_requestor", "migration_path", "release", "repo_name", "track_lead"])
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
                              json={"approved_by": "lead@example.com", "qa_date_planned": "2026-10-02"})
            check("an approver can approve", r.status_code, 200)
            check("the approval is dated automatically", bool(r.json()["date_approved"]), True)

        async with client("ops@example.com") as c:
            r = await c.patch(f"/api/migrations/records/{sl}", json={"executed_in_qa": "Yes"})
            check("devops can then record QA", r.status_code, 200)
            check("and is recorded as having done it", r.json()["qa_migrated_by"], "ops@example.com")

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
                         "Track Lead Name", "Change Requestor"])
            page.append(["R2026.09", "SIT to QA", "cart", "demo/cart", "R. Iyer", "Business Ops"])
            page.append(["R2026.09", "SIT to QA", "cart", "demo/payments", "R. Iyer", "Business Ops"])
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
            check("committing imports only the valid rows", len(r.json()["created"]), 1)

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
            # Every field, plus the derived Status and Active columns.
            check("with a header naming every field",
                  r.text.splitlines()[0].count(",") + 1, len(migrations.FIELDS) + 2)

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
            check("the CSV gains an Active column", csv_text.splitlines()[0].endswith(",Active"), True)

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
        M.settings.auth.roles["admin"] = ["boss@elsewhere.com"]
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
    M.settings.migration_store_path = root / "dev.sqlite3"

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

            r = await c.patch(f"/api/migrations/records/{sl}", json={"approved_by": "lead@example.com"})
            check("and cannot approve, being only a developer", r.status_code, 403)

        # Switching identity is a cookie away — no restart, no config edit.
        async with client(cookies={"dev_user": "lead@example.com"}) as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("the cookie switches who you are", meta["user"]["email"], "lead@example.com")
            check("with the approver role", "approver" in meta["user"]["roles"], True)
            r = await c.patch(f"/api/migrations/records/{sl}", json={"approved_by": "lead@example.com"})
            check("who can then approve", r.status_code, 200)

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
            meta = (await c.get("/api/migrations/meta")).json()
            check("and meta explains why", "loopback" in meta["dev"]["why_not"], True)
            check("while still admitting it is configured", meta["dev"]["configured"], True)

        M.settings.auth.dev_allow_remote = True
        async with client(peer="10.1.2.3") as c:
            check("unless explicitly widened",
                  (await c.get("/api/migrations/records")).status_code, 200)
        M.settings.auth.dev_allow_remote = False

        M.settings.auth.trusted_proxies = ["127.0.0.1"]
        async with client() as c:
            meta = (await c.get("/api/migrations/meta")).json()
            check("a configured proxy switches dev mode off", meta["dev"]["enabled"], False)
            check("and nobody is signed in without a header", meta["user"]["signed_in"], False)


def test_sorting_and_dates(root: Path) -> None:
    """The created date, the orderings it enables, and the range filter."""
    print("\n=== sorting and the created date ===")
    settings = build_settings()
    store = MigrationStore(root / "sorting.sqlite3")

    import sqlite3
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
    conn = sqlite3.connect(store.path)
    for sl, when in made:
        conn.execute("UPDATE migrations SET date_created = ? WHERE sl_no = ?", (str(at(when)), sl))
    conn.commit()
    conn.close()

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
    migrations.apply_changes(settings, store, APPROVER, 1, {"approved_by": "lead@example.com"})
    check("status sorts along the workflow",
          [r["status"] for r in store.list(sort="status", direction="asc")],
          ["submitted", "submitted", "approved"])

    # A row with no date must not float to the top of a newest-first sort.
    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE migrations SET date_created = '' WHERE sl_no = 2")
    conn.commit()
    conn.close()
    check("an undated row sorts as oldest", order(sort="date_created", direction="desc")[-1], 2)
    check("and is not dropped", len(order(sort="date_created")), 3)

    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE migrations SET date_created = ? WHERE sl_no = 2", (str(at("2026-09-05T10:00:00")),))
    conn.commit()
    conn.close()

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
    store = MigrationStore(root / "retire.sqlite3")

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
                                             {"approved_by": "lead@example.com"}),
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

    check("deleting removes it", store.delete(first["sl_no"]), True)
    check("it is gone from the store", store.get(first["sl_no"]), None)
    check("its history goes with it", store.audit(first["sl_no"]), [])
    check("even when inactive rows are asked for",
          [r["sl_no"] for r in store.list(include_inactive=True)], [second["sl_no"]])
    check("deleting something absent says so", store.delete(9999), False)
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

    reopened = MigrationStore(legacy)
    check("an older database gains the column on open",
          "active" in {r[1] for r in sqlite3.connect(legacy).execute("PRAGMA table_info(migrations)")},
          True)
    check("and its existing rows count as active", reopened.get(1)["active"], True)
    check("so they are still listed", len(reopened.list()), 1)


def main() -> int:
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
    test_sheet()
    test_spec()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_http(Path(tmp)))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_http_dev(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
