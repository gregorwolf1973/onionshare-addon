"""
OnionShare HA Addon - Flask web UI

Architecture
------------
We don't link to onionshare-cli as a library because its CLI invocation
gives us cleaner process isolation per share. Each "session" is one
onionshare-cli subprocess managed by SessionManager. We parse its stdout
to extract the .onion address and progress info.

Routes
------
GET  /                       -> main UI
GET  /api/status             -> tor + sessions overview
GET  /api/browse?path=...    -> filesystem browser (for picking files/dirs)
POST /api/session            -> start new session
POST /api/session/<id>/stop  -> stop one
GET  /api/session/<id>/log   -> stream logs (SSE)
"""

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
import socket
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("ONIONSHARE_DATA_DIR", "/data/onionshare"))
CONFIG_DIR = Path(os.environ.get("ONIONSHARE_CONFIG_DIR", "/config/onionshare"))
PERSISTENT_ONIONS = os.environ.get("PERSISTENT_ONIONS", "true").lower() == "true"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Roots the user is allowed to browse / share from.
# Order matters - first match wins for path resolution.
BROWSE_ROOTS = {
    "share":  Path("/share"),
    "media":  Path("/media"),
    "backup": Path("/backup"),
    "config": Path("/config"),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("onionshare-addon")

# ---------------------------------------------------------------------------
# Reverse-proxy middleware (for HA Ingress)
# ---------------------------------------------------------------------------


class ReverseProxied:
    """Make Flask aware that we run behind HA Ingress."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get("HTTP_X_INGRESS_PATH", "")
        if script_name:
            environ["SCRIPT_NAME"] = script_name
            path_info = environ.get("PATH_INFO", "")
            if path_info.startswith(script_name):
                environ["PATH_INFO"] = path_info[len(script_name):]
        scheme = environ.get("HTTP_X_FORWARDED_PROTO")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
        return self.app(environ, start_response)


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

# Regex patterns to parse onionshare-cli stdout
RE_ONION_URL = re.compile(r"(https?://[a-z2-7]{56}\.onion[^\s]*)")
RE_AUTH_KEY = re.compile(r"Private key:\s+(\S+)")
RE_HIDDEN_AUTH = re.compile(r"client-onion-auth-cookie\s*=\s*(\S+)", re.IGNORECASE)
RE_READY = re.compile(r"(Give this address|Files are ready|chat room is ready|website|hosting)", re.IGNORECASE)


class Session:
    """One running onionshare-cli subprocess."""

    def __init__(self, sid, mode, args, paths, public, label):
        self.id = sid
        self.mode = mode            # share | receive | website | chat
        self.args = args            # full argv list
        self.paths = paths          # list of paths being shared (for display)
        self.public = public
        self.label = label or mode.capitalize()
        self.created_at = time.time()
        self.proc = None
        self.onion_url = None
        self.private_key = None
        self.status = "starting"    # starting | ready | stopped | error
        self.log_lines = deque(maxlen=500)
        self._reader_thread = None
        self._lock = threading.Lock()

    def start(self):
        log.info("Session %s starting: %s", self.id, " ".join(shlex.quote(a) for a in self.args))
        try:
            self.proc = subprocess.Popen(
                self.args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                # New process group so we can SIGTERM the whole tree
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self.status = "error"
            self.log_lines.append(f"[startup error] {e}")
            log.exception("Failed to start session %s", self.id)
            return

        self._reader_thread = threading.Thread(
            target=self._read_output, name=f"session-{self.id}-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_output(self):
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip()
            with self._lock:
                self.log_lines.append(line)

            m = RE_ONION_URL.search(line)
            if m and not self.onion_url:
                self.onion_url = m.group(1)
                log.info("Session %s got onion: %s", self.id, self.onion_url)

            m = RE_AUTH_KEY.search(line)
            if m and not self.private_key:
                self.private_key = m.group(1)

            if self.status == "starting" and (self.onion_url or RE_READY.search(line)):
                self.status = "ready"

        # Process ended
        rc = self.proc.wait() if self.proc else -1
        with self._lock:
            self.log_lines.append(f"[process exited rc={rc}]")
            if self.status != "stopped":
                self.status = "error" if rc != 0 else "stopped"
        log.info("Session %s ended rc=%s", self.id, rc)

    def stop(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            return
        log.info("Stopping session %s", self.id)
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.status = "stopped"

    def to_dict(self):
        return {
            "id": self.id,
            "mode": self.mode,
            "label": self.label,
            "paths": [str(p) for p in self.paths],
            "public": self.public,
            "status": self.status,
            "onion_url": self.onion_url,
            "private_key": self.private_key,
            "created_at": self.created_at,
            "log_tail": list(self.log_lines)[-20:],
        }


class SessionManager:
    def __init__(self):
        self.sessions = {}
        self._lock = threading.Lock()

    def list(self):
        with self._lock:
            return [s.to_dict() for s in self.sessions.values()]

    def get(self, sid):
        with self._lock:
            return self.sessions.get(sid)

    def add(self, session):
        with self._lock:
            self.sessions[session.id] = session

    def remove(self, sid):
        with self._lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.stop()
        return s is not None

    def stop_all(self):
        with self._lock:
            sids = list(self.sessions.keys())
        for sid in sids:
            self.remove(sid)


manager = SessionManager()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def resolve_safe(path_str):
    """Resolve `path_str` to a real path, ensuring it stays under one of BROWSE_ROOTS."""
    if not path_str:
        return None
    p = Path(path_str).expanduser()
    try:
        real = p.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return None
    for root in BROWSE_ROOTS.values():
        try:
            real.relative_to(root.resolve())
            return real
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Tor health check
# ---------------------------------------------------------------------------


def tor_status():
    """Quick check: is the SOCKS port up? Use plain TCP, no actual handshake."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 9050), timeout=2):
            return {"running": True, "socks_port": 9050}
    except OSError as e:
        return {"running": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Build onionshare-cli command
# ---------------------------------------------------------------------------


def build_command(mode, paths, public, custom_title=None, chat_room=None):
    """Translate a user request into an onionshare-cli argv list.

    onionshare-cli flags (as of recent versions):
      --receive            -> receive mode
      --website            -> website mode
      --chat               -> chat mode
      (no flag)            -> share mode (default)
      --public             -> no private key required
      --persistent FILE    -> store key in FILE so address survives restarts
      --title TITLE
    Positional args at end = files/folders to share.
    """
    cmd = ["onionshare-cli"]

    if mode == "receive":
        cmd.append("--receive")
        # Where uploads should land. We pass a fixed dir under /share.
        upload_dir = Path("/share/onionshare-uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--data-dir", str(upload_dir)]
    elif mode == "website":
        cmd.append("--website")
        # Disable CSP so the site works for users who include their own scripts
        cmd.append("--disable-csp")
    elif mode == "chat":
        cmd.append("--chat")
    elif mode == "share":
        pass  # default
    else:
        raise ValueError(f"unknown mode: {mode}")

    if public:
        cmd.append("--public")

    if custom_title:
        cmd += ["--title", custom_title]

    if PERSISTENT_ONIONS:
        # One persistent file per (mode, paths) combo. We hash the input.
        import hashlib
        key = hashlib.sha256(
            (mode + "|" + "|".join(sorted(str(p) for p in paths)) + "|" + (chat_room or "")).encode()
        ).hexdigest()[:16]
        persist_file = DATA_DIR / f"persist-{mode}-{key}.json"
        cmd += ["--persistent", str(persist_file)]

    # Positional args: only share & website take files/folders
    if mode in ("share", "website"):
        if not paths:
            raise ValueError("share/website mode needs at least one file or folder")
        cmd += [str(p) for p in paths]

    return cmd


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.wsgi_app = ReverseProxied(app.wsgi_app)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "tor": tor_status(),
        "sessions": manager.list(),
        "persistent_onions": PERSISTENT_ONIONS,
        "roots": list(BROWSE_ROOTS.keys()),
    })


@app.route("/api/browse")
def api_browse():
    """List one directory under one of the BROWSE_ROOTS."""
    root_name = request.args.get("root", "share")
    sub = request.args.get("path", "")

    if root_name not in BROWSE_ROOTS:
        return jsonify({"error": "invalid root"}), 400

    root = BROWSE_ROOTS[root_name]
    if not root.exists():
        return jsonify({"error": f"{root} does not exist (not mounted?)", "entries": []}), 200

    target = (root / sub.lstrip("/")).resolve()

    # Anti-path-traversal: target must stay under root
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return jsonify({"error": "path escapes root"}), 400

    if not target.exists():
        return jsonify({"error": "not found"}), 404
    if not target.is_dir():
        return jsonify({"error": "not a directory"}), 400

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                stat = child.stat()
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size": stat.st_size if child.is_file() else None,
                    "mtime": int(stat.st_mtime),
                })
            except OSError:
                continue
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403

    return jsonify({
        "root": root_name,
        "path": str(target),
        "parent": str(target.parent) if target != root.resolve() else None,
        "entries": entries,
    })


@app.route("/api/session", methods=["POST"])
def api_session_create():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode", "share")
    raw_paths = body.get("paths", [])
    public = bool(body.get("public", False))
    title = (body.get("title") or "").strip() or None
    label = (body.get("label") or "").strip() or None

    # Validate paths against safe roots (chat needs none)
    safe_paths = []
    if mode in ("share", "website"):
        for rp in raw_paths:
            sp = resolve_safe(rp)
            if not sp:
                return jsonify({"error": f"path not allowed: {rp}"}), 400
            safe_paths.append(sp)
        if not safe_paths:
            return jsonify({"error": "at least one path required for this mode"}), 400

    try:
        cmd = build_command(mode, safe_paths, public, custom_title=title)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    sid = uuid.uuid4().hex[:12]
    session = Session(sid, mode, cmd, safe_paths, public, label)
    session.start()
    manager.add(session)

    return jsonify({"id": sid, "session": session.to_dict()}), 201


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_session_delete(sid):
    ok = manager.remove(sid)
    return jsonify({"removed": ok})


@app.route("/api/session/<sid>", methods=["GET"])
def api_session_get(sid):
    s = manager.get(sid)
    if not s:
        return jsonify({"error": "not found"}), 404
    return jsonify(s.to_dict())


@app.route("/api/session/<sid>/log")
def api_session_log(sid):
    """Server-Sent Events stream of new log lines."""
    s = manager.get(sid)
    if not s:
        return jsonify({"error": "not found"}), 404

    def gen():
        last_idx = 0
        while True:
            time.sleep(0.5)
            with s._lock:
                lines = list(s.log_lines)
            new_lines = lines[last_idx:]
            last_idx = len(lines)
            for line in new_lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
            yield f"event: status\ndata: {json.dumps({'status': s.status, 'onion_url': s.onion_url})}\n\n"
            if s.status in ("stopped", "error"):
                yield "event: end\ndata: {}\n\n"
                break

    return Response(gen(), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _install_safe_getfqdn():
    """Keep the reverse DNS lookup during bind from killing the addon.

    http.server calls socket.getfqdn() while binding. With host_network the
    addon uses the router's DNS; a PTR record that is not valid UTF-8 then
    raises UnicodeDecodeError and the addon never starts.
    """
    real_getfqdn = socket.getfqdn

    def safe_getfqdn(name=""):
        try:
            return real_getfqdn(name)
        except (UnicodeDecodeError, UnicodeError, OSError):
            return name or "localhost"

    socket.getfqdn = safe_getfqdn


if __name__ == "__main__":
    _install_safe_getfqdn()
    # Check onionshare-cli is callable
    if not shutil.which("onionshare-cli"):
        log.error("onionshare-cli not found in PATH!")
    else:
        try:
            v = subprocess.run(
                ["onionshare-cli", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            log.info("onionshare-cli version: %s", (v.stdout or v.stderr).strip())
        except Exception as e:
            log.warning("Could not get onionshare-cli version: %s", e)

    app.run(host="0.0.0.0", port=8099, threaded=True)
