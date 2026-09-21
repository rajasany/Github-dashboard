"""Copy the migration register from one database to another.

Works in any direction — SQLite to PostgreSQL, PostgreSQL to PostgreSQL, or back
to SQLite — because both sides go through app/db.py and the schema is created by
the application's own stores.

    .venv/bin/python tools/migrate_db.py \\
        --from "sqlite:///.cache/migrations.sqlite3" \\
        --to   "postgresql://dashboard:secret@db.internal:5432/repo_dashboard"

Primary keys are preserved: SL# is the number people quote to each other, so a
record must not be renumbered by being moved. That means inserting explicit ids,
which on PostgreSQL leaves the identity sequence behind — the next new record
would collide. The sequences are reset at the end, and that is the step most
easily forgotten when this is done by hand.

The destination must be empty. `--replace` empties it first, which is what you
want after a half-finished attempt; there is no merge, because two registers
cannot be combined while SL# is preserved on both sides.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import POSTGRES, Database  # noqa: E402
from app.migrations import MigrationStore  # noqa: E402
from app.reference import ReferenceStore  # noqa: E402

# Copied in this order: nothing here has foreign keys, but reading the register
# first makes the progress output tell you the interesting number soonest.
TABLES: list[tuple[str, str]] = [
    ("migrations", "sl_no"),
    ("migration_audit", "id"),
    ("freezes", "id"),
    ("ref_values", "id"),
    ("ref_microservices", "id"),
    ("ref_microservice_links", "id"),
    ("ref_employees", "id"),
]

CHUNK = 500


def counts(db: Database) -> dict[str, int]:
    out: dict[str, int] = {}
    with db.connect() as conn:
        for table, _ in TABLES:
            if not db.table_exists(conn, table):
                continue
            out[table] = int(conn.scalar(f"SELECT COUNT(*) AS n FROM {table}") or 0)
    return out


def copy_table(source: Database, target: Database, table: str) -> int:
    with source.connect() as src:
        if not source.table_exists(src, table):
            return 0
        rows = src.all(f"SELECT * FROM {table}")
    if not rows:
        return 0

    with target.connect() as dst:
        # Only columns both sides have, so a source written by an older version
        # copies into a newer schema without hand-editing anything.
        shared = [c for c in rows[0] if c in target.columns(dst, table)]
        placeholders = ", ".join("?" * len(shared))
        statement = f"INSERT INTO {table} ({', '.join(shared)}) VALUES ({placeholders})"
        for start in range(0, len(rows), CHUNK):
            dst.many(
                statement,
                [tuple(row[c] for c in shared) for row in rows[start : start + CHUNK]],
            )
    return len(rows)


def reset_sequences(target: Database) -> list[str]:
    """Move each identity sequence past the ids just inserted.

    Without this the first record created after a migration reuses SL# 1 and the
    insert fails on the primary key. SQLite works it out from the table itself.
    """
    if target.kind != POSTGRES:
        return []
    done = []
    with target.connect() as conn:
        for table, key in TABLES:
            if not target.table_exists(conn, table):
                continue
            highest = conn.scalar(f"SELECT COALESCE(MAX({key}), 0) AS n FROM {table}")
            if not highest:
                continue
            conn.execute(
                "SELECT setval(pg_get_serial_sequence(?, ?), ?)", (table, key, int(highest))
            )
            done.append(f"{table}.{key} → {highest}")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True, help="source database URL")
    parser.add_argument("--to", dest="target", required=True, help="destination database URL")
    parser.add_argument("--replace", action="store_true",
                        help="delete everything in the destination first")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied")
    args = parser.parse_args()

    source, target = Database(args.source), Database(args.target)
    if source.url == target.url:
        print("Source and destination are the same database.", file=sys.stderr)
        return 2

    print(f"from  {source.describe}")
    print(f"to    {target.describe}\n")
    try:
        source.verify()
        target.verify()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(exc, file=sys.stderr)
        return 2

    # Creating the stores is what builds the destination schema.
    MigrationStore(target)
    ReferenceStore(target)

    before, existing = counts(source), counts(target)
    total = sum(before.values())
    if not total:
        print("The source holds nothing to copy.")
        return 0

    for table, _ in TABLES:
        if before.get(table):
            print(f"  {table:<24} {before[table]:>6}")
    print(f"  {'':<24} {'-' * 6}\n  {'total':<24} {total:>6}\n")

    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    occupied = {t: n for t, n in existing.items() if n}
    if occupied and not args.replace:
        print("The destination is not empty:", file=sys.stderr)
        for table, n in occupied.items():
            print(f"  {table}: {n} rows", file=sys.stderr)
        print(
            "\nRefusing to copy into it. Records cannot be merged — both sides number "
            "their own SL# 1 — so pass --replace to discard what is there and copy over it.",
            file=sys.stderr,
        )
        return 1

    if occupied:
        print(f"  --replace: discarding {sum(occupied.values())} rows already there")
        with target.connect() as conn:
            for table, _ in reversed(TABLES):
                if target.table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table}")
        existing = {}

    for table, _ in TABLES:
        moved = copy_table(source, target, table)
        if moved:
            print(f"  copied {moved:>6} into {table}")

    for line in reset_sequences(target):
        print(f"  sequence {line}")

    after = counts(target)
    print()
    ok = True
    for table, _ in TABLES:
        want = before.get(table, 0) + existing.get(table, 0)
        got = after.get(table, 0)
        if want != got:
            print(f"  MISMATCH {table}: expected {want}, found {got}", file=sys.stderr)
            ok = False
    if not ok:
        return 1

    print(f"Done. {total} rows copied, destination verified.")
    source.close()
    target.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
