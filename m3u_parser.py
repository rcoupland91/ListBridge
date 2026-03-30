"""
M3U / M3U8 playlist parser and writer.

Handles both plain (#EXTM3U) and extended M3U formats.
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class M3UTrack:
    path: str                   # Absolute or relative file path / URL
    title: Optional[str] = None
    artist: Optional[str] = None
    duration: int = -1          # seconds; -1 = unknown
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        if self.title:
            return self.title
        return os.path.basename(self.path)


def parse(m3u_path: str, relative_base: str = None) -> List[M3UTrack]:
    """Parse an .m3u / .m3u8 file and return a list of M3UTrack objects.

    relative_base: directory to resolve relative paths against. Defaults to
    the M3U file's own directory. Set to the music root (e.g. LOCAL_PATH_PREFIX)
    when M3U files use paths relative to the library root rather than to themselves.
    """
    tracks: List[M3UTrack] = []
    m3u_dir = os.path.dirname(os.path.abspath(m3u_path))
    resolve_base = os.path.abspath(relative_base) if relative_base else m3u_dir

    pending_title: Optional[str] = None
    pending_artist: Optional[str] = None
    pending_duration: int = -1

    encoding = "utf-8"
    try:
        with open(m3u_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except UnicodeDecodeError:
        encoding = "latin-1"
        with open(m3u_path, encoding="latin-1") as fh:
            lines = fh.readlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            # #EXTINF:<duration>,<artist> - <title>
            # or #EXTINF:<duration> tvg-name="..." ,<title>
            m = re.match(r"#EXTINF:(-?\d+)[^,]*,\s*(.*)", line)
            if m:
                pending_duration = int(m.group(1))
                info = m.group(2).strip()
                # Try "Artist - Title" split
                if " - " in info:
                    parts = info.split(" - ", 1)
                    pending_artist = parts[0].strip()
                    pending_title = parts[1].strip()
                else:
                    pending_title = info
                    pending_artist = None
            continue

        if line.startswith("#"):
            # Unknown directive — skip but don't consume pending data
            continue

        # It's a file path or URL
        path = line
        if not path.startswith(("http://", "https://", "ftp://")) and not os.path.isabs(path):
            resolved = os.path.normpath(os.path.join(resolve_base, path))
            # Fallback: if resolve_base differs from m3u_dir and file doesn't exist there,
            # try resolving relative to the M3U file's own directory
            if resolve_base != m3u_dir and not os.path.exists(resolved):
                fallback = os.path.normpath(os.path.join(m3u_dir, path))
                if os.path.exists(fallback):
                    resolved = fallback
            path = resolved

        tracks.append(
            M3UTrack(
                path=path,
                title=pending_title,
                artist=pending_artist,
                duration=pending_duration,
            )
        )
        pending_title = None
        pending_artist = None
        pending_duration = -1

    return tracks


def write(m3u_path: str, tracks: List[M3UTrack], extended: bool = True) -> None:
    """Write tracks to an .m3u file, preserving existing entries and appending new ones."""
    lines = []
    if extended:
        lines.append("#EXTM3U\n")

    for track in tracks:
        if extended:
            artist_title = (
                f"{track.artist} - {track.title}"
                if track.artist and track.title
                else (track.title or os.path.basename(track.path))
            )
            lines.append(f"#EXTINF:{track.duration},{artist_title}\n")
        lines.append(f"{track.path}\n")

    os.makedirs(os.path.dirname(os.path.abspath(m3u_path)), exist_ok=True)
    with open(m3u_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def merge_paths(existing_tracks: List[M3UTrack], new_tracks: List[M3UTrack]) -> List[M3UTrack]:
    """
    Merge new_tracks into existing_tracks without duplicating.
    Returns a new list (existing + genuinely new entries).
    """
    existing_paths = {_normalise(t.path) for t in existing_tracks}
    result = list(existing_tracks)
    for t in new_tracks:
        if _normalise(t.path) not in existing_paths:
            result.append(t)
            existing_paths.add(_normalise(t.path))
    return result


def path_set(tracks: List[M3UTrack]) -> set:
    return {_normalise(t.path) for t in tracks}


def _normalise(path: str) -> str:
    """Normalise a file path for comparison (lowercase, forward slashes)."""
    return os.path.normpath(path).replace("\\", "/").lower()
