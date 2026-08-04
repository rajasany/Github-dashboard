"""On-disk cache of which files each commit touched.

A commit's file list never changes, so this is cached permanently — the expensive
part of folder tracking (one GitHub API call per commit) is paid once per commit
ever, not once per dashboard refresh.

Raw paths are stored rather than derived folder names, so changing
`folders.depth` or `folders.paths` in config takes effect immediately without
re-fetching anything.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

MAX_STORED_PATHS = 300  # matches GitHub's own per-commit file cap


class FileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commit_files (
                    key           TEXT PRIMARY KEY,
                    paths         TEXT NOT NULL,
                    files_changed INTEGER NOT NULL,
                    truncated     INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _get_many_sync(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        out: dict[str, dict] = {}
        with self._connect() as conn:
            # Chunked to stay under SQLite's variable limit.
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT key, paths, files_changed, truncated FROM commit_files WHERE key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for key, paths, files_changed, truncated in rows:
                    try:
                        parsed = json.loads(paths)
                    except json.JSONDecodeError:
                        continue
                    out[key] = {
                        "paths": parsed,
                        "files_changed": files_changed,
                        "truncated": bool(truncated),
                    }
        return out

    def _put_many_sync(self, rows: list[tuple[str, list[str], int, bool]]) -> None:
        if not rows:
            return
        payload = [
            (key, json.dumps(paths[:MAX_STORED_PATHS]), files_changed, int(truncated))
            for key, paths, files_changed, truncated in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO commit_files (key, paths, files_changed, truncated) VALUES (?, ?, ?, ?)",
                payload,
            )

    async def get_many(self, keys: list[str]) -> dict[str, dict]:
        return await asyncio.to_thread(self._get_many_sync, keys)

    async def put_many(self, rows: list[tuple[str, list[str], int, bool]]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put_many_sync, rows)

    async def count(self) -> int:
        def _count() -> int:
            with self._connect() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM commit_files").fetchone()[0])

        return await asyncio.to_thread(_count)
