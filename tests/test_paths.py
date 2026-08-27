"""Tests for changed-path -> owning-folder attribution.

Run with:  .venv/bin/python tests/test_paths.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paths import ROOT_LABEL, changed_directories, derive_folders  # noqa: E402

CASES: list[tuple[str, list[str], int, list[str], list[str], list[str]]] = [
    # label, paths, depth, patterns, exclude, expected
    ("top-level folders", ["auth/main.py", "billing/app.go"], 1, [], [], ["auth", "billing"]),
    (
        "shared prefix collapses without a pattern",
        ["services/auth/main.py", "services/billing/app.go"],
        1,
        [],
        [],
        ["services"],
    ),
    (
        "pattern splits the shared prefix",
        ["services/auth/main.py", "services/billing/app.go"],
        1,
        ["services/*"],
        [],
        ["services/auth", "services/billing"],
    ),
    ("depth 2 equals a one-level pattern", ["services/auth/main.py"], 2, [], [], ["services/auth"]),
    ("depth never swallows the filename", ["auth/main.py"], 5, [], [], ["auth"]),
    ("root files get their own bucket", ["README.md", "Makefile"], 1, [], [], [ROOT_LABEL]),
    (
        "multi-service commit yields every folder",
        ["services/auth/a.py", "shared/proto/b.proto", "README.md"],
        1,
        ["services/*"],
        [],
        [ROOT_LABEL, "services/auth", "shared"],
    ),
    ("deep path truncates to depth", ["a/b/c/d/e.py"], 2, [], [], ["a/b"]),
    (
        "exclude drops a nested noise folder but keeps its sibling",
        ["api/__pycache__/x.pyc", "api/main.py"],
        1,
        [],
        ["__pycache__"],
        ["api"],
    ),
    ("commit touching only excluded folders yields nothing", ["__pycache__/x.pyc"], 1, [], ["__pycache__"], []),
    (
        "exclude matches any segment",
        ["frontend/node_modules/lib/x.js", "frontend/src/a.ts"],
        1,
        [],
        ["node_modules"],
        ["frontend"],
    ),
    ("exclude does not over-match", ["backend/a.py"], 1, [], ["__pycache__"], ["backend"]),
    ("glob exclude", [".github/workflows/ci.yml", "api/x.py"], 1, [], [".*"], ["api"]),
    ("empty input", [], 1, [], [], []),
    ("blank and leading-slash paths", ["", "   ", "/x/y.py"], 1, [], [], ["x"]),
    ("patterns falls back to depth when unmatched", ["infra/k8s/a.yaml"], 1, ["services/*"], [], ["infra"]),
]


DIR_CASES: list[tuple[str, list[str], list[str], list[str]]] = [
    # label, paths, exclude, expected
    (
        "a commit confined to one deep folder names that folder, not its parent",
        ["app/static/app.js", "app/static/index.html"],
        [],
        ["app/static"],
    ),
    (
        "a commit spanning folders names each one",
        ["app/compare.py", "app/static/app.js", "tests/test_compare.py"],
        [],
        ["app", "app/static", "tests"],
    ),
    ("root-level files report the repo root", ["README.md"], [], [ROOT_LABEL]),
    (
        "the repo root sorts last, being the least specific",
        ["README.md", "app/x.py"],
        [],
        ["app", ROOT_LABEL],
    ),
    ("excluded directories are dropped", ["api/__pycache__/x.pyc", "api/main.py"], ["__pycache__"], ["api"]),
    ("the deepest directory is kept", ["a/b/c/d.py"], [], ["a/b/c"]),
    ("duplicates collapse", ["x/a.py", "x/b.py", "x/c.py"], [], ["x"]),
    ("empty input yields nothing", [], [], []),
    ("blank and slash-prefixed paths are tolerated", ["", "  ", "/x/y.py"], [], ["x"]),
]


def test_directories() -> None:
    print("\n=== exact directories (single-commit view) ===")
    failures = 0
    for label, paths, exclude, expected in DIR_CASES:
        got = changed_directories(paths, exclude)
        ok = got == expected
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        got  {got}\n        want {expected}")
            failures += 1

    # The two functions answer different questions and must not be conflated.
    paths = ["app/static/app.js"]
    exact = changed_directories(paths, [])
    rollup = derive_folders(paths, 1, [], [])
    ok = exact == ["app/static"] and rollup == ["app"]
    print(f"{'PASS' if ok else 'FAIL'}  the service rollup stays coarse while the directory stays exact")
    if not ok:
        print(f"        exact={exact} rollup={rollup}")
        failures += 1
    return failures


def main() -> int:
    failures = 0
    for label, paths, depth, patterns, exclude, expected in CASES:
        got = derive_folders(paths, depth, patterns, exclude)
        ok = got == expected
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        paths={paths} depth={depth} patterns={patterns} exclude={exclude}")
            print(f"        got  {got}\n        want {expected}")
            failures += 1

    dir_failures = test_directories()
    total = len(CASES) + len(DIR_CASES) + 1
    passed = total - failures - dir_failures
    print(f"\n{passed}/{total} passed")
    return 1 if (failures or dir_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
