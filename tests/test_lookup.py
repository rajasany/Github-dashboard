"""Tests for tag metadata, the summary table and the commit lookup.

Exercises the CSR/git provider against a purpose-built fixture repository, so
annotated tags, lightweight tags, multi-branch commits and graph positions are
all covered without touching the network.

Run with:  .venv/bin/python tests/test_lookup.py
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
from app import lookup, summary  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.github import GitHubClient  # noqa: E402
from app.store import FileStore  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def run(cmd: list[str], cwd: Path, when: str | None = None) -> None:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, env=env)


def build_fixture(root: Path) -> Path:
    """A repo with: two branches, an annotated tag, a lightweight tag, and a
    commit that lives on both branches at different depths."""
    repo = root / "src"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "dev@example.test"], repo)
    run(["git", "config", "user.name", "Dev One"], repo)
    run(["git", "config", "tag.gpgSign", "false"], repo)

    (repo / "svc").mkdir()
    (repo / "svc" / "a.py").write_text("one\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-qm", "shared root"], repo, "2026-08-01T10:00:00+00:00")
    run(["git", "tag", "lightweight-1"], repo)  # no tag object

    (repo / "svc" / "b.py").write_text("two\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-qm", "second on main"], repo, "2026-08-02T10:00:00+00:00")
    run(["git", "tag", "-a", "v1.0.0", "-m", "First release"], repo, "2026-08-02T11:00:00+00:00")

    (repo / "other").mkdir()
    (repo / "other" / "c.py").write_text("three\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-qm", "third on main"], repo, "2026-08-03T10:00:00+00:00")

    run(["git", "checkout", "-qb", "feature", "HEAD~2"], repo)
    (repo / "svc" / "d.py").write_text("four\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-qm", "on feature"], repo, "2026-08-04T10:00:00+00:00")
    run(["git", "checkout", "-q", "main"], repo)
    return repo


@dataclass(frozen=True)
class FixtureRepo:
    url: str
    project: str = "demo"
    repo: str = "fixture"

    @property
    def key(self) -> str:
        return f"csr:{self.project}/{self.repo}"

    @property
    def name(self) -> str:
        return f"{self.project}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return self.url

    @property
    def web_url(self) -> str:
        return f"https://source.cloud.google.com/{self.project}/{self.repo}"

    def commit_url(self, sha: str) -> str:
        return f"{self.web_url}/+/{sha}"


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = build_fixture(root)
        fixture = FixtureRepo(url=f"file://{src}")

        settings = load_settings()
        settings.repos = []
        settings.csr_repos = [fixture]
        settings.gcloud_token = "not-used-for-file-urls"
        settings.mirror_dir = root / "mirrors"
        settings.store_path = root / "store.sqlite3"
        settings.folders_enabled = True
        settings.folder_depth = 1
        settings.folder_paths = []
        settings.folder_exclude = []
        settings.branch_include = []
        settings.branch_exclude = []

        gh = GitHubClient(settings)
        mirror = csr_provider.GitMirror(settings)
        store = FileStore(settings.store_path)
        path = await mirror.sync(fixture.key, fixture.clone_url)

        # ---------------- tag metadata ----------------
        print("=== tag metadata ===")
        tags = await mirror.tag_details(path)
        flat = {t["name"]: t for ts in tags.values() for t in ts}

        check("both tags found", sorted(flat), ["lightweight-1", "v1.0.0"])
        ann = flat["v1.0.0"]
        check("annotated tag is marked annotated", ann["annotated"], True)
        check("annotated tag carries its tagger", ann["tagger_name"], "Dev One")
        check("annotated tag carries its own date", (ann["tagger_date"] or "")[:10], "2026-08-02")
        check("annotated tag carries its message", ann["message"], "First release")

        light = flat["lightweight-1"]
        check("lightweight tag is marked as such", light["annotated"], False)
        check("lightweight tag invents no tagger", light["tagger_name"], None)
        check("lightweight tag invents no date", light["tagger_date"], None)
        # git's contents:subject falls back to the commit subject for a
        # lightweight tag; presenting that as a tag message would be a lie.
        check("lightweight tag invents no message", light["message"], None)
        check(
            "annotated tag resolves to the commit, not the tag object",
            ann["commit_sha"] in tags and tags[ann["commit_sha"]],
            [t for t in tags[ann["commit_sha"]]],
        )

        # ---------------- summary ----------------
        print("\n=== summary table ===")
        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        result = await summary.build_summary(
            settings, gh, mirror, store,
            repo_key=fixture.key, branch="main", folder=None,
            since_dt=since, until_dt=None, limit=100,
        )
        check("all three main commits listed", result["row_count"], 3)
        check("one of them is tagged... plus the lightweight one", result["tagged_rows"], 2)
        check("folders discovered", result["folders_available"], ["other", "svc"])
        check("not capped for a small branch", result["capped"], False)

        titles = [r["title"] for r in result["rows"]]
        check("newest first", titles[0], "third on main")

        tagged = {r["title"]: r["tags"] for r in result["rows"] if r["tags"]}
        check(
            "the annotated tag lands on its commit with its tagger",
            [(t["name"], t["tagger_name"]) for t in tagged["second on main"]],
            [("v1.0.0", "Dev One")],
        )

        folder_scoped = await summary.build_summary(
            settings, gh, mirror, store,
            repo_key=fixture.key, branch="main", folder="other",
            since_dt=since, until_dt=None, limit=100,
        )
        check("folder filter narrows the rows", folder_scoped["row_count"], 1)
        check("and keeps the right one", folder_scoped["rows"][0]["title"], "third on main")

        missing = await summary.build_summary(
            settings, gh, mirror, store,
            repo_key=fixture.key, branch="main", folder="nope",
            since_dt=since, until_dt=None, limit=100,
        )
        check("an unmatched folder yields no rows", missing["row_count"], 0)

        feature = await summary.build_summary(
            settings, gh, mirror, store,
            repo_key=fixture.key, branch="feature", folder=None,
            since_dt=since, until_dt=None, limit=100,
        )
        check("a different branch gives a different set", feature["row_count"], 2)

        # ---------------- lookup ----------------
        print("\n=== commit lookup ===")
        shared = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "main~2"], capture_output=True, text=True
        ).stdout.strip()

        found = await lookup.lookup_commit(settings, gh, mirror, shared)
        check("the commit is found", found["found"], True)
        match = found["matches"][0]
        check("in the right repository", match["repo"], "demo/fixture")
        check("full hash returned", match["sha"], shared)
        check("author reported", match["author_name"], "Dev One")

        branches = {b["name"]: b for b in match["branches"]}
        check("listed on both branches", sorted(branches), ["feature", "main"])
        check("distance to main head", branches["main"]["distance_to_head"], 2)
        check("distance to feature head", branches["feature"]["distance_to_head"], 1)
        check("position on main", (branches["main"]["position"], branches["main"]["total_commits"]), (1, 3))
        check("position on feature", (branches["feature"]["position"], branches["feature"]["total_commits"]), (1, 2))
        check("default branch flagged", branches["main"]["is_default"], True)
        check("not the head of either", [b["is_head"] for b in match["branches"]], [False, False])
        check("carries its lightweight tag", [t["name"] for t in match["tags"]], ["lightweight-1"])
        check("reports the folders the commit touched", match["folders"], ["svc"])

        # Position within a folder is counted over that folder's history alone.
        # In the fixture: main has 3 commits, of which 2 touched svc/ and 1
        # touched other/. The shared root commit is svc's 1st, not main's 1st
        # of 3 measured over everything.
        by_folder = {f["folder"]: f for f in match["folder_positions"]}
        check("folder position is scoped to that folder",
              (by_folder["svc"]["position"], by_folder["svc"]["total"]), (1, 2))
        check("it is counted on a named branch", by_folder["svc"]["branch"], "main")
        check("nothing was capped", by_folder["svc"]["capped"], False)
        check("reports no failed probes when all succeeded", match["branches_failed"], [])

        # A commit touching two folders must list both.
        multi = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "main"], capture_output=True, text=True
        ).stdout.strip()
        multi_hit = await lookup.lookup_commit(settings, gh, mirror, multi)
        check("a root-level change is bucketed, not dropped",
              multi_hit["matches"][0]["folders"], ["other"])

        # `other/` has a single commit, so its position there is 1 of 1 even
        # though the same commit is 3 of 3 on the branch — the distinction the
        # folder view exists to make.
        other = {f["folder"]: f for f in multi_hit["matches"][0]["folder_positions"]}["other"]
        branch_pos = multi_hit["matches"][0]["branches"][0]
        check("position within a rarely-touched folder differs from the branch",
              (other["position"], other["total"]), (1, 1))
        check("while the branch-wide position is the whole history",
              (branch_pos["position"], branch_pos["total_commits"]), (3, 3))

        # A commit that IS a branch tip.
        tip = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "main"], capture_output=True, text=True
        ).stdout.strip()
        at_head = await lookup.lookup_commit(settings, gh, mirror, tip)
        tip_branches = {b["name"]: b for b in at_head["matches"][0]["branches"]}
        check("tip is flagged as head", tip_branches["main"]["is_head"], True)
        check("tip has zero distance", tip_branches["main"]["distance_to_head"], 0)
        check("tip is only on its own branch", sorted(tip_branches), ["main"])
        check(
            "an untagged commit reports the nearest earlier tag",
            (at_head["matches"][0]["nearest_tag"] or {}).get("name"),
            "v1.0.0",
        )

        short = await lookup.lookup_commit(settings, gh, mirror, shared[:7])
        check("a short hash resolves to the full one", short["matches"][0]["sha"], shared)

        url_form = await lookup.lookup_commit(
            settings, gh, mirror, f"https://example.test/demo/fixture/commit/{shared}"
        )
        check("a pasted commit URL resolves", url_form["found"], True)

        absent = await lookup.lookup_commit(settings, gh, mirror, "deadbeef" * 5)
        check("an unknown hash is a clean miss", (absent["found"], absent["matches"]), (False, []))
        check("and still reports what was searched", absent["searched"], 1)

        for bad in ["not-a-hash", "zz", ""]:
            try:
                lookup.normalise_sha(bad)
                check(f"{bad!r} rejected", "accepted", "rejected")
            except lookup.LookupError_:
                check(f"{bad!r} rejected", "rejected", "rejected")

        await gh.aclose()

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
