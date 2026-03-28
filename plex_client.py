"""
Plex API wrapper using the plexapi library.
"""

import os
import logging
from typing import List, Optional, Dict, Tuple
from plexapi.server import PlexServer
from plexapi.playlist import Playlist
from plexapi.audio import Track
from plexapi.exceptions import NotFound

log = logging.getLogger(__name__)


class PlexClient:
    def __init__(self, url: str, token: str, music_library: str = "Music",
                 plex_path_prefix: str = "", local_path_prefix: str = ""):
        self.url = url.rstrip("/")
        self.token = token
        self.music_library_name = music_library
        self.plex_path_prefix = plex_path_prefix
        self.local_path_prefix = local_path_prefix
        self._plex: Optional[PlexServer] = None
        self._track_index: Optional[Dict[str, Track]] = None   # normalised_path → Track
        self._index_dirty = True

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._plex = PlexServer(self.url, self.token)
            self._index_dirty = True
            return True
        except Exception as exc:
            log.error("Plex connection failed: %s", exc)
            self._plex = None
            return False

    @property
    def connected(self) -> bool:
        return self._plex is not None

    def _server(self) -> PlexServer:
        if not self._plex:
            raise RuntimeError("Not connected to Plex. Call connect() first.")
        return self._plex

    def _music(self):
        return self._server().library.section(self.music_library_name)

    # ── Path conversion helpers ───────────────────────────────────────────────

    def plex_to_local(self, plex_path: str) -> str:
        """Convert a Plex server file path to a local/m3u-visible path."""
        if self.plex_path_prefix and self.local_path_prefix:
            if plex_path.startswith(self.plex_path_prefix):
                return self.local_path_prefix + plex_path[len(self.plex_path_prefix):]
        return plex_path

    def local_to_plex(self, local_path: str) -> str:
        """Convert a local/m3u-visible path to a Plex server file path."""
        if self.plex_path_prefix and self.local_path_prefix:
            if local_path.startswith(self.local_path_prefix):
                return self.plex_path_prefix + local_path[len(self.local_path_prefix):]
        return local_path

    @staticmethod
    def _norm(path: str) -> str:
        return os.path.normpath(path).replace("\\", "/").lower()

    # ── Track index ───────────────────────────────────────────────────────────

    def _build_index(self) -> None:
        log.info("Building Plex track index …")
        index: Dict[str, Track] = {}
        for track in self._music().search(libtype="track"):
            for media in track.media:
                for part in media.parts:
                    index[self._norm(part.file)] = track
        self._track_index = index
        self._index_dirty = False
        log.info("Plex track index built: %d tracks", len(index))

    def _get_index(self) -> Dict[str, Track]:
        if self._track_index is None or self._index_dirty:
            self._build_index()
        return self._track_index

    def invalidate_index(self) -> None:
        self._index_dirty = True

    # ── Track lookup ─────────────────────────────────────────────────────────

    def find_track_by_path(self, local_path: str) -> Optional[Track]:
        """Find a Plex Track by local file path."""
        plex_path = self.local_to_plex(local_path)
        index = self._get_index()
        return index.get(self._norm(plex_path))

    def find_track_by_title_artist(self, title: str, artist: str) -> Optional[Track]:
        """Fuzzy-find a Plex track by title and artist name."""
        try:
            results = self._music().search(title, libtype="track")
            title_l = title.lower()
            artist_l = artist.lower() if artist else ""
            for t in results:
                if t.title.lower() == title_l:
                    try:
                        if not artist_l or t.artist().title.lower() == artist_l:
                            return t
                    except Exception:
                        return t
        except Exception as exc:
            log.debug("find_track_by_title_artist error: %s", exc)
        return None

    def find_track(self, local_path: str, title: str = None, artist: str = None) -> Optional[Track]:
        """Try path first, then title+artist fallback."""
        track = self.find_track_by_path(local_path)
        if track is None and title:
            track = self.find_track_by_title_artist(title, artist or "")
        return track

    # ── Playlist operations ───────────────────────────────────────────────────

    def get_playlist(self, playlist_id_or_name: str) -> Optional[Playlist]:
        try:
            # Try by ratingKey first (numeric ID)
            if playlist_id_or_name.isdigit():
                return self._server().fetchItem(int(playlist_id_or_name))
            return self._server().playlist(playlist_id_or_name)
        except NotFound:
            return None
        except Exception as exc:
            log.error("get_playlist(%s) error: %s", playlist_id_or_name, exc)
            return None

    def list_playlists(self) -> List[Dict]:
        playlists = []
        for pl in self._server().playlists():
            if pl.playlistType == "audio":
                playlists.append({
                    "id": str(pl.ratingKey),
                    "title": pl.title,
                    "track_count": pl.leafCount,
                    "updated_at": str(pl.updatedAt) if pl.updatedAt else None,
                })
        return playlists

    def get_playlist_tracks(self, playlist: Playlist) -> List[Dict]:
        """Return list of dicts with track info including local file path."""
        result = []
        for track in playlist.items():
            local_path = None
            for media in track.media:
                for part in media.parts:
                    local_path = self.plex_to_local(part.file)
                    break
                if local_path:
                    break
            try:
                artist = track.artist().title
            except Exception:
                artist = getattr(track, "grandparentTitle", None)
            result.append({
                "key": str(track.ratingKey),
                "title": track.title,
                "artist": artist,
                "album": getattr(track, "parentTitle", None),
                "local_path": local_path,
            })
        return result

    def create_playlist(self, name: str, tracks: List[Track]) -> Optional[Playlist]:
        try:
            pl = Playlist.create(self._server(), name, items=tracks)
            log.info("Created Plex playlist '%s' (id=%s)", name, pl.ratingKey)
            return pl
        except Exception as exc:
            log.error("create_playlist('%s') error: %s", name, exc)
            return None

    def add_tracks_to_playlist(self, playlist: Playlist, tracks: List[Track]) -> int:
        if not tracks:
            return 0
        try:
            playlist.addItems(tracks)
            return len(tracks)
        except Exception as exc:
            log.error("add_tracks_to_playlist error: %s", exc)
            return 0

    def remove_tracks_from_playlist(self, playlist: Playlist, tracks: List[Track]) -> int:
        if not tracks:
            return 0
        try:
            playlist.removeItems(tracks)
            return len(tracks)
        except Exception as exc:
            log.error("remove_tracks_from_playlist error: %s", exc)
            return 0

    def get_or_create_playlist(self, name: str, initial_tracks: List[Track] = None) -> Optional[Playlist]:
        try:
            pl = self._server().playlist(name)
            return pl
        except NotFound:
            if initial_tracks:
                return self.create_playlist(name, initial_tracks)
            # plexapi requires at least one item; create with a dummy then clear is not ideal.
            # We return None and let the caller handle it when first track arrives.
            return None

    def playlist_updated_at(self, playlist: Playlist) -> Optional[str]:
        try:
            playlist.reload()
            return str(playlist.updatedAt) if playlist.updatedAt else None
        except Exception:
            return None
