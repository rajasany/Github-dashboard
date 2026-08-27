"""Cherry-pick detection, across the detector and every surface that reports it.

Uses a fixture repository containing a real `git cherry-pick -x` commit, a
hand-written mention, and ordinary commits, so the three cases are distinguished
against genuine git output rather than hand-built strings alone.

Run with:  .venv/bin/python tests/test_cherrypick.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import csr as csr_provider  # noqa: E402
from app import lookup, ordering, summary  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.github import GitHubClient  # noqa: E402
from app.models import detect_cherry_pick  # noqa: E402
from app.store import FileStore  # noqa: E402

FAILURES: list[str] = []
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env={**ENV, "HOME": str(cwd)}
    ).stdout.strip()


def test_detector() -> None:
    print("=== detector ===")

    recorded = detect_cherry_pick(
        "Fix login\n\n(cherry picked from commit 9fceb02d0ae598e95dc970b74767f19372d61af8)"
    )
    check("the -x trailer is recognised", recorded["is_cherry_pick"], True)
    check("its evidence is 'recorded'", recorded["evidence"], "recorded")
    check("the source commit is captured",
          recorded["source_sha"], "9fceb02d0ae598e95dc970b74767f19372d61af8")

    check("a hyphenated trailer also matches",
          detect_cherry_pick("x\n\n(cherry-picked from commit 9fceb02)")["source_sha"], "9fceb02")
    check("the sha is lower-cased",
          detect_cherry_pick("x\n\n(cherry picked from commit ABCDEF1234)")["source_sha"], "abcdef1234")

    mentioned = detect_cherry_pick("Backport: cherry-pick of the auth fix")
    check("a bare mention is flagged", mentioned["is_cherry_pick"], True)
    check("but only as 'mentioned'", mentioned["evidence"], "mentioned")
    check("with no invented source", mentioned["source_sha"], None)

    for text, why in [
        ("Add cherry flavour to the picker", "'cherry' and 'pick' in unrelated words"),
        ('Revert "Fix login"', "a revert is not a cherry-pick"),
        ("Ordinary commit", "plain message"),
        ("", "empty message"),
    ]:
        check(f"not flagged: {why}", detect_cherry_pick(text)["is_cherry_pick"], False)


@dataclass(frozen=True)
class Fixture:
    root: str
    project: str = "demo"
    repo: str = "cp"

    @property
    def key(self) -> str:
        return "csr:demo/cp"

    @property
    def name(self) -> str:
        return "demo/cp"

    @property
    def clone_url(self) -> str:
        return self.root

    @property
    def web_url(self) -> str:
        return "https://example.test"

    def commit_url(self, sha: str) -> str:
        return f"https://example.test/{sha}"


async def test_surfaces(root: Path) -> None:
    print("\n=== reported by every surface ===")

    work = root / "work"
    work.mkdir()
    git(["init", "-q", "-b", "main"], work)
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev"], work)
    (work / "a.txt").write_text("one")
    git(["add", "-A"], work)
    git(["commit", "-qm", "base"], work)

    git(["checkout", "-qb", "feature"], work)
    (work / "fix.txt").write_text("fix")
    git(["add", "-A"], work)
    git(["commit", "-qm", "Fix the auth bug"], work)
    source = git(["rev-parse", "HEAD"], work)

    git(["checkout", "-q", "main"], work)
    git(["cherry-pick", "-x", source], work)
    (work / "b.txt").write_text("b")
    git(["add", "-A"], work)
    git(["commit", "-qm", "Manual backport: cherry-pick of something"], work)
    (work / "c.txt").write_text("c")
    git(["add", "-A"], work)
    git(["commit", "-qm", "Ordinary work"], work)

    # The fixture must actually contain a real trailer, or the rest proves nothing.
    trailer = git(["log", "--format=%b", "-1", "main~2"], work)
    check("the fixture really has a -x trailer", "cherry picked from commit" in trailer, True)

    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [Fixture(root=str(work))]
    settings.gcloud_token = "unused"
    settings.mirror_dir = root / "mirror"
    settings.store_path = root / "store.sqlite3"

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)
    store = FileStore(settings.store_path)
    path = await mirror.sync(Fixture(root=str(work)).key, str(work))

    def by_title(rows, key="title"):
        return {r[key]: r for r in rows}

    # --- Summary table ---
    result = await summary.build_summary(
        settings, gh, mirror, store, repo_key="csr:demo/cp", branch="main",
        folder=None, since_dt=datetime(2020, 1, 1, tzinfo=timezone.utc),
        until_dt=None, limit=50,
    )
    rows = by_title(result["rows"])
    check("summary: the -x commit is recorded",
          rows["Fix the auth bug"]["cherry_pick"]["evidence"], "recorded")
    check("summary: it names the source",
          rows["Fix the auth bug"]["cherry_pick"]["source_sha"], source)
    check("summary: the hand-written one is only mentioned",
          rows["Manual backport: cherry-pick of something"]["cherry_pick"]["evidence"], "mentioned")
    check("summary: ordinary commits are not flagged",
          rows["Ordinary work"]["cherry_pick"]["is_cherry_pick"], False)

    # --- Find a commit ---
    picked = git(["rev-parse", "main~2"], work)
    found = await lookup.lookup_commit(settings, gh, mirror, picked)
    cp = found["matches"][0]["cherry_pick"]
    check("lookup: flagged", cp["is_cherry_pick"], True)
    check("lookup: source carried", cp["source_sha"], source)

    plain = await lookup.lookup_commit(settings, gh, mirror, git(["rev-parse", "main"], work))
    check("lookup: an ordinary commit is not flagged",
          plain["matches"][0]["cherry_pick"]["is_cherry_pick"], False)

    # --- Order commits ---
    shas = git(["rev-list", "main"], work).split()
    ordered = await ordering.order_commits(settings, gh, mirror, raw="\n".join(shas))
    listed = by_title(ordered["commits"])
    check("order: the -x commit is recorded",
          listed["Fix the auth bug"]["cherry_pick"]["evidence"], "recorded")
    check("order: the mention is weaker",
          listed["Manual backport: cherry-pick of something"]["cherry_pick"]["evidence"], "mentioned")
    check("order: ordinary commits are clean",
          listed["Ordinary work"]["cherry_pick"]["is_cherry_pick"], False)
    check("order: every entry carries the field",
          all("cherry_pick" in c for c in ordered["commits"]), True)

    await gh.aclose()


def main() -> int:
    test_detector()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_surfaces(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
