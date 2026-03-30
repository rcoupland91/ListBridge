"""
ListBridge — M3U ↔ Plex ↔ Navidrome playlist sync web application.
"""

import logging
import os
import threading
from datetime import datetime

import eventlet
import eventlet.tpool

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO

import database as db
from plex_client import PlexClient
from navidrome_client import NavidromeClient
from sync_engine import SyncEngine
from watcher import PlaylistWatcher, PlexPoller

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")


@app.template_filter("log_badge_class")
def log_badge_class(event_type: str) -> str:
    mapping = {
        "sync_start": "bg-primary",
        "sync_done": "bg-success",
        "track_added": "bg-info text-dark",
        "track_matched": "bg-success",
        "track_not_found": "bg-warning text-dark",
        "error": "bg-danger",
        "file_changed": "bg-purple",
        "plex_change_detected": "bg-warning text-dark",
        "sync_info": "bg-secondary",
    }
    return mapping.get(event_type, "bg-secondary")

# ── Global singletons ─────────────────────────────────────────────────────────

_plex: PlexClient = None
_navi: NavidromeClient = None
_engine: SyncEngine = None
_watcher: PlaylistWatcher = None
_poller: PlexPoller = None
_lock = threading.Lock()


def _emit(event, data):
    socketio.emit(event, data)


def _rebuild_clients():
    global _plex, _navi, _engine, _watcher, _poller

    settings = db.get_all_settings()

    plex_url = settings.get("plex_url", "")
    plex_token = settings.get("plex_token", "")
    plex_lib = settings.get("plex_music_library", "Music")
    plex_pfx = settings.get("plex_path_prefix", "")
    local_pfx = settings.get("local_path_prefix", "")

    navi_url = settings.get("navidrome_url", "")
    navi_user = settings.get("navidrome_user", "")
    navi_pass = settings.get("navidrome_password", "")

    m3u_dir = settings.get("m3u_directory", "")
    poll_interval = int(settings.get("plex_poll_interval", "60"))

    # Stop old background workers
    if _watcher:
        _watcher.stop()
    if _poller:
        _poller.stop()

    # Build Plex client
    if plex_url and plex_token:
        _plex = PlexClient(plex_url, plex_token, plex_lib, plex_pfx, local_pfx)
        connected = _plex.connect()
        log.info("Plex connected: %s", connected)
    else:
        _plex = None

    # Build Navidrome client
    if navi_url and navi_user and navi_pass:
        _navi = NavidromeClient(navi_url, navi_user, navi_pass)
        connected = _navi.ping()
        log.info("Navidrome connected: %s", connected)
    else:
        _navi = None

    # Build sync engine
    _engine = SyncEngine(plex=_plex, navi=_navi, emit_fn=_emit)

    # File watcher — started in background to avoid blocking on large directories
    # (PollingObserver takes an initial snapshot synchronously before the thread starts)
    _watcher = PlaylistWatcher(on_change=_engine.on_m3u_changed)

    dirs_to_watch = []
    if m3u_dir and os.path.isdir(m3u_dir):
        dirs_to_watch.append(m3u_dir)
    for pl in db.get_playlists():
        if pl["m3u_path"]:
            d = os.path.dirname(pl["m3u_path"])
            if d and os.path.isdir(d) and d not in dirs_to_watch:
                dirs_to_watch.append(d)

    def _start_watcher():
        for d in dirs_to_watch:
            _watcher.watch(d)
        log.info("File watcher ready, watching %d director(ies)", len(dirs_to_watch))

    threading.Thread(target=_start_watcher, daemon=True, name="watcher-init").start()

    # Plex poller
    _poller = PlexPoller(on_poll=_engine.on_plex_poll, interval=poll_interval)
    if _plex and _plex.connected:
        _poller.start()


def _get_engine() -> SyncEngine:
    if _engine is None:
        _rebuild_clients()
    return _engine


# ── App startup ───────────────────────────────────────────────────────────────

@app.before_request
def _ensure_init():
    """Lazy init on first request."""
    pass


with app.app_context():
    db.init_db()
    # Seed settings from env vars if not already set
    env_defaults = {
        "plex_url": os.environ.get("PLEX_URL", ""),
        "plex_token": os.environ.get("PLEX_TOKEN", ""),
        "plex_music_library": os.environ.get("PLEX_MUSIC_LIBRARY", "Music"),
        "plex_path_prefix": os.environ.get("PLEX_PATH_PREFIX", ""),
        "local_path_prefix": os.environ.get("LOCAL_PATH_PREFIX", ""),
        "navidrome_url": os.environ.get("NAVIDROME_URL", ""),
        "navidrome_user": os.environ.get("NAVIDROME_USER", ""),
        "navidrome_password": os.environ.get("NAVIDROME_PASSWORD", ""),
        "m3u_directory": os.environ.get("M3U_DIRECTORY", ""),
        "plex_poll_interval": os.environ.get("PLEX_POLL_INTERVAL", "60"),
    }
    for key, val in env_defaults.items():
        if val and not db.get_setting(key):
            db.set_setting(key, val)

    _rebuild_clients()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    playlists = [dict(p) for p in db.get_playlists()]
    logs = [dict(l) for l in db.get_logs(limit=50)]
    plex_ok = _plex is not None and _plex.connected
    navi_ok = _navi is not None and _navi.connected
    return render_template(
        "index.html",
        playlists=playlists,
        logs=logs,
        plex_ok=plex_ok,
        navi_ok=navi_ok,
    )


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        fields = [
            "plex_url", "plex_token", "plex_music_library",
            "plex_path_prefix", "local_path_prefix",
            "navidrome_url", "navidrome_user", "navidrome_password",
            "m3u_directory", "plex_poll_interval",
        ]
        for f in fields:
            val = request.form.get(f, "").strip()
            db.set_setting(f, val)
        _rebuild_clients()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    s = db.get_all_settings()
    plex_ok = _plex is not None and _plex.connected
    navi_ok = _navi is not None and _navi.connected
    return render_template("settings.html", s=s, plex_ok=plex_ok, navi_ok=navi_ok)


# ── Playlists ─────────────────────────────────────────────────────────────────

@app.route("/playlists")
def playlists():
    pls = [dict(p) for p in db.get_playlists()]

    # Attach available Plex playlists for linking
    plex_playlists = []
    if _plex and _plex.connected:
        try:
            plex_playlists = _plex.list_playlists()
        except Exception:
            pass

    navi_playlists = []
    if _navi and _navi.connected:
        try:
            navi_playlists = _navi.list_playlists()
        except Exception:
            pass

    return render_template(
        "playlists.html",
        playlists=pls,
        plex_playlists=plex_playlists,
        navi_playlists=navi_playlists,
    )


@app.route("/playlists/add", methods=["POST"])
def playlist_add():
    name = request.form.get("name", "").strip()
    m3u_path = request.form.get("m3u_path", "").strip()
    plex_id = request.form.get("plex_playlist_id", "").strip() or None
    navi_id = request.form.get("navidrome_playlist_id", "").strip() or None
    sync_m3u_to_plex = "sync_m3u_to_plex" in request.form
    sync_plex_to_m3u = "sync_plex_to_m3u" in request.form
    sync_to_navidrome = "sync_to_navidrome" in request.form

    if not name:
        flash("Playlist name is required.", "danger")
        return redirect(url_for("playlists"))

    pl_id = db.upsert_playlist(
        name=name,
        m3u_path=m3u_path or None,
        plex_playlist_id=plex_id,
        navidrome_playlist_id=navi_id,
        sync_m3u_to_plex=sync_m3u_to_plex,
        sync_plex_to_m3u=sync_plex_to_m3u,
        sync_to_navidrome=sync_to_navidrome,
    )

    # Register m3u directory with watcher
    if m3u_path:
        d = os.path.dirname(m3u_path)
        if d and _watcher:
            _watcher.watch(d)

    flash(f"Playlist '{name}' added.", "success")
    return redirect(url_for("playlists"))


@app.route("/playlists/<int:playlist_id>/edit", methods=["POST"])
def playlist_edit(playlist_id):
    name = request.form.get("name", "").strip()
    m3u_path = request.form.get("m3u_path", "").strip()
    plex_id = request.form.get("plex_playlist_id", "").strip() or None
    navi_id = request.form.get("navidrome_playlist_id", "").strip() or None
    sync_m3u_to_plex = int("sync_m3u_to_plex" in request.form)
    sync_plex_to_m3u = int("sync_plex_to_m3u" in request.form)
    sync_to_navidrome = int("sync_to_navidrome" in request.form)

    db.update_playlist_fields(
        playlist_id,
        name=name,
        plex_playlist_id=plex_id,
        navidrome_playlist_id=navi_id,
        sync_m3u_to_plex=sync_m3u_to_plex,
        sync_plex_to_m3u=sync_plex_to_m3u,
        sync_to_navidrome=sync_to_navidrome,
    )
    if m3u_path:
        # m3u_path change needs upsert because it's a UNIQUE column
        db.upsert_playlist(
            name=name,
            m3u_path=m3u_path,
            plex_playlist_id=plex_id,
            navidrome_playlist_id=navi_id,
        )
        if _watcher:
            _watcher.watch(os.path.dirname(m3u_path))

    flash("Playlist updated.", "success")
    return redirect(url_for("playlists"))


@app.route("/playlists/<int:playlist_id>/delete", methods=["POST"])
def playlist_delete(playlist_id):
    pl = db.get_playlist(playlist_id)
    if pl:
        db.delete_playlist(playlist_id)
        flash(f"Playlist '{pl['name']}' removed.", "success")
    return redirect(url_for("playlists"))


# ── Sync API endpoints ────────────────────────────────────────────────────────

@app.route("/api/sync/<int:playlist_id>", methods=["POST"])
def api_sync(playlist_id):
    direction = request.args.get("direction", "full")
    engine = _get_engine()
    if direction == "m3u_to_plex":
        result = engine.sync_m3u_to_plex(playlist_id)
    elif direction == "plex_to_m3u":
        result = engine.sync_plex_to_m3u(playlist_id)
    elif direction == "to_navidrome":
        result = engine.sync_to_navidrome(playlist_id)
    else:
        result = engine.full_sync(playlist_id)
    return jsonify(result)


@app.route("/api/sync/all", methods=["POST"])
def api_sync_all():
    engine = _get_engine()
    results = {}
    for pl in db.get_playlists():
        results[pl["id"]] = engine.full_sync(pl["id"])
    return jsonify(results)


@app.route("/api/playlists/<int:playlist_id>/tracks")
def api_tracks(playlist_id):
    tracks = [dict(t) for t in db.get_sync_tracks(playlist_id)]
    return jsonify(tracks)


@app.route("/api/logs")
def api_logs():
    playlist_id = request.args.get("playlist_id", type=int)
    limit = request.args.get("limit", 100, type=int)
    logs = [dict(l) for l in db.get_logs(playlist_id=playlist_id, limit=limit)]
    return jsonify(logs)


@app.route("/api/status")
def api_status():
    return jsonify({
        "plex_connected": _plex is not None and _plex.connected,
        "navidrome_connected": _navi is not None and _navi.connected,
        "watched_playlists": len(db.get_playlists()),
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.route("/api/discover")
def api_discover():
    """
    Recursively scan M3U_DIRECTORY (or a supplied path) for .m3u / .m3u8 files.
    Returns a list of discovered files annotated with whether they are already
    registered in the database.
    """
    scan_root = request.args.get("path") or db.get_setting("m3u_directory", "")
    if not scan_root:
        return jsonify({"error": "No M3U directory configured. Set it in Settings first."}), 400
    if not os.path.isdir(scan_root):
        return jsonify({"error": f"Directory not found: {scan_root}"}), 400

    registered = {pl["m3u_path"] for pl in db.get_playlists() if pl["m3u_path"]}

    max_depth = int(request.args.get("depth", 1))

    def _scan():
        results = []
        root_depth = scan_root.rstrip(os.sep).count(os.sep)
        for dirpath, dirs, files in os.walk(scan_root):
            current_depth = dirpath.count(os.sep) - root_depth
            if current_depth >= max_depth:
                dirs.clear()  # prune — don't descend further
                continue
            for fname in sorted(files):
                if fname.lower().endswith((".m3u", ".m3u8")):
                    full = os.path.join(dirpath, fname)
                    results.append((dirpath, fname, full))
        return results

    found = []
    for dirpath, fname, full in eventlet.tpool.execute(_scan):  # noqa: E501
        found.append({
                    "path": full,
                    "name": os.path.splitext(fname)[0],
                    "registered": full in registered,
                    "rel": os.path.relpath(full, scan_root),
                })
    found.sort(key=lambda x: x["rel"].lower())
    return jsonify(found)


@app.route("/api/discover/import", methods=["POST"])
def api_discover_import():
    """Bulk-register a list of discovered m3u paths."""
    paths = request.json.get("paths", [])
    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    added = []
    skipped = []
    for path in paths:
        if not os.path.isfile(path):
            skipped.append({"path": path, "reason": "file not found"})
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        existing = db.get_playlist_by_m3u(path)
        if existing:
            skipped.append({"path": path, "reason": "already registered"})
            continue
        pl_id = db.upsert_playlist(name=name, m3u_path=path)
        if _watcher:
            _watcher.watch(os.path.dirname(path))
        added.append({"path": path, "name": name, "id": pl_id})

    return jsonify({"added": added, "skipped": skipped})


@app.route("/api/plex/playlists")
def api_plex_playlists():
    if not _plex or not _plex.connected:
        return jsonify({"error": "Plex not connected"}), 503
    return jsonify(_plex.list_playlists())


@app.route("/api/navidrome/playlists")
def api_navidrome_playlists():
    if not _navi or not _navi.connected:
        return jsonify({"error": "Navidrome not connected"}), 503
    return jsonify(_navi.list_playlists())


# ── Socket.IO events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    log.debug("Client connected")


@socketio.on("trigger_sync")
def on_trigger_sync(data):
    playlist_id = data.get("playlist_id")
    direction = data.get("direction", "full")
    if not playlist_id:
        return
    threading.Thread(
        target=_run_sync_in_thread,
        args=(playlist_id, direction),
        daemon=True,
    ).start()


def _run_sync_in_thread(playlist_id, direction):
    engine = _get_engine()
    if direction == "m3u_to_plex":
        engine.sync_m3u_to_plex(playlist_id)
    elif direction == "plex_to_m3u":
        engine.sync_plex_to_m3u(playlist_id)
    elif direction == "to_navidrome":
        engine.sync_to_navidrome(playlist_id)
    else:
        engine.full_sync(playlist_id)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
