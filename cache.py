#!/usr/bin/env python3
"""
Persistent TTL cache for parsed profiles, backed by a local SQLite file.

Why not a dict: an in-process cache dies with the process and is duplicated
per uvicorn worker, so a restart or a second worker means re-scraping every
slug — the expensive, rate-limited part. A SQLite file survives restarts and
is shared by every worker on the box (WAL mode gives us concurrent readers
alongside a writer). Point CACHE_DB at a mounted volume to survive redeploys.

Entries expire two ways: lazily, because a read ignores anything older than
the TTL, and eagerly, because the server sweeps expired rows on a timer so
the file doesn't grow without bound.

Every function here is blocking; callers on the event loop should wrap them
in asyncio.to_thread.
"""

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    slug       TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS profiles_fetched_at ON profiles (fetched_at);
"""


class ProfileCache:
    """A slug -> parsed-profile store that forgets entries older than `ttl`."""

    def __init__(self, path: str, ttl: float):
        self.path = path
        self.ttl = ttl
        self._init_db()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per call: sqlite3 objects aren't shareable across
        # threads, and connecting to an already-open file is cheap.
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def get(self, slug: str) -> dict | None:
        if not self.enabled:
            return None
        cutoff = time.time() - self.ttl
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM profiles WHERE slug = ? AND fetched_at > ?",
                (slug, cutoff),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            # Corrupt row (partial write, schema change); treat it as a miss.
            self.delete(slug)
            return None

    def set(self, slug: str, payload: dict, fetched_at: float | None = None) -> None:
        if not self.enabled:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO profiles (slug, fetched_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET fetched_at = excluded.fetched_at, "
                "payload = excluded.payload",
                (slug, fetched_at if fetched_at is not None else time.time(), json.dumps(payload)),
            )

    def delete(self, slug: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM profiles WHERE slug = ?", (slug,))

    def purge_expired(self) -> int:
        """Drop rows past the TTL. Returns how many were removed."""
        if not self.enabled:
            return 0
        cutoff = time.time() - self.ttl
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE fetched_at <= ?", (cutoff,))
            return cur.rowcount

    def count(self) -> int:
        """Live (unexpired) entries."""
        cutoff = time.time() - self.ttl if self.enabled else time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM profiles WHERE fetched_at > ?", (cutoff,)
            ).fetchone()
        return row[0]
