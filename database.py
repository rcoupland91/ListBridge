import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "listbridge.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS watched_playlists (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT NOT NULL,
            m3u_path              TEXT UNIQUE,
            plex_playlist_id      TEXT,
            navidrome_playlist_id TEXT,
            sync_m3u_to_plex      INTEGER DEFAULT 1,
            sync_plex_to_m3u      INTEGER DEFAULT 1,
            sync_to_navidrome     INTEGER DEFAULT 1,
            last_m3u_hash         TEXT,
            last_plex_sync        TEXT,
            last_m3u_sync         TEXT,
            created_at            TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sync_tracks (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id         INTEGER NOT NULL REFERENCES watched_playlists(id) ON DELETE CASCADE,
            file_path           TEXT,
            title               TEXT,
            artist              TEXT,
            plex_track_key      TEXT,
            navidrome_track_id  TEXT,
            in_m3u              INTEGER DEFAULT 0,
            in_plex             INTEGER DEFAULT 0,
            in_navidrome        INTEGER DEFAULT 0,
            added_at            TEXT DEFAULT (datetime('now')),
            UNIQUE(playlist_id, file_path)
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER REFERENCES watched_playlists(id) ON DELETE CASCADE,
            event_type  TEXT NOT NULL,
            source      TEXT,
            message     TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)

    conn.commit()
    conn.close()


# ── Settings helpers ─────────────────────────────────────────────────────────

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def get_all_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# ── Playlist helpers ──────────────────────────────────────────────────────────

def get_playlists():
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watched_playlists ORDER BY name"
        ).fetchall()


def get_playlist(playlist_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watched_playlists WHERE id=?", (playlist_id,)
        ).fetchone()


def get_playlist_by_m3u(m3u_path):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watched_playlists WHERE m3u_path=?", (m3u_path,)
        ).fetchone()


def upsert_playlist(name, m3u_path=None, plex_playlist_id=None,
                    navidrome_playlist_id=None, sync_m3u_to_plex=True,
                    sync_plex_to_m3u=True, sync_to_navidrome=True):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO watched_playlists
                (name, m3u_path, plex_playlist_id, navidrome_playlist_id,
                 sync_m3u_to_plex, sync_plex_to_m3u, sync_to_navidrome)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(m3u_path) DO UPDATE SET
                name=excluded.name,
                plex_playlist_id=COALESCE(excluded.plex_playlist_id, plex_playlist_id),
                navidrome_playlist_id=COALESCE(excluded.navidrome_playlist_id, navidrome_playlist_id),
                sync_m3u_to_plex=excluded.sync_m3u_to_plex,
                sync_plex_to_m3u=excluded.sync_plex_to_m3u,
                sync_to_navidrome=excluded.sync_to_navidrome
            """,
            (name, m3u_path, plex_playlist_id, navidrome_playlist_id,
             int(sync_m3u_to_plex), int(sync_plex_to_m3u), int(sync_to_navidrome)),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM watched_playlists WHERE m3u_path=?", (m3u_path,)
        ).fetchone()["id"]


def update_playlist_fields(playlist_id, **fields):
    allowed = {
        "name", "plex_playlist_id", "navidrome_playlist_id",
        "sync_m3u_to_plex", "sync_plex_to_m3u", "sync_to_navidrome",
        "last_m3u_hash", "last_plex_sync", "last_m3u_sync",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE watched_playlists SET {set_clause} WHERE id=?",
            (*updates.values(), playlist_id),
        )
        conn.commit()


def delete_playlist(playlist_id):
    with get_db() as conn:
        conn.execute("DELETE FROM watched_playlists WHERE id=?", (playlist_id,))
        conn.commit()


# ── Sync-track helpers ────────────────────────────────────────────────────────

def get_sync_tracks(playlist_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM sync_tracks WHERE playlist_id=?", (playlist_id,)
        ).fetchall()


def upsert_sync_track(playlist_id, file_path, title=None, artist=None,
                      plex_track_key=None, navidrome_track_id=None,
                      in_m3u=None, in_plex=None, in_navidrome=None):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM sync_tracks WHERE playlist_id=? AND file_path=?",
            (playlist_id, file_path),
        ).fetchone()

        if existing:
            updates = {}
            if title is not None:
                updates["title"] = title
            if artist is not None:
                updates["artist"] = artist
            if plex_track_key is not None:
                updates["plex_track_key"] = plex_track_key
            if navidrome_track_id is not None:
                updates["navidrome_track_id"] = navidrome_track_id
            if in_m3u is not None:
                updates["in_m3u"] = int(in_m3u)
            if in_plex is not None:
                updates["in_plex"] = int(in_plex)
            if in_navidrome is not None:
                updates["in_navidrome"] = int(in_navidrome)
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE sync_tracks SET {set_clause} WHERE playlist_id=? AND file_path=?",
                    (*updates.values(), playlist_id, file_path),
                )
        else:
            conn.execute(
                """
                INSERT INTO sync_tracks
                    (playlist_id, file_path, title, artist, plex_track_key,
                     navidrome_track_id, in_m3u, in_plex, in_navidrome)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    playlist_id, file_path, title, artist, plex_track_key,
                    navidrome_track_id,
                    int(in_m3u) if in_m3u is not None else 0,
                    int(in_plex) if in_plex is not None else 0,
                    int(in_navidrome) if in_navidrome is not None else 0,
                ),
            )
        conn.commit()


def clear_playlist_presence(playlist_id, source):
    """Reset all in_<source> flags to 0 before a fresh sync scan."""
    col = f"in_{source}"
    if col not in ("in_m3u", "in_plex", "in_navidrome"):
        return
    with get_db() as conn:
        conn.execute(
            f"UPDATE sync_tracks SET {col}=0 WHERE playlist_id=?", (playlist_id,)
        )
        conn.commit()


# ── Log helpers ───────────────────────────────────────────────────────────────

def add_log(playlist_id, event_type, source=None, message=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_log(playlist_id,event_type,source,message) VALUES(?,?,?,?)",
            (playlist_id, event_type, source, message),
        )
        conn.commit()


def get_logs(playlist_id=None, limit=100):
    with get_db() as conn:
        if playlist_id:
            rows = conn.execute(
                "SELECT l.*, p.name as playlist_name FROM sync_log l "
                "LEFT JOIN watched_playlists p ON l.playlist_id=p.id "
                "WHERE l.playlist_id=? ORDER BY l.id DESC LIMIT ?",
                (playlist_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT l.*, p.name as playlist_name FROM sync_log l "
                "LEFT JOIN watched_playlists p ON l.playlist_id=p.id "
                "ORDER BY l.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows
