"""Tests for staging and pushing tags.

The push path is exercised against a local bare repository acting as the remote,
so the whole stage → confirm → push cycle is covered without touching anything
on the network.

Run with:  .venv/bin/python tests/test_tagging.py
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
from app import tagging  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.identity import resolve_tagger  # noqa: E402
from app.github import GitHubClient  # noqa: E402

FAILURES: list[str] = []
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def git(args: list[str], cwd: Path | None = None) -> str:
    env = {**ENV, "HOME": str(cwd or Path("/tmp"))}
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    return out.stdout.strip()


def test_names() -> None:
    print("=== tag name validation ===")
    for good in ["v1.0.0", "release-2026-08", "rc1", "team/v2", "v1.0.0-beta.1"]:
        try:
            check(f"{good!r} accepted", tagging.validate_tag_name(good), good)
        except tagging.TagError as exc:
            check(f"{good!r} accepted", f"rejected: {exc}", good)

    for bad, why in [
        ("", "empty"),
        ("   ", "whitespace only"),
        ("my tag", "contains a space"),
        ("-lead", "leading dash"),
        ("trail.", "trailing dot"),
        ("a..b", "double dot"),
        ("a~b", "tilde"),
        ("a^b", "caret"),
        ("a:b", "colon"),
        ("a?b", "question mark"),
        ("a[b", "bracket"),
        ("a\\b", "backslash"),
        ("x.lock", "reserved .lock suffix"),
        ("a//b", "double slash"),
        ("/lead", "leading slash"),
    ]:
        try:
            tagging.validate_tag_name(bad)
            check(f"{why} rejected", "accepted", "rejected")
        except tagging.TagError:
            check(f"{why} rejected", "rejected", "rejected")

    check("surrounding whitespace is trimmed", tagging.validate_tag_name("  v2.0  "), "v2.0")


@dataclass(frozen=True)
class Fixture:
    bare: str

    project: str = "demo"
    repo: str = "tagged"

    @property
    def key(self) -> str:
        return "csr:demo/tagged"

    @property
    def name(self) -> str:
        return "demo/tagged"

    @property
    def clone_url(self) -> str:
        return self.bare

    @property
    def web_url(self) -> str:
        return "https://example.test"

    def commit_url(self, sha: str) -> str:
        return f"https://example.test/{sha}"


async def test_flow(root: Path) -> None:
    print("\n=== stage → push ===")

    bare = root / "remote.git"
    work = root / "work"
    git(["init", "-q", "--bare", str(bare)])
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev"], work)
    (work / "a.txt").write_text("one\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "first"], work)
    git(["remote", "add", "origin", str(bare)], work)
    git(["push", "-q", "origin", "main"], work)
    # A second branch, to prove a tag push cannot disturb other refs.
    git(["checkout", "-qb", "keepme"], work)
    (work / "b.txt").write_text("two\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "second"], work)
    git(["push", "-q", "origin", "keepme"], work)

    fixture = Fixture(bare=str(bare))
    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [fixture]
    settings.gcloud_token = "unused-for-local-paths"
    settings.mirror_dir = root / "mirror"
    settings.tag_store_path = root / "tags.sqlite3"
    settings.tagger_name = "Release Bot"
    settings.tagger_email = "bot@example.test"

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)
    store = tagging.TagStore(settings.tag_store_path)

    path = await mirror.sync(fixture.key, fixture.clone_url)
    sha = git(["-C", str(path), "rev-parse", "main"])

    refs = lambda: sorted(git(["--git-dir", str(bare), "for-each-ref", "--format=%(refname)"]).split())  # noqa: E731
    before = refs()
    check("remote starts with two branches and no tags", before,
          ["refs/heads/keepme", "refs/heads/main"])

    staged = await tagging.stage_tag(
        settings, gh, mirror, store,
        repo_key=fixture.key, sha=sha, name="v1.0.0", message="First release",
    )
    check("staging records the tag", (staged["name"], staged["pushed"]), ("v1.0.0", False))
    check("staging resolves the full sha", staged["sha"], sha)
    check("staging captures the commit subject", staged["commit_title"], "first")
    check("STAGING TOUCHES NOTHING ON THE REMOTE", refs(), before)

    check("a staged tag is listed", [t["name"] for t in store.list()], ["v1.0.0"])
    check("listing can be scoped by repo", len(store.list(fixture.key)), 1)
    check("and returns nothing for another repo", store.list("github:x/y"), [])

    try:
        await tagging.stage_tag(settings, gh, mirror, store,
                                repo_key=fixture.key, sha=sha, name="v1.0.0", message="dup")
        check("staging the same name twice is refused", "accepted", "refused")
    except tagging.TagError:
        check("staging the same name twice is refused", "refused", "refused")

    try:
        await tagging.stage_tag(settings, gh, mirror, store,
                                repo_key=fixture.key, sha="deadbeef" * 5, name="v9", message="")
        check("staging on a missing commit is refused", "accepted", "refused")
    except tagging.TagError:
        check("staging on a missing commit is refused", "refused", "refused")

    pushed = await tagging.push_tag(settings, gh, mirror, store, staged["id"])
    check("push marks the row pushed", pushed["pushed"], True)
    check("push records no error", pushed["push_error"], None)

    after = refs()
    check("the tag now exists on the remote", after,
          ["refs/heads/keepme", "refs/heads/main", "refs/tags/v1.0.0"])
    check("PUSHING ONE TAG DOES NOT DISTURB THE BRANCHES",
          [r for r in after if r.startswith("refs/heads/")], before)

    meta = git(["--git-dir", str(bare), "for-each-ref",
                "--format=%(objecttype)|%(taggername)|%(contents:subject)", "refs/tags/v1.0.0"])
    kind, tagger, subject = meta.split("|")
    check("the tag is annotated, not lightweight", kind, "tag")
    check("the configured tagger is recorded", tagger, "Release Bot")
    check("the comment becomes the tag message", subject, "First release")

    try:
        await tagging.push_tag(settings, gh, mirror, store, staged["id"])
        check("pushing twice is refused", "accepted", "refused")
    except tagging.TagError:
        check("pushing twice is refused", "refused", "refused")

    try:
        await tagging.stage_tag(settings, gh, mirror, store,
                                repo_key=fixture.key, sha=sha, name="v1.0.0", message="")
        check("staging a name that now exists remotely is refused", "accepted", "refused")
    except tagging.TagError:
        check("staging a name that now exists remotely is refused", "refused", "refused")

    # Discarding only affects the local staging record.
    second = await tagging.stage_tag(settings, gh, mirror, store,
                                     repo_key=fixture.key, sha=sha, name="v2.0.0", message="")
    check("a second tag can be staged", second["name"], "v2.0.0")
    check("discarding removes it locally", store.delete(second["id"]), True)
    check("and it never reached the remote", refs(), after)
    check("discarding an unknown id is a no-op", store.delete(99999), False)

    print("\n=== overview ===")
    overview = await tagging.tag_overview(settings, gh, mirror, store, lambda commits: None)
    repo_entry = overview["repos"][0]
    check("the pushed tag appears in the overview", [t["name"] for t in repo_entry["tags"]], ["v1.0.0"])
    check("with its folders derived", repo_entry["tags"][0]["folders"], ["(repo root)"])
    check("and its tagger", repo_entry["tags"][0]["tagger_name"], "Release Bot")
    check("the total counts it", overview["total"], 1)

    print("\n=== per-repo view: which branches hold each tag ===")
    per_repo = await tagging.tags_for_repo(
        settings, gh, mirror, store, fixture.key, lambda commits: None
    )
    check("scoped to the one repository", per_repo["repo"], "demo/tagged")
    check("lists that repo's tags", [t["name"] for t in per_repo["tags"]], ["v1.0.0"])
    check("reports every branch in the repo", sorted(per_repo["branches_known"]), ["keepme", "main"])
    check("names the default branch", per_repo["default_branch"], "main")

    # The tag sits on the first commit, which both branches descend from, so both
    # contain it — a tag belongs to a commit, not to one branch.
    check("both containing branches are listed", sorted(per_repo["tags"][0]["branches"]),
          ["keepme", "main"])
    check("the default branch is listed first", per_repo["tags"][0]["branches"][0], "main")
    check("nothing was capped for a small repo", per_repo["capped"], {"tags": 0, "branches": 0})
    check("staged tags for this repo travel with it", isinstance(per_repo["staged"], list), True)

    # A tag on a commit that only one branch can reach.
    only_keepme = git(["-C", str(path), "rev-parse", "keepme"])
    solo = await tagging.stage_tag(settings, gh, mirror, store,
                                   repo_key=fixture.key, sha=only_keepme, name="v3.0.0", message="")
    await tagging.push_tag(settings, gh, mirror, store, solo["id"])
    per_repo = await tagging.tags_for_repo(
        settings, gh, mirror, store, fixture.key, lambda commits: None
    )
    holders = {t["name"]: t["branches"] for t in per_repo["tags"]}
    check("a branch-specific tag lists only that branch", holders["v3.0.0"], ["keepme"])
    check("the shared tag still lists both", sorted(holders["v1.0.0"]), ["keepme", "main"])

    try:
        await tagging.tags_for_repo(settings, gh, mirror, store, "csr:no/such", lambda c: None)
        check("an unknown repo is rejected", "accepted", "rejected")
    except tagging.TagError:
        check("an unknown repo is rejected", "rejected", "rejected")

    await gh.aclose()


async def test_identity(root: Path) -> None:
    """With no TAGGER_* set, a CSR tag must be attributed to the signed-in
    gcloud account rather than to the dashboard."""
    print("\n=== tagger identity ===")

    bare = root / "id-remote.git"
    work = root / "id-work"
    git(["init", "-q", "--bare", str(bare)])
    git(["init", "-q", "-b", "main", str(work)])
    git(["config", "user.email", "d@e.test"], work)
    git(["config", "user.name", "Dev"], work)
    (work / "a.txt").write_text("one\n")
    git(["add", "-A"], work)
    git(["commit", "-qm", "first"], work)
    git(["remote", "add", "origin", str(bare)], work)
    git(["push", "-q", "origin", "main"], work)

    fixture = Fixture(bare=str(bare))
    fixture.__class__.repo = "identity"
    settings = load_settings()
    settings.repos = []
    settings.csr_repos = [fixture]
    settings.gcloud_token = "unused-for-local-paths"
    settings.mirror_dir = root / "id-mirror"
    settings.tag_store_path = root / "id-tags.sqlite3"
    # Deliberately unset: this is the reported bug's configuration.
    settings.tagger_name = ""
    settings.tagger_email = ""

    gh = GitHubClient(settings)
    mirror = csr_provider.GitMirror(settings)
    store = tagging.TagStore(settings.tag_store_path)

    # Stand in for a signed-in gcloud session.
    mirror.tokens._account = "person@example.com"

    who = await resolve_tagger(settings, "csr", mirror=mirror)
    check("the gcloud account supplies the email", who["email"], "person@example.com")
    check("and the name, verbatim from the local part", who["name"], "person")
    check("the source is reported as gcloud", who["source"], "gcloud")

    path = await mirror.sync(fixture.key, fixture.clone_url)
    sha = git(["-C", str(path), "rev-parse", "main"])

    staged = await tagging.stage_tag(settings, gh, mirror, store,
                                     repo_key=fixture.key, sha=sha, name="v9.0.0", message="ident")
    check("staging shows who it will be attributed to", staged["tagger"], "person")

    await tagging.push_tag(settings, gh, mirror, store, staged["id"])
    meta = git(["--git-dir", str(bare), "for-each-ref",
                "--format=%(taggername)|%(taggeremail)", "refs/tags/v9.0.0"])
    name, email = meta.split("|")
    check("THE PUSHED TAG CARRIES THE SIGNED-IN PERSON", name, "person")
    check("with their address", email, "<person@example.com>")
    check("and not the dashboard", name == "Repo Change Dashboard", False)

    # No account signed in at all: fall back, but say so.
    mirror.tokens._account = ""
    bare_who = await resolve_tagger(settings, "csr", mirror=mirror)
    check("with nobody signed in it falls back", bare_who["source"], "fallback")
    check("and names the dashboard, not a person", bare_who["name"], "Repo Change Dashboard")

    await gh.aclose()


def main() -> int:
    test_names()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_flow(Path(tmp)))
        asyncio.run(test_identity(Path(tmp)))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
