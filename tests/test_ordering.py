"""Tests for parsing and ordering a pasted commit list.

Uses a local git fixture with two branches so "same repo", "same branch" and
ancestry-vs-date ordering are all covered without touching the network.

Run with:  .venv/bin/python tests/test_ordering.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import csr as csr_provider  # noqa: E402
from app import ordering  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.github import GitHubClient  # noqa: E402

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


def test_parsing() -> None:
    print("=== parsing a pasted list ===")

    shas, rejected = ordering.parse_commit_list(
        "abc1234\ndef5678, 99887766\n- 11223344\n* https://github.com/o/r/commit/aabbccdd\n\n"
    )
    check("newlines, commas, bullets and URLs all parse",
          shas, ["abc1234", "def5678", "99887766", "11223344", "aabbccdd"])
    check("nothing spurious is rejected", rejected, [])

    shas, rejected = ordering.parse_commit_list("abc1234\nABC1234\nabc1234")
    check("case is normalised, so repeats collapse", shas, ["abc1234"])
    check("the repeats are reported, not silently dropped",
          [r["reason"] for r in rejected], ["duplicate", "duplicate"])

    shas, rejected = ordering.parse_commit_list("abc1234\nhello world\nzz")
    check("non-hashes are separated out", shas, ["abc1234"])
    check("and each is named", sorted(r["input"] for r in rejected), ["hello", "world", "zz"])

    shas, rejected = ordering.parse_commit_list("-\n*\n   \n")
    check("bullets alone yield nothing", shas, [])
    check("and are not reported as bad hashes", rejected, [])


@dataclass(frozen=True)
class Fixture:
    bare: str
    project: str = "demo"
    repo: str = "ordered"

    @property
    def key(self) -> str:
        return "csr:demo/ordered"

    @property
    def name(self) -> str:
        return "demo/ordered"

    @property
    def clone_url(self) -> str:
        return self.bare

    @property
    def web_url(self) -> str:
        return "https://example.test"

    def commit_url(self, sha: str) -> str:
        return f"https://example.test/{sha}"


async def test_ordering(root: Path) -> None:
    print("\n=== ordering ===")

    work = root / "work"
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev"], work)

    shas = {}
    for label, when in [("one", "2026-08-01T10:00:00+00:00"),
                        ("two", "2026-08-02T10:00:00+00:00"),
                        ("three", "2026-08-03T10:00:00+00:00")]:
        (work / f"{label}.txt").write_text(label)
        git(["add", "-A"], work)
        git(["commit", "-qm", label], work, when)
        shas[label] = git(["rev-parse", "HEAD"], work)

    # A commit whose author date is deliberately older than its parent's — the
    # case where date order and ancestry order disagree.
    (work / "four.txt").write_text("four")
    git(["add", "-A"], work)
    git(["commit", "-qm", "four-backdated"], work, "2026-07-01T10:00:00+00:00")
    shas["four"] = git(["rev-parse", "HEAD"], work)

    git(["checkout", "-qb", "side", "HEAD~2"], work)
    (work / "side.txt").write_text("side")
    git(["add", "-A"], work)
    git(["commit", "-qm", "on side"], work, "2026-08-05T10:00:00+00:00")
    shas["side"] = git(["rev-parse", "HEAD"], work)
    git(["checkout", "-q", "main"], work)

    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [Fixture(bare=str(work))]
    settings.gcloud_token = "unused"
    settings.mirror_dir = root / "mirror"

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)

    async def order(text: str, **kw):
        return await ordering.order_commits(settings, gh, mirror, raw=text, **kw)

    result = await order(f"{shas['one']}\n{shas['three']}\n{shas['two']}")
    check("the repository is detected", result["repo"], "demo/ordered")
    check("the default branch is used", result["branch"], "main")
    check("ordered newest-first by ancestry",
          [c["title"] for c in result["commits"]], ["three", "two", "one"])
    check("exactly one commit is marked latest",
          [c["title"] for c in result["commits"] if c["is_latest"]], ["three"])
    check("ranks are 1..n", [c["rank"] for c in result["commits"]], [1, 2, 3])
    check("positions come from the branch",
          [c["position"] for c in result["commits"]], [3, 2, 1])
    check("ordering method is reported", result["ordered_by"], "ancestry")

    # Ancestry must win over a misleading author date.
    backdated = await order(f"{shas['three']}\n{shas['four']}")
    check("a backdated commit still sorts by ancestry",
          [c["title"] for c in backdated["commits"]], ["four-backdated", "three"])
    check("the newest by ancestry is marked latest",
          backdated["commits"][0]["title"], "four-backdated")
    check("the date/ancestry disagreement is flagged", backdated["date_order_differs"], True)

    agreeing = await order(f"{shas['one']}\n{shas['two']}")
    check("no flag when dates agree", agreeing["date_order_differs"], False)

    off_branch = await order(f"{shas['one']}\n{shas['side']}")
    check("a commit off the branch is excluded",
          [c["title"] for c in off_branch["commits"]], ["one"])
    check("and the reason names the branch",
          "not on branch" in off_branch["problems"][0]["reason"], True)

    on_side = await order(f"{shas['one']}\n{shas['side']}", branch="side")
    check("choosing that branch includes it",
          sorted(c["title"] for c in on_side["commits"]), ["on side", "one"])
    check("with no problems", on_side["problems"], [])

    missing = await order(f"{shas['one']}\n{'deadbeef' * 5}")
    check("an unknown commit is excluded", len(missing["commits"]), 1)
    check("and reported", "not in demo/ordered" in missing["problems"][0]["reason"], True)

    short = await order(f"{shas['one'][:7]}\n{shas['two'][:7]}")
    check("short hashes resolve", [c["sha"] for c in short["commits"]],
          [shas["two"], shas["one"]])

    for bad, why in [("", "empty input"), ("nothing here", "no hashes at all")]:
        try:
            await order(bad)
            check(f"{why} rejected", "accepted", "rejected")
        except ordering.OrderError:
            check(f"{why} rejected", "rejected", "rejected")

    try:
        await order(shas["one"], branch="no-such-branch")
        check("an unknown branch is rejected", "accepted", "rejected")
    except ordering.OrderError:
        check("an unknown branch is rejected", "rejected", "rejected")

    try:
        await order("\n".join([shas["one"][:7]] * 1) + "\n" + "\n".join(
            f"{i:07x}" for i in range(ordering.MAX_COMMITS + 5)
        ))
        check("an oversized list is rejected", "accepted", "rejected")
    except ordering.OrderError:
        check("an oversized list is rejected", "rejected", "rejected")

    await gh.aclose()


def main() -> int:
    test_parsing()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_ordering(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
