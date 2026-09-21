"""Tests for the spreadsheet upload, the tag convention, and bulk processing.

Runs against a local git fixture so the whole pipeline — parse the sheet,
resolve each repository, summarise the folder, propose a tag — is exercised
without a network call or a valid token.

Run with:  .venv/bin/python tests/test_bulk.py
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bulk, naming, spreadsheet  # noqa: E402
from app import csr as csr_provider  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.github import GitHubClient  # noqa: E402
from app.store import FileStore  # noqa: E402

FAILURES: list[str] = []
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def git(args: list[str], cwd: Path | None = None, when: str | None = None) -> str:
    env = {**ENV, "HOME": str(cwd or Path("/tmp"))}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env).stdout.strip()


def sheet_bytes(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_parsing() -> None:
    print("=== reading a spreadsheet ===")

    # A realistic sheet: title line, blank row, aliased headers, a junk row.
    data = sheet_bytes([
        ["Q3 Release Plan"],
        [],
        ["Repository Name", "Sub Folder", "Branch Name", "Remarks"],
        ["demo/one", "svc", "main", "a note"],
        ["demo/two", "", "", ""],
        [None, None, None, "no repo here"],
    ])
    parsed = spreadsheet.parse_sheet("plan.xlsx", data)
    check("the header is found below a title and a blank row", parsed["header_row"], 3)
    check("aliased column names resolve", parsed["columns"], ["branch", "folder", "note", "repo"])
    check("only rows with a repository are kept", len(parsed["rows"]), 2)
    check("a row without one is counted, not silently dropped", parsed["ignored"], 1)
    check("values come through", parsed["rows"][0]["folder"], "svc")
    check("the sheet row number is kept for error messages", parsed["rows"][0]["_row"], 4)

    csv_bytes = b"Repository,Folder,Branch\ndemo/one,svc,main\n"
    check("CSV works too", len(spreadsheet.parse_sheet("plan.csv", csv_bytes)["rows"]), 1)
    check("semicolon-delimited CSV is sniffed",
          len(spreadsheet.parse_sheet("p.csv", b"Repository;Folder\ndemo/one;svc\n")["rows"]), 1)

    for content, name, why in [
        (b"", "x.xlsx", "an empty file"),
        (b"a,b\n1,2\n", "x.csv", "a sheet with no repository column"),
        (b"x", "x.xls", "the legacy .xls format"),
        (b"x", "x.docx", "an unsupported extension"),
    ]:
        try:
            spreadsheet.parse_sheet(name, content)
            check(f"{why} is rejected", "accepted", "rejected")
        except spreadsheet.SheetError:
            check(f"{why} is rejected", "rejected", "rejected")

    check("the template is a real workbook",
          len(spreadsheet.parse_sheet("t.xlsx", spreadsheet.build_template())["rows"]), 3)


def test_convention() -> None:
    print("\n=== tag conventions ===")
    common = dict(repo="acme/payments", folder="services/api", branch="release/2.0",
                  sha="abc1234def5678", commit_date="2026-09-18T10:00:00Z")

    check("plain placeholders fill",
          naming.render("v{yyyy}.{mm}.{dd}", existing_tags=[], **common), "v2026.09.18")
    check("slugs replace separators",
          naming.render("{repo_name}-{folder_slug}", existing_tags=[], **common),
          "payments-services-api")
    check("short and full sha both work",
          naming.render("{sha7}|{sha}", existing_tags=[], **common), "abc1234|abc1234def5678")
    check("nothing is left unsubstituted",
          "{" in naming.render("{repo}{repo_name}{owner}{folder}{folder_slug}{branch}"
                               "{branch_slug}{sha}{sha7}{date}{yyyy}{mm}{dd}{today}",
                               existing_tags=[], **common), False)

    check("a sequence starts at one",
          naming.render("rel-{n:03}", existing_tags=[], **common), "rel-001")
    check("it continues from what exists",
          naming.render("rel-{n:03}", existing_tags=["rel-001", "rel-004"], **common), "rel-005")
    check("unrelated tags cannot inflate it",
          naming.render("rel-{n:03}", existing_tags=["rel-002", "v9.9.9", "other-77"], **common),
          "rel-003")
    check("padding is respected",
          naming.render("rel-{n}", existing_tags=["rel-7"], **common), "rel-8")

    for bad, why in [("", "an empty convention"), ("v{nope}", "an unknown placeholder"),
                     ("v{Sha7}", "wrong case")]:
        try:
            naming.render(bad, existing_tags=[], **common)
            check(f"{why} is rejected", "accepted", "rejected")
        except naming.NamingError:
            check(f"{why} is rejected", "rejected", "rejected")


@dataclass(frozen=True)
class Repo:
    path: str
    project: str = "demo"
    repo: str = "payments"

    @property
    def key(self) -> str:
        return "csr:demo/payments"

    @property
    def name(self) -> str:
        return "demo/payments"

    @property
    def clone_url(self) -> str:
        return self.path

    @property
    def web_url(self) -> str:
        return "https://example.test"

    def commit_url(self, sha: str) -> str:
        return f"https://example.test/{sha}"


async def test_processing(root: Path) -> None:
    print("\n=== processing rows ===")

    work = root / "src"
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev One"], work)
    git(["config", "tag.gpgSign", "false"], work)

    (work / "svc").mkdir()
    (work / "svc" / "a.py").write_text("one\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "svc: first"], work, "2026-09-01T10:00:00+00:00")
    git(["tag", "-a", "rel-001", "-m", "first"], work, "2026-09-01T11:00:00+00:00")

    (work / "other").mkdir()
    (work / "other" / "b.py").write_text("two\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "other: only elsewhere"], work, "2026-09-02T10:00:00+00:00")

    (work / "svc" / "c.py").write_text("three\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "svc: latest here"], work, "2026-09-03T10:00:00+00:00")

    repo = Repo(path=str(work))
    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [repo]
    settings.gcloud_token = "unused-for-local-paths"
    settings.mirror_dir = root / "mirror"
    settings.store_path = root / "store.sqlite3"
    settings.folder_exclude = ["__pycache__"]

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)
    store = FileStore(settings.store_path)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Repository cells can be written several ways.
    check("a full name resolves", bulk.resolve_repo_key("demo/payments", settings), repo.key)
    check("a bare name resolves", bulk.resolve_repo_key("payments", settings), repo.key)
    check("a repo key resolves", bulk.resolve_repo_key("csr:demo/payments", settings), repo.key)
    check("case is ignored", bulk.resolve_repo_key("DEMO/Payments", settings), repo.key)
    check("an unknown name does not", bulk.resolve_repo_key("nope/nope", settings), None)
    check("a blank cell does not", bulk.resolve_repo_key("  ", settings), None)

    rows = [
        {"_row": 2, "repo": "demo/payments", "folder": "svc", "branch": "main"},
        {"_row": 3, "repo": "payments", "folder": "", "branch": ""},
        {"_row": 4, "repo": "demo/payments", "folder": "nosuch", "branch": "main"},
        {"_row": 5, "repo": "ghost/repo", "folder": "", "branch": ""},
        {"_row": 6, "repo": "demo/payments", "folder": "svc", "branch": "no-branch"},
    ]
    out = await bulk.process_rows(
        settings, gh, mirror, store,
        rows=rows, convention="rel-{n:03}", since_dt=since,
    )

    check("every row is accounted for", out["total"], 5)
    check("the resolvable ones are resolved", out["resolved"], 2)
    check("and the rest are reported", out["failed"], 3)

    by_row = {r["row"]: r for r in out["rows"]}

    svc = by_row[2]
    check("the folder's latest commit is chosen, not the repo's",
          svc["latest_title"], "svc: latest here")
    check("only that folder's commits are counted", svc["commit_count"], 2)
    check("the exact directory is reported", svc["directories"], ["svc"])
    check("the author is carried", svc["latest_author"], "Dev One")
    check("the full hash is carried", len(svc["latest_sha"]), 40)

    whole = by_row[3]
    check("a blank folder covers the whole repository", whole["commit_count"], 3)
    check("and its latest is the repo's latest", whole["latest_title"], "svc: latest here")
    check("a blank branch uses the default", whole["branch"], "main")

    check("a folder with no commits says so", "no commits in nosuch" in by_row[4]["error"], True)
    check("an unknown repository says so",
          "not a configured repository" in by_row[5]["error"], True)
    check("a missing branch says so", "no branch" in by_row[6]["error"], True)

    # rel-001 already exists in the fixture, so proposals must start at 002 and
    # the two resolvable rows must not both claim the same number.
    proposed = [r["proposed_tag"] for r in out["rows"] if r.get("proposed_tag")]
    check("proposals skip the tag that already exists", "rel-001" in proposed, False)
    check("two rows never propose the same tag", len(set(proposed)), len(proposed))
    check("they continue the series", sorted(proposed), ["rel-002", "rel-003"])
    check("none is flagged as already existing",
          [r["tag_exists"] for r in out["rows"] if "tag_exists" in r], [False, False])

    check("an existing tag on the commit is surfaced",
          by_row[3]["existing_tags"] in ([], ["rel-001"]), True)

    # A bad convention must fail the whole run, not silently produce junk.
    try:
        await bulk.process_rows(settings, gh, mirror, store,
                                rows=rows[:1], convention="v{bogus}", since_dt=since)
        check("a bad convention is rejected", "accepted", "rejected")
    except bulk.BulkError:
        check("a bad convention is rejected", "rejected", "rejected")

    try:
        await bulk.process_rows(settings, gh, mirror, store,
                                rows=[rows[0]] * (bulk.MAX_ROWS + 1),
                                convention="rel-{n}", since_dt=since)
        check("an oversized sheet is rejected", "accepted", "rejected")
    except bulk.BulkError:
        check("an oversized sheet is rejected", "rejected", "rejected")

    await gh.aclose()


async def test_concurrent_sync(root: Path) -> None:
    """Many rows naming one repository must not race each other's clone.

    Regression: sync() used to check, remove and clone with no mutual exclusion,
    so a second caller arriving mid-clone hit "destination path already exists
    and is not an empty directory". Several folders of one repo is the ordinary
    case for this tab, so the collision was the rule, not the exception.
    """
    print("\n=== concurrent syncs of one repo ===")

    work = root / "concurrent"
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev One"], work)
    (work / "f.py").write_text("x\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "only"], work, "2026-09-01T10:00:00+00:00")

    repo = Repo(path=str(work))
    settings = load_settings()
    settings.mirror_dir = root / "mirror-concurrent"
    settings.gcloud_token = "unused-for-local-paths"
    mirror = csr_provider.GitMirror(settings)

    # max_age=0 is the default, so this exercises the lock and not the reuse window.
    async def one():
        try:
            return await mirror.sync(repo.key, repo.clone_url)
        except Exception as exc:  # noqa: BLE001 - the failure mode under test
            return exc

    results = await asyncio.gather(*(one() for _ in range(8)))
    errors = [r for r in results if isinstance(r, Exception)]
    check("eight concurrent syncs all succeed", len(errors), 0)
    if errors:
        print(f"        first error: {errors[0]}")
    check("they agree on one mirror path", len({str(r) for r in results if not isinstance(r, Exception)}), 1)
    check("and it is a real clone", (mirror.path_for(repo.key) / "HEAD").exists(), True)

    # The reuse window must not hand back a path that was never cloned.
    fresh = csr_provider.GitMirror(settings)
    fresh._synced_at[repo.key] = __import__("time").monotonic()
    shutil.rmtree(fresh.path_for(repo.key), ignore_errors=True)
    path = await fresh.sync(repo.key, repo.clone_url, max_age=60)
    check("a stale note does not skip a missing clone", (path / "HEAD").exists(), True)


def main() -> int:
    test_parsing()
    test_convention()
    # The concurrency check runs first: when it regresses it takes the row
    # processing down with it, and its own name is the more useful report.
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_concurrent_sync(Path(tmp)))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_processing(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
