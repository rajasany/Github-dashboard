"""Maps changed file paths to the folder (microservice) that owns them."""

from __future__ import annotations

import fnmatch

ROOT_LABEL = "(repo root)"


def derive_folders(
    paths: list[str],
    depth: int,
    patterns: list[str],
    exclude: list[str] | None = None,
) -> list[str]:
    """Reduce changed file paths to the set of folders they belong to.

    `patterns` wins when set: a glob like ``services/*`` attributes
    ``services/auth/main.py`` to ``services/auth`` — the glob's segment count
    decides how deep to cut. Paths matching no pattern fall back to `depth`
    leading segments, so nothing is silently dropped.

    `exclude` globs drop derived folders that aren't services at all
    (``__pycache__``, ``node_modules``, ``.github``…). A commit that touched
    nothing else ends up with no folders, which the UI buckets separately rather
    than misattributing.

    Files directly in the repo root have no owning folder and collapse to
    ROOT_LABEL rather than being attributed to a service.
    """
    normalised = [p.strip().strip("/") for p in patterns]
    skip = [p.strip().strip("/") for p in (exclude or [])]
    out: set[str] = set()

    for raw in paths:
        path = (raw or "").strip().strip("/")
        if not path:
            continue

        segments = path.split("/")
        if len(segments) == 1:
            out.add(ROOT_LABEL)
            continue

        matched = None
        for pattern in normalised:
            width = len(pattern.split("/"))
            if len(segments) >= width:
                candidate = "/".join(segments[:width])
                if fnmatch.fnmatch(candidate, pattern):
                    matched = candidate
                    break

        if matched:
            out.add(matched)
            continue

        # Never let `depth` swallow the filename itself.
        take = min(depth, len(segments) - 1)
        out.add("/".join(segments[:take]) if take > 0 else ROOT_LABEL)

    if skip:
        out = {
            f
            for f in out
            # Match the whole folder or any leading segment, so "__pycache__"
            # also drops "api/__pycache__".
            if not any(
                fnmatch.fnmatch(f, pat) or any(fnmatch.fnmatch(s, pat) for s in f.split("/"))
                for pat in skip
            )
        }

    return sorted(out)


def changed_directories(paths: list[str], exclude: list[str]) -> list[str]:
    """The directories the changed files actually sit in.

    This is deliberately *not* `derive_folders`. That one rolls a path up to a
    service bucket (`app/static/app.js` -> `app`) so activity can be grouped;
    this one answers the different question "where was this commit made", and a
    commit confined to `app/static` should say `app/static`, not `app`.

    A parent appears only when a file sits directly in it, so `app` and
    `app/static` both showing means files were changed in both.
    """
    normalised = [p.strip().strip("/") for p in exclude]
    out: set[str] = set()

    for raw in paths:
        path = (raw or "").strip().strip("/")
        if not path:
            continue
        segments = path.split("/")
        directory = "/".join(segments[:-1])
        if not directory:
            out.add(ROOT_LABEL)
            continue
        if any(
            fnmatch.fnmatch(directory, pat) or any(fnmatch.fnmatch(s, pat) for s in directory.split("/"))
            for pat in normalised
        ):
            continue
        out.add(directory)

    # Root last: it is the least specific answer, so it should not lead.
    return sorted(out, key=lambda d: (d == ROOT_LABEL, d))
