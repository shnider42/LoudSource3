#!/usr/bin/env python3
import os
import sqlite3
import time

DB_PATH = os.getenv("DATABASE_PATH", "/var/data/loudsource3.db")

_STATE_COLUMNS = {
    "current_uri",
    "current_duration_sec",
    "current_progress_sec",
    "current_is_playing",
    "active_device_name",
    "queued_next_for_uri",
    "auto_enabled",
    "cooldown_until",
    "last_paused_ts",
    "auto_enabled_set_ts",
    "boot_id",
}


def _connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db():
    now = int(time.time())
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spotify_token (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                scope TEXT,
                token_type TEXT
            );

            CREATE TABLE IF NOT EXISTS tracks (
                track_id TEXT PRIMARY KEY,
                name TEXT,
                artist TEXT,
                image TEXT,
                preview_url TEXT,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS votes (
                track_id TEXT PRIMARY KEY,
                vote_count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS playback_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_uri TEXT,
                current_duration_sec INTEGER,
                current_progress_sec INTEGER,
                current_is_playing INTEGER,
                active_device_name TEXT,
                queued_next_for_uri TEXT,
                auto_enabled INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0,
                last_paused_ts REAL,
                auto_enabled_set_ts INTEGER,
                boot_id TEXT,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO playback_state (
                id, auto_enabled, cooldown_until, updated_at
            ) VALUES (1, 0, 0, ?)
            """,
            (now,),
        )


def log_event(level: str, message: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO event_log (ts, level, message) VALUES (?, ?, ?)",
            (int(time.time()), level, message),
        )


def save_token(token_info: dict):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO spotify_token (
                id, access_token, refresh_token, expires_at, scope, token_type
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                scope=excluded.scope,
                token_type=excluded.token_type
            """,
            (
                token_info["access_token"],
                token_info["refresh_token"],
                int(token_info["expires_at"]),
                token_info.get("scope"),
                token_info.get("token_type"),
            ),
        )


def get_token():
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT access_token, refresh_token, expires_at, scope, token_type
            FROM spotify_token
            WHERE id = 1
            """
        ).fetchone()
    if not row:
        return None
    return {
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "expires_at": int(row["expires_at"]),
        "scope": row["scope"],
        "token_type": row["token_type"],
    }


def clear_token():
    with _connect() as conn:
        conn.execute("DELETE FROM spotify_token WHERE id = 1")


def _state_defaults():
    return {
        "current_uri": None,
        "current_duration_sec": None,
        "current_progress_sec": None,
        "current_is_playing": None,
        "active_device_name": None,
        "queued_next_for_uri": None,
        "auto_enabled": False,
        "cooldown_until": 0.0,
        "last_paused_ts": None,
        "auto_enabled_set_ts": None,
        "boot_id": None,
    }


def get_state():
    defaults = _state_defaults()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM playback_state WHERE id = 1").fetchone()
    if not row:
        return defaults

    defaults.update(
        {
            "current_uri": row["current_uri"],
            "current_duration_sec": row["current_duration_sec"],
            "current_progress_sec": row["current_progress_sec"],
            "current_is_playing": None
            if row["current_is_playing"] is None
            else bool(row["current_is_playing"]),
            "active_device_name": row["active_device_name"],
            "queued_next_for_uri": row["queued_next_for_uri"],
            "auto_enabled": bool(row["auto_enabled"]),
            "cooldown_until": float(row["cooldown_until"] or 0),
            "last_paused_ts": row["last_paused_ts"],
            "auto_enabled_set_ts": row["auto_enabled_set_ts"],
            "boot_id": row["boot_id"],
        }
    )
    return defaults


def update_state(**fields):
    if not fields:
        return

    safe = {}
    for k, v in fields.items():
        if k not in _STATE_COLUMNS:
            continue
        if k in {"current_is_playing", "auto_enabled"} and v is not None:
            safe[k] = 1 if bool(v) else 0
        else:
            safe[k] = v

    if not safe:
        return

    safe["updated_at"] = int(time.time())

    parts = [f"{k} = ?" for k in safe.keys()]
    values = list(safe.values())
    values.append(1)

    with _connect() as conn:
        conn.execute(
            f"UPDATE playback_state SET {', '.join(parts)} WHERE id = ?",
            values,
        )


def mark_queued_for_snapshot(snapshot_uri: str, cooldown_until: float):
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE playback_state
            SET queued_next_for_uri = ?, cooldown_until = ?, updated_at = ?
            WHERE id = 1 AND current_uri = ?
            """,
            (
                snapshot_uri,
                cooldown_until,
                int(time.time()),
                snapshot_uri,
            ),
        )
        return cur.rowcount > 0


def upsert_track(track_id: str, meta: dict):
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (
                track_id, name, artist, image, preview_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                name=excluded.name,
                artist=excluded.artist,
                image=excluded.image,
                preview_url=excluded.preview_url,
                updated_at=excluded.updated_at
            """,
            (
                track_id,
                meta.get("name"),
                meta.get("artist"),
                meta.get("image"),
                meta.get("preview_url"),
                now,
            ),
        )


def get_track(track_id: str):
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT track_id, name, artist, image, preview_url
            FROM tracks
            WHERE track_id = ?
            """,
            (track_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "track_id": row["track_id"],
        "name": row["name"],
        "artist": row["artist"],
        "image": row["image"],
        "preview_url": row["preview_url"],
    }


def get_tracks(track_ids):
    track_ids = [tid for tid in track_ids if tid]
    if not track_ids:
        return {}

    placeholders = ",".join(["?"] * len(track_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT track_id, name, artist, image, preview_url
            FROM tracks
            WHERE track_id IN ({placeholders})
            """,
            track_ids,
        ).fetchall()

    out = {}
    for row in rows:
        out[row["track_id"]] = {
            "name": row["name"],
            "artist": row["artist"],
            "image": row["image"],
            "preview_url": row["preview_url"],
        }
    return out


def vote_delta(track_id: str, delta: int):
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO votes (track_id, vote_count, updated_at)
            VALUES (?, 0, ?)
            """,
            (track_id, now),
        )
        conn.execute(
            """
            UPDATE votes
            SET vote_count = MAX(0, vote_count + ?),
                updated_at = ?
            WHERE track_id = ?
            """,
            (int(delta), now, track_id),
        )


def clear_votes():
    with _connect() as conn:
        conn.execute("DELETE FROM votes")


def remove_vote(track_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM votes WHERE track_id = ?", (track_id,))


def get_ordered_votes(exclude_tid=None):
    query = """
        SELECT track_id, vote_count
        FROM votes
        WHERE vote_count > 0
    """
    params = []
    if exclude_tid:
        query += " AND track_id != ?"
        params.append(exclude_tid)
    query += " ORDER BY vote_count DESC, updated_at ASC, track_id ASC"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    return [(row["track_id"], int(row["vote_count"])) for row in rows]