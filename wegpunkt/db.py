"""SQLite-Speicher für Aktivitäten, Follower und Reaktionen aus dem Fediverse."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    sport            TEXT NOT NULL,
    started_at       TEXT,
    distance_m       REAL NOT NULL,
    duration_s       REAL,
    moving_s         REAL,
    elevation_gain_m REAL,
    track_json       TEXT NOT NULL,
    has_image        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followers (
    actor_url    TEXT PRIMARY KEY,
    inbox        TEXT NOT NULL,
    shared_inbox TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    ap_id         TEXT PRIMARY KEY,
    activity_id   TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,          -- like | announce | reply
    actor_url     TEXT NOT NULL,
    actor_name    TEXT NOT NULL,
    actor_handle  TEXT NOT NULL,
    actor_profile TEXT,
    content       TEXT NOT NULL DEFAULT '',
    url           TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS interactions_by_activity ON interactions(activity_id);

CREATE TABLE IF NOT EXISTS remote_actors (
    url        TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # Aktivitäten -----------------------------------------------------------

    def add_activity(self, a: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO activities (id, title, description, sport, started_at,
                   distance_m, duration_s, moving_s, elevation_gain_m, track_json,
                   has_image, created_at)
                   VALUES (:id, :title, :description, :sport, :started_at, :distance_m,
                   :duration_s, :moving_s, :elevation_gain_m, :track_json, :has_image,
                   :created_at)""",
                {**a, "track_json": json.dumps(a["track"]), "has_image": int(a["has_image"])},
            )

    @staticmethod
    def _activity(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        a = dict(row)
        a["track"] = json.loads(a.pop("track_json"))
        a["has_image"] = bool(a["has_image"])
        return a

    def get_activity(self, activity_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        return self._activity(row)

    def list_activities(
        self, limit: int = 50, offset: int = 0, sport: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM activities"
        params: list[Any] = []
        if sport:
            query += " WHERE sport = ?"
            params.append(sport)
        query += " ORDER BY COALESCE(started_at, created_at) DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._activity(r) for r in rows]  # type: ignore[misc]

    def activity_totals(self) -> dict[str, float]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(distance_m), 0) AS distance_m,"
                " COALESCE(SUM(elevation_gain_m), 0) AS elevation_gain_m FROM activities"
            ).fetchone()
        return dict(row)

    def delete_activity(self, activity_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        return cur.rowcount > 0

    # Follower --------------------------------------------------------------

    def add_follower(self, actor_url: str, inbox: str, shared_inbox: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO followers (actor_url, inbox, shared_inbox, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(actor_url) DO UPDATE SET inbox = excluded.inbox,
                   shared_inbox = excluded.shared_inbox""",
                (actor_url, inbox, shared_inbox, now_iso()),
            )

    def remove_follower(self, actor_url: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM followers WHERE actor_url = ?", (actor_url,))

    def list_followers(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM followers").fetchall()]

    def count_followers(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM followers").fetchone()[0]

    # Reaktionen ------------------------------------------------------------

    def add_interaction(self, i: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO interactions (ap_id, activity_id, kind, actor_url,
                   actor_name, actor_handle, actor_profile, content, url, created_at)
                   VALUES (:ap_id, :activity_id, :kind, :actor_url, :actor_name,
                   :actor_handle, :actor_profile, :content, :url, :created_at)""",
                {"content": "", "url": None, "actor_profile": None, "created_at": now_iso(), **i},
            )

    def remove_interaction(self, ap_id: str, actor_url: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM interactions WHERE ap_id = ? AND actor_url = ?", (ap_id, actor_url)
            )

    def remove_interactions_by_actor(self, actor_url: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM interactions WHERE actor_url = ?", (actor_url,))

    def list_interactions(self, activity_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM interactions WHERE activity_id = ? ORDER BY created_at",
                (activity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # Cache für fremde Accounts ----------------------------------------------

    def get_remote_actor(self, url: str, max_age_s: float) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data, fetched_at FROM remote_actors WHERE url = ?", (url,)
            ).fetchone()
        if row is None or time.time() - row["fetched_at"] > max_age_s:
            return None
        return json.loads(row["data"])

    def put_remote_actor(self, url: str, data: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO remote_actors (url, data, fetched_at) VALUES (?, ?, ?)",
                (url, json.dumps(data), time.time()),
            )
