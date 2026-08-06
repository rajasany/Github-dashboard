"""Tests for branch comparison.

The CSR half runs against a purpose-built local git repo with two genuinely
diverged branches, so the three-dot semantics are exercised for real rather
than mocked.

Run with:  .venv/bin/python tests/test_compare.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compare  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.csr import GitMirror  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def build_repo(root: Path) -> Path:
    """A repo where `main` and `feature/pay` have each moved on independently."""
    repo = root / "cmprepo"
    repo.mkdir()

    def git(*args: str, **env_extra: str) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Dev One", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "Dev One", "GIT_COMMITTER_EMAIL": "t@e.com", **env_extra}
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "services" / "auth").mkdir(parents=True)
    (repo / "services" / "billing").mkdir(parents=True)

    stamp = {"GIT_AUTHOR_DATE": "2026-08-01T10:00:00+00:00", "GIT_COMMITTER_DATE": "2026-08-01T10:00:00+00:00"}
    (repo / "services/auth/main.py").write_text("a\nb\nc\n")
    (repo / "README.md").write_text("base\n")
    git("add", "-A")
    git("commit", "-qm", "shared base", **stamp)

    git("checkout", "-qb", "feature/pay")
    stamp = {"GIT_AUTHOR_DATE": "2026-08-02T10:00:00+00:00", "GIT_COMMITTER_DATE": "2026-08-02T10:00:00+00:00"}
    (repo / "services/billing/pay.go").write_text("x\ny\n")
    git("add", "-A")
    git("commit", "-qm", "billing: add pay endpoint", **stamp)

    stamp = {"GIT_AUTHOR_DATE": "2026-08-03T10:00:00+00:00", "GIT_COMMITTER_DATE": "2026-08-03T10:00:00+00:00"}
    (repo / "services/auth/main.py").write_text("a\nB\nc\nd\n")
    git("rm", "-q", "README.md")
    git("add", "-A")
    git("commit", "-qm", "auth: tweak and drop readme", **stamp)

    git("checkout", "-q", "main")
    stamp = {"GIT_AUTHOR_DATE": "2026-08-02T12:00:00+00:00", "GIT_COMMITTER_DATE": "2026-08-02T12:00:00+00:00"}
    (repo / "MAINONLY.md").write_text("hello\n")
    git("add", "-A")
    git("commit", "-qm", "main-only change", **stamp)
    return repo


def test_helpers() -> None:
    print("=== status wording ===")
    check("ahead only", compare._status_word(3, 0), "ahead")
    check("behind only", compare._status_word(0, 2), "behind")
    check("both directions is diverged", compare._status_word(3, 2), "diverged")
    check("neither is identical", compare._status_word(0, 0), "identical")


async def run_csr(repo_path: Path, cache: Path) -> None:
    print("\n=== CSR / git comparison (real diverged repo) ===")

    @dataclass(frozen=True)
    class LocalRepo:
        project: str = "demo"
        repo: str = "cmprepo"

        @property
        def key(self): return f"csr:{self.project}/{self.repo}"
        @property
        def name(self): return f"{self.project}/{self.repo}"
        @property
        def clone_url(self): return f"file://{repo_path}"
        @property
        def web_url(self): return f"https://source.cloud.google.com/{self.project}/{self.repo}"
        def commit_url(self, sha): return f"{self.web_url}/+/{sha}"

    os.environ["GCLOUD_ACCESS_TOKEN"] = "unused-for-file-transport"
    os.environ["MIRROR_DIR"] = str(cache)
    settings = load_settings()
    settings.folder_paths = ["services/*"]
    mirror = GitMirror(settings)

    result = await compare.compare_csr(mirror, LocalRepo(), "main", "feature/pay", settings)

    check("status is diverged", result["status"], "diverged")
    check("two commits ahead on the feature branch", result["ahead_by"], 2)
    check("one commit behind (main moved on)", result["behind_by"], 1)
    check("merge base is resolved", bool(result["merge_base"]), True)

    # The crucial one: a three-dot diff must not report main's own file as a
    # deletion on the feature branch.
    paths = sorted(f["path"] for f in result["files"])
    check("diff is from the merge base, not from main's tip", paths, ["README.md", "services/auth/main.py", "services/billing/pay.go"])
    check("main-only file is absent from the diff", "MAINONLY.md" in paths, False)

    by_path = {f["path"]: f for f in result["files"]}
    check("added file detected", by_path["services/billing/pay.go"]["status"], "added")
    check("removed file detected", by_path["README.md"]["status"], "removed")
    check("modified file detected", by_path["services/auth/main.py"]["status"], "modified")
    check("line counts on the modified file", (by_path["services/auth/main.py"]["additions"], by_path["services/auth/main.py"]["deletions"]), (2, 1))

    check("commits are newest first", [c["title"] for c in result["commits"]],
          ["auth: tweak and drop readme", "billing: add pay endpoint"])
    check("each commit is tagged with the head branch", {b for c in result["commits"] for b in c["branches"]}, {"feature/pay"})
    check("per-commit folders derived from its own files",
          sorted(result["commits"][1]["folders"]), ["services/billing"])
    check("rollup folders span the whole diff",
          sorted(result["folders"]), ["(repo root)", "services/auth", "services/billing"])
    check("totals", (result["files_changed"], result["additions"], result["deletions"]), (3, 4, 2))

    # Reversing the direction must mirror ahead/behind.
    reverse = await compare.compare_csr(mirror, LocalRepo(), "feature/pay", "main", settings)
    check("reversing swaps ahead and behind", (reverse["ahead_by"], reverse["behind_by"]), (1, 2))
    check("reverse diff shows main's own file", "MAINONLY.md" in [f["path"] for f in reverse["files"]], True)

    # Identical refs.
    same = await compare.compare_csr(mirror, LocalRepo(), "main", "main", settings)
    check("a branch against itself is identical", same["status"], "identical")
    check("no files differ", same["files_changed"], 0)

    try:
        await compare.compare_csr(mirror, LocalRepo(), "main", "no-such-branch", settings)
        check("unknown branch rejected", "no error", "CompareError")
    except compare.CompareError as exc:
        check("unknown branch rejected", "no-such-branch" in str(exc), True)


def main() -> int:
    test_helpers()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = build_repo(root)
        asyncio.run(run_csr(repo, root / "mirrors"))
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
