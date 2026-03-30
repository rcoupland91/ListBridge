"""
Navidrome client using the Subsonic-compatible REST API.
"""

import hashlib
import logging
import os
import random
import string
from typing import List, Optional, Dict

import requests

log = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "ListBridge"


class NavidromeClient:
    def __init__(self, url: str, username: str, password: str):
        self.base_url = url.rstrip("/") + "/rest"
        self.username = username
        self.password = password
        self._connected = False

    # ── Authentication ────────────────────────────────────────────────────────

    def _auth(self) -> Dict[str, str]:
        salt = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "c": CLIENT_NAME,
            "v": API_VERSION,
            "f": "json",
        }

    def _get(self, endpoint: str, **params) -> Optional[Dict]:
        url = f"{self.base_url}/{endpoint}.view"
        try:
            resp = requests.get(url, params={**self._auth(), **params}, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("subsonic-response", {})
            if data.get("status") != "ok":
                log.warning("Navidrome API error on %s: %s", endpoint, data.get("error"))
                return None
            return data
        except Exception as exc:
            log.error("Navidrome request failed (%s): %s", endpoint, exc)
            return None

    def ping(self) -> bool:
        data = self._get("ping")
        self._connected = data is not None
        return self._connected

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Playlist operations ───────────────────────────────────────────────────

    def list_playlists(self) -> List[Dict]:
        data = self._get("getPlaylists")
        if not data:
            return []
        raw = data.get("playlists", {}).get("playlist", [])
        if isinstance(raw, dict):  # single item
            raw = [raw]
        return [
            {
                "id": str(p["id"]),
                "name": p.get("name", ""),
                "song_count": p.get("songCount", 0),
            }
            for p in raw
        ]

    def get_playlist(self, playlist_id: str) -> Optional[Dict]:
        data = self._get("getPlaylist", id=playlist_id)
        if not data:
            return None
        return data.get("playlist")

    def get_playlist_tracks(self, playlist_id: str) -> List[Dict]:
        pl = self.get_playlist(playlist_id)
        if not pl:
            return []
        entries = pl.get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        return [
            {
                "id": str(e["id"]),
                "title": e.get("title", ""),
                "artist": e.get("artist", ""),
                "album": e.get("album", ""),
                "path": e.get("path", ""),
            }
            for e in entries
        ]

    def create_playlist(self, name: str, song_ids: List[str] = None) -> Optional[str]:
        """Create a playlist and return its ID."""
        params: Dict = {"name": name}
        data = self._get("createPlaylist", **params)
        if not data:
            return None
        pl_id = str(data.get("playlist", {}).get("id", ""))
        if pl_id and song_ids:
            self.update_playlist(pl_id, songs_to_add=song_ids)
        return pl_id or None

    def update_playlist(
        self,
        playlist_id: str,
        name: str = None,
        songs_to_add: List[str] = None,
        song_indexes_to_remove: List[int] = None,
    ) -> bool:
        """
        Add or remove songs from an existing Navidrome playlist.

        Because the Subsonic API accepts multiple songIdToAdd params, we
        build the query string manually.
        """
        url = f"{self.base_url}/updatePlaylist.view"
        auth = self._auth()
        params = {**auth, "playlistId": playlist_id}
        if name:
            params["name"] = name

        # requests doesn't natively repeat params; use a list of tuples
        param_list = list(params.items())
        if songs_to_add:
            for sid in songs_to_add:
                param_list.append(("songIdToAdd", sid))
        if song_indexes_to_remove:
            for idx in song_indexes_to_remove:
                param_list.append(("songIndexToRemove", idx))

        try:
            resp = requests.get(url, params=param_list, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("subsonic-response", {})
            return data.get("status") == "ok"
        except Exception as exc:
            log.error("update_playlist(%s) error: %s", playlist_id, exc)
            return False

    def find_playlist_by_name(self, name: str) -> Optional[str]:
        """Return playlist ID for the given name, or None."""
        for pl in self.list_playlists():
            if pl["name"].lower() == name.lower():
                return pl["id"]
        return None

    def get_or_create_playlist(self, name: str) -> Optional[str]:
        pl_id = self.find_playlist_by_name(name)
        if pl_id:
            return pl_id
        return self.create_playlist(name)

    # ── Track search ──────────────────────────────────────────────────────────

    def search_track(self, title: str, artist: str = "") -> Optional[str]:
        """Search for a track by title (and optionally artist). Returns song ID."""
        query = f"{artist} {title}".strip() if artist else title
        data = self._get("search3", query=query, songCount=50, artistCount=0, albumCount=0)
        if not data:
            return None
        songs = data.get("searchResult3", {}).get("song", [])
        if isinstance(songs, dict):
            songs = [songs]
        title_l = title.lower()
        artist_l = artist.lower() if artist else ""
        # Exact title + artist match
        for s in songs:
            if s.get("title", "").lower() == title_l:
                if not artist_l or s.get("artist", "").lower() == artist_l:
                    return str(s["id"])
        # Exact title match, any artist (handles artist tag differences)
        for s in songs:
            if s.get("title", "").lower() == title_l:
                return str(s["id"])
        # Partial title match
        for s in songs:
            if title_l in s.get("title", "").lower():
                return str(s["id"])
        return None

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: List[str]) -> int:
        """Remove tracks by ID from a Navidrome playlist. Returns count removed."""
        if not track_ids:
            return 0
        tracks = self.get_playlist_tracks(playlist_id)
        id_set = set(track_ids)
        # Collect indexes in reverse order so removals don't shift earlier positions
        indexes = sorted(
            [i for i, t in enumerate(tracks) if t["id"] in id_set],
            reverse=True,
        )
        if not indexes:
            return 0
        ok = self.update_playlist(playlist_id, song_indexes_to_remove=indexes)
        return len(indexes) if ok else 0

    def find_track_by_path(self, path: str, artist: str = None) -> Optional[str]:
        """
        Find a Navidrome song ID by matching its file path.
        Navidrome returns `path` in results relative to the music folder root.

        Strategy (in order):
        1. Search by parent directory name — most reliable since Navidrome indexes
           by directory structure, and the directory is usually the real artist name.
        2. Search by artist name — if provided and not a generic value.
        3. Fallback to filename stem search.
        """
        import re

        _GENERIC_ARTISTS = {"various artists", "unknown artist", "unknown", ""}

        norm = path.replace("\\", "/")

        def _match_path(songs):
            for s in songs:
                song_path = s.get("path", "").replace("\\", "/")
                if song_path and norm.endswith(song_path):
                    return str(s["id"])
            return None

        def _search(query, count=200):
            data = self._get("search3", query=query,
                             songCount=count, artistCount=0, albumCount=0)
            if not data:
                return []
            songs = data.get("searchResult3", {}).get("song", [])
            return songs if isinstance(songs, list) else [songs]

        # 1. Parent directory name (real on-disk artist as Navidrome sees it)
        parts = norm.rsplit("/", 2)
        if len(parts) >= 2:
            parent_dir = parts[-2]
            if parent_dir.lower() not in _GENERIC_ARTISTS:
                result = _match_path(_search(parent_dir))
                if result:
                    return result

        # 2. Provided artist name (skip generic values like "Various Artists")
        if artist and artist.lower() not in _GENERIC_ARTISTS:
            result = _match_path(_search(artist, count=500))
            if result:
                return result

        # 3. Filename stem (strip leading track numbers)
        stem = os.path.splitext(os.path.basename(norm))[0]
        stem = re.sub(r"^\d+[\s\.\-]+", "", stem).strip()
        return _match_path(_search(stem, count=100))
