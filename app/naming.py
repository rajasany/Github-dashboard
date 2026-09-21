"""Turn a user-supplied tag convention into a concrete tag name.

The convention is a template of literal text and {placeholders}, e.g.

    release/{repo_name}/{folder_slug}/{yyyy}.{mm}.{n:03}

`{n}` is the interesting one: it is resolved by looking at the tags that already
exist in the repository and taking the next free number, so a proposal is the
*next* tag rather than one that already exists.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

PLACEHOLDERS: list[tuple[str, str]] = [
    ("{repo}", "Full repository, e.g. rajasany/Insurance"),
    ("{repo_name}", "Repository without the owner, e.g. Insurance"),
    ("{owner}", "Owner or GCP project, e.g. rajasany"),
    ("{folder}", "Folder as written, e.g. app/static"),
    ("{folder_slug}", "Folder with / and spaces turned into -, e.g. app-static"),
    ("{branch}", "Branch name"),
    ("{branch_slug}", "Branch with / turned into -"),
    ("{sha}", "Full commit hash"),
    ("{sha7}", "Short commit hash"),
    ("{date}", "Commit date, YYYY-MM-DD"),
    ("{yyyy}", "Commit year"),
    ("{mm}", "Commit month, zero padded"),
    ("{dd}", "Commit day, zero padded"),
    ("{today}", "Today's date, YYYY-MM-DD"),
    ("{n}", "Next free sequence number for this pattern"),
    ("{n:03}", "…zero padded to a width, e.g. 007"),
]

# Permissive on purpose: names with digits (sha7) must match, and so must
# typos and wrong case, so `describe_unknown` can reject them instead of letting
# an unsubstituted "{sha7}" end up inside a real tag name.
_TOKEN = re.compile(r"\{([A-Za-z0-9_]+)(?::0?(\d+))?\}")


class NamingError(Exception):
    pass


def _slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", value or "")).strip("-")


def describe_unknown(template: str) -> list[str]:
    """Placeholders in the template that this renderer does not understand."""
    known = {name.strip("{}").split(":")[0] for name, _ in PLACEHOLDERS}
    return sorted({m.group(1) for m in _TOKEN.finditer(template or "") if m.group(1) not in known})


def render(
    template: str,
    *,
    repo: str,
    folder: str | None,
    branch: str | None,
    sha: str | None,
    commit_date: str | None,
    existing_tags: list[str] | None = None,
) -> str:
    """Fill a convention in. Raises NamingError on an unusable template."""
    if not (template or "").strip():
        raise NamingError("Give a tag convention, e.g. release/{repo_name}/{yyyy}.{mm}.{n:02}")

    unknown = describe_unknown(template)
    if unknown:
        raise NamingError(
            "Unknown placeholder(s): " + ", ".join(f"{{{u}}}" for u in unknown)
        )

    owner, _, repo_name = (repo or "").partition("/")
    if not repo_name:
        owner, repo_name = "", repo or ""

    when = None
    if commit_date:
        try:
            when = datetime.fromisoformat(str(commit_date).replace("Z", "+00:00"))
        except ValueError:
            when = None
    when = when or datetime.now(timezone.utc)
    today = datetime.now(timezone.utc)

    values = {
        "repo": repo or "",
        "repo_name": repo_name,
        "owner": owner,
        "folder": folder or "",
        "folder_slug": _slug(folder or ""),
        "branch": branch or "",
        "branch_slug": _slug(branch or ""),
        "sha": sha or "",
        "sha7": (sha or "")[:7],
        "date": when.strftime("%Y-%m-%d"),
        "yyyy": when.strftime("%Y"),
        "mm": when.strftime("%m"),
        "dd": when.strftime("%d"),
        "today": today.strftime("%Y-%m-%d"),
    }

    # Everything except {n} first, so the sequence is searched for against an
    # otherwise-final string.
    def fill(match: re.Match[str]) -> str:
        name, width = match.group(1), match.group(2)
        if name == "n":
            return match.group(0)  # left for the second pass
        text = values.get(name, "")
        return text.zfill(int(width)) if width and text.isdigit() else text

    partial = _TOKEN.sub(fill, template)

    if "{n" not in partial:
        return partial.strip()

    width_match = re.search(r"\{n(?::0?(\d+))?\}", partial)
    width = int(width_match.group(1)) if width_match and width_match.group(1) else 0
    number = next_sequence(partial, existing_tags or [], width)
    return re.sub(r"\{n(?::0?\d+)?\}", str(number).zfill(width), partial).strip()


def next_sequence(pattern: str, existing_tags: list[str], width: int = 0) -> int:
    """The lowest number that does not already exist for this pattern.

    The pattern is turned into a regex with a capture group where {n} sits, so
    only tags shaped like this convention are considered — an unrelated `v2.1`
    cannot inflate a `release/app/2026.09.NN` series.
    """
    placeholder = re.search(r"\{n(?::0?\d+)?\}", pattern)
    if not placeholder:
        return 1

    before = re.escape(pattern[: placeholder.start()])
    after = re.escape(pattern[placeholder.end() :])
    matcher = re.compile(f"^{before}(\\d+){after}$")

    used = {
        int(found.group(1))
        for tag in existing_tags
        if (found := matcher.match(tag or ""))
    }
    return max(used) + 1 if used else 1
