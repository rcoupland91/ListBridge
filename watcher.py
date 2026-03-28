"""
File watcher — monitors a directory for .m3u / .m3u8 file changes
and triggers syncs via the SyncEngine.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

log = logging.getLogger(__name__)


class M3UEventHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[str], None]):
        super().__init__()
        self._on_change = on_change
        # Debounce per-path to avoid double-firing on editor saves
        self._debounce: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str) -> None:
        with self._lock:
            existing = self._debounce.get(path)
            if existing:
                existing.cancel()
            timer = threading.Timer(1.5, self._fire, args=(path,))
            self._debounce[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._debounce.pop(path, None)
        self._on_change(path)

    def _is_m3u(self, path: str) -> bool:
        return path.lower().endswith((".m3u", ".m3u8"))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_m3u(event.src_path):
            log.debug("File modified: %s", event.src_path)
            self._schedule(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_m3u(event.src_path):
            log.debug("File created: %s", event.src_path)
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_m3u(getattr(event, "dest_path", "")):
            log.debug("File moved to: %s", event.dest_path)
            self._schedule(event.dest_path)


class PlaylistWatcher:
    def __init__(self, on_change: Callable[[str], None]):
        self._on_change = on_change
        self._observer: Optional[Observer] = None
        self._watched_dirs: set[str] = set()
        self._lock = threading.Lock()

    def watch(self, directory: str) -> bool:
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            log.warning("Cannot watch non-existent directory: %s", directory)
            return False

        with self._lock:
            if directory in self._watched_dirs:
                return True
            if self._observer is None:
                self._start_observer()
            handler = M3UEventHandler(self._on_change)
            self._observer.schedule(handler, directory, recursive=True)
            self._watched_dirs.add(directory)
            log.info("Watching directory: %s", directory)
            return True

    def _start_observer(self) -> None:
        self._observer = Observer()
        self._observer.start()

    def stop(self) -> None:
        with self._lock:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=5)
                self._observer = None
                self._watched_dirs.clear()


class PlexPoller:
    """Polls all registered playlists for Plex-side changes."""

    def __init__(self, on_poll: Callable[[int], None], interval: int = 60):
        self._on_poll = on_poll
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="plex-poller")
        self._thread.start()
        log.info("Plex poller started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        import database as db
        while not self._stop_event.wait(self.interval):
            try:
                for pl in db.get_playlists():
                    if pl["plex_playlist_id"]:
                        self._on_poll(pl["id"])
            except Exception as exc:
                log.error("Plex poller error: %s", exc)
