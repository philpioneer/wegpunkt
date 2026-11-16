"""SQLite-Speicher: Aktivitäten, Follower, Reaktionen, Zustell-Warteschlange, Profil."""

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
    elevation_loss_m REAL,
    kind             TEXT NOT NULL DEFAULT 'gps',
    workout_json     TEXT,
    track_json       TEXT NOT NULL,
    has_image        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    visibility       TEXT NOT NULL DEFAULT 'public',
    extra_json       TEXT,
    updated_at       TEXT
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

-- Ausgehende Nachrichten, die noch zugestellt werden müssen
CREATE TABLE IF NOT EXISTS deliveries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox      TEXT NOT NULL,
    body       TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(next_at);

-- Profilangaben, die in der Oberfläche geändert wurden
CREATE TABLE IF NOT EXISTS profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Spalten, die ältere Datenbanken noch nicht haben
MIGRATIONS = (
    ("elevation_loss_m", "REAL"),
    ("kind", "TEXT NOT NULL DEFAULT 'gps'"),
    ("workout_json", "TEXT"),
    ("visibility", "TEXT NOT NULL DEFAULT 'public'"),
    ("extra_json", "TEXT"),
    ("updated_at", "TEXT"),
)

LIGHT_COLUMNS = (
    "id, title, sport, kind, visibility, started_at, created_at, distance_m, duration_s,"
    " moving_s, elevation_gain_m, elevation_loss_m, workout_json, extra_json"
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self._conn() as conn:
            self._pragmas(conn)
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _pragmas(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode = WAL")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Ältere Datenbanken auf den aktuellen Stand bringen."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
        for name, definition in MIGRATIONS:
            if name not in columns:
                conn.execute(f"ALTER TABLE activities ADD COLUMN {name} {definition}")
        # Frühere Sportarten auf die aktuellen abbilden
        conn.execute("UPDATE activities SET sport = 'gravel' WHERE sport = 'ride'")
        conn.execute(
            "UPDATE activities SET sport = 'run' WHERE kind = 'gps'"
            " AND sport NOT IN ('gravel', 'enduro', 'run')"
        )

    # Aktivitäten -----------------------------------------------------------

    @staticmethod
    def _encode(a: dict[str, Any]) -> dict[str, Any]:
        return {
            "description": "",
            "started_at": None,
            "duration_s": None,
            "moving_s": None,
            "elevation_gain_m": None,
            "elevation_loss_m": None,
            "kind": "gps",
            "visibility": "public",
            "updated_at": None,
            **a,
            "workout_json": json.dumps(a["workout"]) if a.get("workout") else None,
            "extra_json": json.dumps(a["extra"]) if a.get("extra") else None,
            "track_json": json.dumps(a.get("track") or []),
            "has_image": int(bool(a.get("has_image"))),
        }

    @staticmethod
    def _decode(row: sqlite3.Row | None, light: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        a = dict(row)
        if not light:
            a["track"] = json.loads(a.pop("track_json"))
            a["has_image"] = bool(a["has_image"])
        workout = a.pop("workout_json")
        a["workout"] = json.loads(workout) if workout else None
        extra = a.pop("extra_json")
        a["extra"] = json.loads(extra) if extra else {}
        return a

    def add_activity(self, a: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO activities (id, title, description, sport, started_at,
                   distance_m, duration_s, moving_s, elevation_gain_m, elevation_loss_m,
                   kind, workout_json, track_json, has_image, created_at, visibility,
                   extra_json, updated_at)
                   VALUES (:id, :title, :description, :sport, :started_at, :distance_m,
                   :duration_s, :moving_s, :elevation_gain_m, :elevation_loss_m, :kind,
                   :workout_json, :track_json, :has_image, :created_at, :visibility,
                   :extra_json, :updated_at)""",
                self._encode(a),
            )

    def update_activity(self, a: dict[str, Any]) -> None:
        """Schreibt alle veränderlichen Felder einer vorhandenen Aktivität zurück."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE activities SET title = :title, description = :description,
                   sport = :sport, started_at = :started_at, distance_m = :distance_m,
                   duration_s = :duration_s, moving_s = :moving_s,
                   elevation_gain_m = :elevation_gain_m, elevation_loss_m = :elevation_loss_m,
                   workout_json = :workout_json, track_json = :track_json,
                   has_image = :has_image, visibility = :visibility,
                   extra_json = :extra_json, updated_at = :updated_at WHERE id = :id""",
                self._encode(a),
            )

    def get_activity(self, activity_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        return self._decode(row)

    def list_activities(
        self, limit: int = 50, offset: int = 0, sport: str | None = None,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        where, params = self._filter(sport, include_private)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM activities{where}"
                " ORDER BY COALESCE(started_at, created_at) DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._decode(r) for r in rows]  # type: ignore[misc]

    def count_activities(self, sport: str | None = None, include_private: bool = False) -> int:
        where, params = self._filter(sport, include_private)
        with self._conn() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM activities{where}", params).fetchone()[0]

    @staticmethod
    def _filter(sport: str | None, include_private: bool) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if sport:
            clauses.append("sport = ?")
            params.append(sport)
        if not include_private:
            clauses.append("visibility = 'public'")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def all_light(self, include_private: bool = False) -> list[dict[str, Any]]:
        """Alle Aktivitäten ohne Track, für Statistiken."""
        where, params = self._filter(None, include_private)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {LIGHT_COLUMNS} FROM activities{where}", params).fetchall()
        return [self._decode(r, light=True) for r in rows]  # type: ignore[misc]

    def gps_ids(self) -> list[str]:
        with self._conn() as conn:
            return [r[0] for r in conn.execute("SELECT id FROM activities WHERE kind = 'gps'")]

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

    def update_reply(self, ap_id: str, actor_url: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE interactions SET content = ? WHERE ap_id = ? AND actor_url = ? AND kind = 'reply'",
                (content, ap_id, actor_url),
            )

    def remove_interaction(self, ap_id: str, actor_url: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM interactions WHERE ap_id = ? AND actor_url = ?", (ap_id, actor_url)
            )

    def remove_interaction_on(self, kind: str, activity_id: str, actor_url: str) -> None:
        """Für Undo, das nur das Ziel nennt statt der ursprünglichen Aktivität."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM interactions WHERE kind = ? AND activity_id = ? AND actor_url = ?",
                (kind, activity_id, actor_url),
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

    def get_remote_actor(self, url: str) -> tuple[dict[str, Any], float] | None:
        """Gibt (Daten, Alter in Sekunden) zurück."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data, fetched_at FROM remote_actors WHERE url = ?", (url,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["data"]), time.time() - row["fetched_at"]

    def put_remote_actor(self, url: str, data: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO remote_actors (url, data, fetched_at) VALUES (?, ?, ?)",
                (url, json.dumps(data), time.time()),
            )

    # Zustell-Warteschlange ---------------------------------------------------

    def enqueue_deliveries(self, inboxes: list[str], body: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO deliveries (inbox, body, next_at, created_at) VALUES (?, ?, ?, ?)",
                [(inbox, body, now, now) for inbox in inboxes],
            )

    def due_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE next_at <= ? ORDER BY next_at LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delivery_done(self, delivery_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))

    def delivery_failed(self, delivery_id: int, error: str, retry_in_s: float | None) -> None:
        """retry_in_s = None gibt endgültig auf."""
        with self._conn() as conn:
            if retry_in_s is None:
                conn.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))
            else:
                conn.execute(
                    "UPDATE deliveries SET attempts = attempts + 1, next_at = ?, last_error = ?"
                    " WHERE id = ?",
                    (time.time() + retry_in_s, error[:500], delivery_id),
                )

    def count_deliveries(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

    # Profil ------------------------------------------------------------------

    def get_profile(self) -> dict[str, str]:
        with self._conn() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}

    def set_profile(self, values: dict[str, str]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO profile (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                list(values.items()),
            )
