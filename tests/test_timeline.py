"""The folder timeline: window resolution, folder scoping and ordering.

Runs against a local git repository so the whole path — collect commits, derive
folders, filter, order, bucket — is exercised without a network call.

Run with:  .venv/bin/python tests/test_timeline.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import csr as csr_provider  # noqa: E402
from app import timeline as timeline_view  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.github import GitHubClient  # noqa: E402
from app.store import FileStore  # noqa: E402

FAILURES: list[str] = []
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def check(name, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def git(args, cwd=None, when=None) -> str:
    env = {**ENV, "HOME": str(cwd or "/tmp")}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, env=env).stdout.strip()


@dataclass(frozen=True)
class Repo:
    path: str
    project: str = "demo"
    repo: str = "svc"

    @property
    def key(self) -> str:
        return "csr:demo/svc"

    @property
    def name(self) -> str:
        return "demo/svc"

    @property
    def clone_url(self) -> str:
        return self.path

    @property
    def web_url(self) -> str:
        return "https://example.test"

    def commit_url(self, sha: str) -> str:
        return f"https://example.test/{sha}"


async def run(root: Path) -> None:
    work = root / "src"
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev One"], work)

    # Three days of work across two folders, at known times.
    plan = [
        ("api", "a1.py", "api: first", "2026-09-01T09:00:00+00:00"),
        ("api", "a2.py", "api: second", "2026-09-01T17:30:00+00:00"),
        ("web", "w1.js", "web: only here", "2026-09-02T11:00:00+00:00"),
        ("api", "a3.py", "api: third", "2026-09-05T08:15:00+00:00"),
    ]
    for folder, filename, message, when in plan:
        (work / folder).mkdir(exist_ok=True)
        (work / folder / filename).write_text("x\n")
        git(["add", "-A"], work)
        git(["commit", "-qm", message], work, when)

    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [Repo(path=str(work))]
    settings.gcloud_token = "unused-for-local-paths"
    settings.mirror_dir = root / "mirror"
    settings.store_path = root / "store.sqlite3"
    settings.folder_depth = 1
    settings.folders_enabled = True
    # The default config restricts branches; the timeline must say so rather
    # than calling a filtered view "all branches".
    settings.branch_include = ["main", "dev"]
    settings.branch_exclude = ["dependabot/*"]

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)
    store = FileStore(settings.store_path)
    key = "csr:demo/svc"

    def at(text: str) -> datetime:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    async def build(**kw):
        params = dict(repo_key=key, branch="main", folder="",
                      since_dt=at("2026-08-01T00:00:00"), until_dt=None, limit=50)
        params.update(kw)
        return await timeline_view.build_timeline(settings, gh, mirror, store, **params)

    print("=== the window ===")
    whole = await build()
    check("every commit in a wide window", whole["count"], 4)
    check("newest first",
          [c["title"] for c in whole["commits"]],
          ["api: third", "web: only here", "api: second", "api: first"])
    check("each carries a short sha", len(whole["commits"][0]["short"]), 10)
    check("and the branch it was found on", whole["commits"][0]["branches"], ["main"])

    tight = await build(since_dt=at("2026-09-01T12:00:00"), until_dt=at("2026-09-02T23:59:59"))
    check("the window cuts to the hour, not the day",
          [c["title"] for c in tight["commits"]], ["web: only here", "api: second"])
    check("a morning commit is excluded by an afternoon start",
          any(c["title"] == "api: first" for c in tight["commits"]), False)

    none = await build(since_dt=at("2026-10-01T00:00:00"))
    check("a window with nothing in it is empty, not an error", none["count"], 0)
    check("and still reports its window", none["since"][:10], "2026-10-01")

    print("\n=== folders ===")
    api_only = await build(folder="api")
    check("one folder narrows the list", api_only["count"], 3)
    check("to that folder's commits",
          [c["title"] for c in api_only["commits"]],
          ["api: third", "api: second", "api: first"])
    check("the folder picker still offers every folder",
          api_only["folders_available"], ["api", "web"])
    check("which is the point: narrowing must not empty the thing you narrowed with",
          "web" in api_only["folders_available"], True)
    check("an unknown folder matches nothing", (await build(folder="nope"))["count"], 0)
    check("blank means all folders", (await build(folder=""))["count"], 4)

    print("\n=== what each entry carries ===")
    first = api_only["commits"][0]
    check("the author", first["author"], "Dev One")
    check("the folders it touched", first["folders"], ["api"])
    check("a file count", first["files_changed"] >= 1, True)
    check("a link", first["url"].startswith("https://example.test/"), True)
    check("and an ISO timestamp", first["date"].endswith("Z") or "+" in first["date"], True)

    print("\n=== the summary counts ===")
    check("authors are counted", whole["authors"], [{"name": "Dev One", "commits": 4}])
    check("files are totalled", whole["files_changed"] >= 4, True)
    check("nothing is tagged here", whole["tagged"], 0)
    check("nor cherry-picked", whole["cherry_picks"], 0)
    check("a named branch says so", whole["all_branches"], False)

    print("\n=== every branch ===")
    git(["checkout", "-qb", "dev"], work)
    (work / "api" / "a4.py").write_text("y\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "api: on a branch"], work, "2026-09-06T10:00:00+00:00")
    git(["checkout", "-q", "main"], work)

    everywhere = await build(branch="")
    check("all branches picks up the feature commit",
          any(c["title"] == "api: on a branch" for c in everywhere["commits"]), True)
    check("and reports itself as such", everywhere["all_branches"], True)
    check("a commit names the branches holding it",
          sorted(next(c for c in everywhere["commits"] if c["title"] == "api: first")["branches"]),
          ["dev", "main"])
    check("while one branch sees only its own",
          any(c["title"] == "api: on a branch"
              for c in (await build(branch="main"))["commits"]), False)

    check("the branch filter is reported",
          everywhere["branch_filter"]["filtered"], True)
    check("naming what it allows", everywhere["branch_filter"]["include"], ["main", "dev"])

    settings.branch_include = []
    settings.branch_exclude = []
    check("with no filter it says so",
          (await build(branch=""))["branch_filter"]["filtered"], False)
    settings.branch_include = ["main", "dev"]

    print("\n=== cherry-picks ===")
    # A real one: -x records the source commit in the message.
    git(["checkout", "-q", "main"], work)
    (work / "api" / "fix.py").write_text("fix\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "api: the original fix"], work, "2026-09-07T09:00:00+00:00")
    source = git(["rev-parse", "HEAD"], work)

    git(["checkout", "-q", "-b", "release"], work)
    git(["reset", "-q", "--hard", "HEAD~1"], work)
    git(["cherry-pick", "-x", source], work)
    git(["checkout", "-q", "main"], work)
    git(["branch", "-m", "release", "dev2"], work)

    picked = await build(branch="dev2", since_dt=at("2026-08-01T00:00:00"))
    marked = [c for c in picked["commits"] if c["cherry_pick"]["is_cherry_pick"]]
    check("a cherry-picked commit is marked", len(marked), 1)
    check("with the source git recorded", marked[0]["cherry_pick"]["source_sha"], source.lower())
    check("as recorded, not merely mentioned",
          marked[0]["cherry_pick"]["evidence"], "recorded")
    check("and it is counted", picked["cherry_picks"], 1)
    check("while the original is not marked",
          any(c["cherry_pick"]["is_cherry_pick"] for c in picked["commits"]
              if c["sha"] == source), False)

    # A message that merely says "cherry picked" proves nothing.
    (work / "api" / "claim.py").write_text("c\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "api: cherry picked from somewhere"], work,
        "2026-09-08T09:00:00+00:00")
    claimed = await build(branch="main", since_dt=at("2026-08-01T00:00:00"))
    weak = next(c for c in claimed["commits"] if c["title"].startswith("api: cherry picked"))
    check("a bare mention still counts as one", weak["cherry_pick"]["is_cherry_pick"], True)
    check("but is kept apart from a recorded one",
          weak["cherry_pick"]["evidence"], "mentioned")
    check("with no source to show", weak["cherry_pick"]["source_sha"], None)

    print("\n=== failures ===")
    try:
        await build(repo_key="csr:demo/nope", branch="")
        check("an unknown repository is refused", "accepted", "refused")
    except timeline_view.TimelineError:
        check("an unknown repository is refused", "refused", "refused")

    await gh.aclose()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
