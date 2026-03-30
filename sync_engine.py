"""
Core sync engine — orchestrates bidirectional sync between m3u, Plex, and Navidrome.
"""

import hashlib
import logging
import os
from datetime import datetime
from typing import Optional, Callable

import database as db
import m3u_parser as m3u
from plex_client import PlexClient
from navidrome_client import NavidromeClient

log = logging.getLogger(__name__)


def _file_hash(path: str) -> str:
    """Return MD5 hash of a file's contents."""
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


class SyncEngine:
    def __init__(
        self,
        plex: Optional[PlexClient] = None,
        navi: Optional[NavidromeClient] = None,
        emit_fn: Optional[Callable] = None,
    ):
        self.plex = plex
        self.navi = navi
        self._emit = emit_fn or (lambda *a, **kw: None)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log(self, playlist_id, event_type, source=None, message=None):
        db.add_log(playlist_id, event_type, source, message)
        self._emit("sync_log", {
            "playlist_id": playlist_id,
            "event_type": event_type,
            "source": source,
            "message": message,
            "ts": datetime.utcnow().isoformat(),
        })
        log.info("[pl=%s] [%s/%s] %s", playlist_id, event_type, source, message)

    # ── M3U → Plex ────────────────────────────────────────────────────────────

    def sync_m3u_to_plex(self, playlist_id: int) -> dict:
        pl = db.get_playlist(playlist_id)
        if not pl:
            return {"error": "Playlist not found"}
        if not pl["sync_m3u_to_plex"]:
            return {"skipped": True, "reason": "sync_m3u_to_plex disabled"}
        if not self.plex or not self.plex.connected:
            return {"error": "Plex not connected"}
        if not pl["m3u_path"] or not os.path.exists(pl["m3u_path"]):
            return {"error": f"M3U file not found: {pl['m3u_path']}"}

        self._log(playlist_id, "sync_start", "m3u→plex", f"Syncing {pl['name']}")

        # 1. Parse m3u — resolve relative paths against the music root if configured
        music_root = self.plex.local_path_prefix if self.plex else None
        m3u_tracks = m3u.parse(pl["m3u_path"], relative_base=music_root)
        if not m3u_tracks:
            self._log(playlist_id, "sync_info", "m3u→plex", "M3U file is empty")
            return {"added": 0, "skipped": 0}

        # 2. Get or create Plex playlist
        plex_playlist = None
        if pl["plex_playlist_id"]:
            plex_playlist = self.plex.get_playlist(pl["plex_playlist_id"])

        # Build set of already-synced file paths (in Plex)
        existing_plex_paths: set = set()
        if plex_playlist:
            for t in self.plex.get_playlist_tracks(plex_playlist):
                if t["local_path"]:
                    existing_plex_paths.add(m3u._normalise(t["local_path"]))

        # 3. Find new tracks to add
        tracks_to_add = []
        skipped = 0
        for track in m3u_tracks:
            norm = m3u._normalise(track.path)
            if norm in existing_plex_paths:
                skipped += 1
                db.upsert_sync_track(playlist_id, track.path,
                                     title=track.title, artist=track.artist,
                                     in_m3u=True, in_plex=True)
                continue

            plex_track = self.plex.find_track(track.path, track.title, track.artist)
            if plex_track:
                tracks_to_add.append(plex_track)
                db.upsert_sync_track(playlist_id, track.path,
                                     title=track.title, artist=track.artist,
                                     plex_track_key=str(plex_track.ratingKey),
                                     in_m3u=True, in_plex=True)
                self._log(playlist_id, "track_matched", "m3u→plex",
                          f"Matched: {track.display_name}")
            else:
                db.upsert_sync_track(playlist_id, track.path,
                                     title=track.title, artist=track.artist,
                                     in_m3u=True, in_plex=False)
                self._log(playlist_id, "track_not_found", "m3u→plex",
                          f"Not found in Plex: {track.display_name}")

        # 4. Push to Plex
        added = 0
        if tracks_to_add:
            if plex_playlist:
                added = self.plex.add_tracks_to_playlist(plex_playlist, tracks_to_add)
            else:
                plex_playlist = self.plex.create_playlist(pl["name"], tracks_to_add)
                if plex_playlist:
                    added = len(tracks_to_add)
                    db.update_playlist_fields(playlist_id,
                                              plex_playlist_id=str(plex_playlist.ratingKey))

        # 5. Update hash so watcher knows file state
        current_hash = _file_hash(pl["m3u_path"])
        db.update_playlist_fields(playlist_id,
                                  last_m3u_hash=current_hash,
                                  last_m3u_sync=datetime.utcnow().isoformat())

        self._log(playlist_id, "sync_done", "m3u→plex",
                  f"Done: {added} added, {skipped} already present")
        self._emit("playlist_updated", {"playlist_id": playlist_id})
        return {"added": added, "skipped": skipped}

    # ── Plex → M3U ────────────────────────────────────────────────────────────

    def sync_plex_to_m3u(self, playlist_id: int) -> dict:
        pl = db.get_playlist(playlist_id)
        if not pl:
            return {"error": "Playlist not found"}
        if not pl["sync_plex_to_m3u"]:
            return {"skipped": True, "reason": "sync_plex_to_m3u disabled"}
        if not self.plex or not self.plex.connected:
            return {"error": "Plex not connected"}
        if not pl["plex_playlist_id"]:
            return {"skipped": True, "reason": "No Plex playlist linked"}

        plex_playlist = self.plex.get_playlist(pl["plex_playlist_id"])
        if not plex_playlist:
            return {"error": "Plex playlist not found"}

        self._log(playlist_id, "sync_start", "plex→m3u", f"Syncing {pl['name']}")

        # 1. Get current m3u tracks (if file exists)
        m3u_path = pl["m3u_path"]
        existing_tracks = []
        if m3u_path and os.path.exists(m3u_path):
            existing_tracks = m3u.parse(m3u_path)
        existing_paths = m3u.path_set(existing_tracks)

        # 2. Get Plex playlist tracks
        plex_tracks = self.plex.get_playlist_tracks(plex_playlist)
        added = 0
        new_tracks = []
        for pt in plex_tracks:
            path = pt["local_path"]
            if not path:
                continue
            db.upsert_sync_track(playlist_id, path,
                                  title=pt["title"], artist=pt["artist"],
                                  plex_track_key=pt["key"],
                                  in_plex=True)
            if m3u._normalise(path) not in existing_paths:
                new_tracks.append(m3u.M3UTrack(
                    path=path,
                    title=pt["title"],
                    artist=pt["artist"],
                ))
                self._log(playlist_id, "track_added", "plex→m3u",
                          f"New from Plex: {pt['title']}")
                added += 1

        # 3. Write updated m3u
        if new_tracks and m3u_path:
            merged = m3u.merge_paths(existing_tracks, new_tracks)
            m3u.write(m3u_path, merged)
            current_hash = _file_hash(m3u_path)
            db.update_playlist_fields(playlist_id,
                                      last_m3u_hash=current_hash,
                                      last_plex_sync=datetime.utcnow().isoformat())

        self._log(playlist_id, "sync_done", "plex→m3u",
                  f"Done: {added} new tracks written to m3u")
        self._emit("playlist_updated", {"playlist_id": playlist_id})
        return {"added": added}

    # ── Plex → Navidrome ─────────────────────────────────────────────────────

    def sync_to_navidrome(self, playlist_id: int) -> dict:
        pl = db.get_playlist(playlist_id)
        if not pl:
            return {"error": "Playlist not found"}
        if not pl["sync_to_navidrome"]:
            return {"skipped": True, "reason": "sync_to_navidrome disabled"}
        if not self.navi or not self.navi.connected:
            return {"error": "Navidrome not connected"}

        self._log(playlist_id, "sync_start", "→navidrome", f"Syncing {pl['name']}")

        # Build list of tracks from our sync_tracks table that are in_plex
        sync_tracks = db.get_sync_tracks(playlist_id)
        added = 0
        skipped = 0

        # Get or create Navidrome playlist
        navi_pl_id = pl["navidrome_playlist_id"]
        if not navi_pl_id:
            navi_pl_id = self.navi.get_or_create_playlist(pl["name"])
            if navi_pl_id:
                db.update_playlist_fields(playlist_id,
                                          navidrome_playlist_id=navi_pl_id)

        if not navi_pl_id:
            return {"error": "Could not get/create Navidrome playlist"}

        # Get tracks already in Navidrome playlist
        existing_navi = {t["id"] for t in self.navi.get_playlist_tracks(navi_pl_id)}

        songs_to_add = []
        for st in sync_tracks:
            if st["navidrome_track_id"] and st["navidrome_track_id"] in existing_navi:
                skipped += 1
                continue

            navi_id = st["navidrome_track_id"]
            if not navi_id:
                # Try to find in Navidrome
                if st["file_path"]:
                    navi_id = self.navi.find_track_by_path(st["file_path"])
                if not navi_id and st["title"]:
                    navi_id = self.navi.search_track(st["title"], st["artist"] or "")
                if navi_id:
                    db.upsert_sync_track(playlist_id, st["file_path"],
                                         navidrome_track_id=navi_id,
                                         in_navidrome=True)

            if navi_id and navi_id not in existing_navi:
                songs_to_add.append(navi_id)
                self._log(playlist_id, "track_matched", "→navidrome",
                          f"Matched: {st['title'] or st['file_path']}")
            elif not navi_id:
                self._log(playlist_id, "track_not_found", "→navidrome",
                          f"Not found: {st['title'] or st['file_path']}")

        if songs_to_add:
            # Batch update
            ok = self.navi.update_playlist(navi_pl_id, songs_to_add=songs_to_add)
            added = len(songs_to_add) if ok else 0

        self._log(playlist_id, "sync_done", "→navidrome",
                  f"Done: {added} added, {skipped} already present")
        self._emit("playlist_updated", {"playlist_id": playlist_id})
        return {"added": added, "skipped": skipped}

    # ── Full sync ─────────────────────────────────────────────────────────────

    def full_sync(self, playlist_id: int) -> dict:
        """Run all enabled sync directions for a playlist."""
        results = {}
        results["m3u_to_plex"] = self.sync_m3u_to_plex(playlist_id)
        results["plex_to_m3u"] = self.sync_plex_to_m3u(playlist_id)
        results["to_navidrome"] = self.sync_to_navidrome(playlist_id)
        return results

    # ── File-change triggered sync ────────────────────────────────────────────

    def on_m3u_changed(self, m3u_path: str) -> None:
        """Called by the file watcher when an m3u file is modified."""
        pl = db.get_playlist_by_m3u(m3u_path)
        if not pl:
            log.debug("on_m3u_changed: no playlist registered for %s", m3u_path)
            return

        # Check if content actually changed (avoid spurious events)
        new_hash = _file_hash(m3u_path)
        if new_hash == pl["last_m3u_hash"]:
            log.debug("on_m3u_changed: hash unchanged, skipping")
            return

        self._log(pl["id"], "file_changed", "m3u", f"Detected change in {m3u_path}")
        self.sync_m3u_to_plex(pl["id"])
        self.sync_to_navidrome(pl["id"])

    # ── Plex-poll triggered sync ──────────────────────────────────────────────

    def on_plex_poll(self, playlist_id: int) -> None:
        """Called periodically to check for Plex-side changes."""
        pl = db.get_playlist(playlist_id)
        if not pl or not pl["plex_playlist_id"]:
            return
        if not self.plex or not self.plex.connected:
            return

        plex_pl = self.plex.get_playlist(pl["plex_playlist_id"])
        if not plex_pl:
            return

        updated_at = self.plex.playlist_updated_at(plex_pl)
        if updated_at and updated_at == pl["last_plex_sync"]:
            return  # Nothing changed

        self._log(pl["id"], "plex_change_detected", "plex",
                  "Plex playlist changed, syncing back")
        self.sync_plex_to_m3u(playlist_id)
        self.sync_to_navidrome(playlist_id)
        db.update_playlist_fields(playlist_id, last_plex_sync=updated_at)
