"""Tests for changed-path -> owning-folder attribution.

Run with:  .venv/bin/python tests/test_paths.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paths import ROOT_LABEL, derive_folders  # noqa: E402

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

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
