"""
HTTP server for the shell UI — pure standard library.

Routes
------
GET  /                      the single-page shell (Splash / Menu / Settings)
GET  /api/state             settings + session flags + instructions + modules
POST /api/settings          partial settings update (applied immediately)
POST /api/chrome/check      attempt/verify Chrome debug connection
POST /api/deps/check        rescan modules & check aggregated dependencies
GET  /api/modules           fresh module scan (Main Menu refresh)
POST /api/modules/launch    {"id": ...} → spawn module, return its URL
POST /api/modules/install   {"filename": ..., "data": base64} → save to modules/
POST /api/modules/remove    {"id": ...} → move file to modules/_removed/
POST /api/modules/order     {"order": [id, ...]} → save Main Menu display order
POST /api/branding/logo     {"filename": ..., "data": base64} → set logo
POST /api/branding/logo/clear
GET  /branding/logo         serve the current logo image
POST /api/splash/complete   mark splash finished for this session
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import settings as settings_mod
from . import ai_endpoints
from . import chrome_bridge, dependencies, module_registry
from .ui import APP_HTML

# Per-session (in-memory) state. Restarting the suite starts a new session,
# which is what re-shows the Splash Page.
SESSION = {
    "splash_complete": False,
    "chrome_ok": False,
    "deps_ok": False,
    "dc_tab_found": False,
    "attach_ok": False,
    # Remembered from the last Chrome check so the dependency check can
    # compare a chromedriver on PATH against the browser it would drive.
    "browser": "",
}
_LOCK = threading.Lock()

_LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".svg": "image/svg+xml", ".webp": "image/webp"}

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "")


def request_allowed(handler) -> bool:
    """Reject requests that did not originate locally. The server binds to
    127.0.0.1, but a malicious web page could still POST here via DNS
    rebinding (bad Host header) or a cross-site form/fetch (foreign Origin
    header). Browsers always send Origin on cross-origin POSTs, so requiring
    a local-or-absent Origin plus a local Host closes both holes."""
    host = (handler.headers.get("Host") or "").rsplit(":", 1)[0].lower()
    if host not in _LOCAL_HOSTS:
        return False
    origin = handler.headers.get("Origin")
    if origin:
        if origin.lower() == "null":     # sandboxed/file: pages — reject
            return False
        if (urlparse(origin).hostname or "").lower() not in _LOCAL_HOSTS:
            return False
    return True


def _state_payload() -> dict:
    settings = settings_mod.load_settings()
    with _LOCK:
        session = dict(SESSION)
    return {
        "settings": settings,
        # The profile-backed defaults, so the UI can offer "reset to
        # defaults" without hardcoding an institution's values in JS.
        "defaults": settings_mod.DEFAULT_SETTINGS,
        "session": session,
        "instructions": chrome_bridge.chrome_instructions(
            settings["chrome"]["port"]),
        "modules": module_registry.scan_modules(),
        "has_logo": bool(settings["branding"].get("logo")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DCAdminSuite/1.2"

    # ---- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def log_message(self, *args):  # keep the console quiet
        pass

    # ---- GET ---------------------------------------------------------------
    def do_GET(self):
        # Same never-drop guarantee as do_POST: always answer JSON on an
        # unexpected error so the UI has something actionable to show.
        try:
            self._do_get()
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            try:
                self._json({"ok": False,
                            "error": "Unexpected error: {}".format(exc)}, 500)
            except OSError:
                pass

    def _do_get(self):
        if not request_allowed(self):
            return self._json({"error": "Forbidden (non-local request)."}, 403)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, APP_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(_state_payload())
        elif path == "/api/modules":
            self._json({"modules": module_registry.scan_modules()})
        elif path == "/api/ai":
            self._json({"config": ai_endpoints.load()})
        elif path == "/branding/logo":
            self._serve_logo()
        else:
            self._json({"error": "not found"}, 404)

    def _serve_logo(self):
        settings = settings_mod.load_settings()
        name = settings["branding"].get("logo")
        file = settings_mod.BRANDING_DIR / name if name else None
        if not (name and file and file.exists()):
            self._json({"error": "no logo"}, 404)
            return
        ctype = _LOGO_TYPES.get(file.suffix.lower(), "application/octet-stream")
        self._send(200, file.read_bytes(), ctype)

    # ---- POST --------------------------------------------------------------
    def do_POST(self):
        # Never drop a connection on an unexpected error — always answer JSON
        # so the UI can show something actionable.
        try:
            self._do_post()
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            try:
                self._json({"ok": False,
                            "error": "Unexpected error: {}".format(exc)}, 500)
            except OSError:
                pass

    def _do_post(self):
        if not request_allowed(self):
            return self._json({"error": "Forbidden (non-local request)."}, 403)
        path = self.path.split("?", 1)[0]
        body = self._read_json() or {}

        if path == "/api/settings":
            # Validate + normalize server-side before persisting — modules
            # depend on these values being well-formed.
            if "base_url" in body:
                msg = settings_mod.validate_base_url(body["base_url"])
                if msg:
                    self._json({"ok": False, "error": msg}, 400)
                    return
                body["base_url"] = str(body["base_url"]).strip().rstrip("/")
            if "chrome" in body and isinstance(body["chrome"], dict):
                ch = body["chrome"]
                if "port" in ch:
                    try:
                        port = int(ch["port"])
                        if not 1 <= port <= 65535:
                            raise ValueError
                    except (TypeError, ValueError):
                        self._json({"ok": False, "error":
                                    "Debugger port must be a number between "
                                    "1 and 65535."}, 400)
                        return
                    ch["port"] = port
                if "host" in ch and not str(ch["host"]).strip():
                    ch["host"] = "127.0.0.1"
            if "crossref_prefix" in body:
                msg = settings_mod.validate_crossref_prefix(
                    body["crossref_prefix"])
                if msg:
                    self._json({"ok": False, "error": msg}, 400)
                    return
                body["crossref_prefix"] = settings_mod.normalize_crossref_prefix(
                    body["crossref_prefix"])
            if "crossref" in body:
                msg = settings_mod.validate_crossref_identity(body["crossref"])
                if msg:
                    self._json({"ok": False, "error": msg}, 400)
                    return
                body["crossref"] = settings_mod.normalize_crossref_identity(
                    body["crossref"])
            merged = settings_mod.update_settings(body)
            # Keep the module session context fresh so any module launched
            # after this change starts with the new settings.
            settings_mod.write_session_context(merged)
            self._json({"ok": True, "settings": merged})

        elif path == "/api/ai":
            cand = ai_endpoints.normalize(body)
            msg = ai_endpoints.validate(cand)
            if msg:
                self._json({"ok": False, "error": msg}, 400)
                return
            saved = ai_endpoints.save(cand)
            self._json({"ok": True, "config": saved})

        elif path == "/api/ai/test":
            cfg = ai_endpoints.load()
            ep = ai_endpoints.find(cfg, str(body.get("name", "")).strip())
            if ep is None:
                self._json({"ok": False, "error": "Endpoint not found."}, 400)
                return
            result = ai_endpoints.test_endpoint(ep)
            self._json(result, 200 if result.get("ok") else 400)

        elif path == "/api/chrome/check":
            settings = settings_mod.load_settings()
            result = chrome_bridge.check_connection(settings)
            with _LOCK:
                SESSION["chrome_ok"] = result["ok"]
                SESSION["dc_tab_found"] = result["dc_tab_found"]
                SESSION["attach_ok"] = result["attach_ok"]
                SESSION["browser"] = result["browser"]
            self._json(result)

        elif path == "/api/deps/check":
            with _LOCK:
                browser = SESSION.get("browser", "")
            result = dependencies.run_check(
                module_registry.scan_modules(), browser)
            with _LOCK:
                SESSION["deps_ok"] = result["all_ok"]
            self._json(result)

        elif path == "/api/modules/launch":
            settings = settings_mod.load_settings()
            self._json(module_registry.launch_module(
                str(body.get("id", "")), settings))

        elif path == "/api/modules/install":
            self._json(module_registry.install_module(
                str(body.get("filename", "")), str(body.get("data", ""))))

        elif path == "/api/modules/remove":
            self._json(module_registry.remove_module(str(body.get("id", ""))))

        elif path == "/api/modules/order":
            order = body.get("order")
            if not isinstance(order, list):
                self._json({"ok": False,
                            "error": "order must be a list of module ids."}, 400)
                return
            settings_mod.set_module_order(order)
            # Return the freshly ordered module list so the UI re-renders in
            # the exact order the shell will use everywhere.
            self._json({"ok": True,
                        "modules": module_registry.scan_modules()})

        elif path == "/api/branding/logo":
            self._json(self._save_logo(body))

        elif path == "/api/branding/logo/clear":
            settings_mod.update_settings({"branding": {"logo": ""}})
            self._json({"ok": True})

        elif path == "/api/splash/complete":
            with _LOCK:
                SESSION["splash_complete"] = True
            self._json({"ok": True})

        else:
            self._json({"error": "not found"}, 404)

    def _save_logo(self, body: dict) -> dict:
        name = Path(str(body.get("filename", ""))).name
        suffix = Path(name).suffix.lower()
        if suffix not in _LOGO_TYPES:
            return {"ok": False, "error":
                    "Logo must be a PNG, JPG, GIF, SVG, or WebP image."}
        try:
            raw = base64.b64decode(str(body.get("data", "")))
        except Exception:
            return {"ok": False, "error": "Could not read the uploaded file."}
        if len(raw) > 2_000_000:
            return {"ok": False, "error": "Logo must be under 2 MB."}
        settings_mod.ensure_dirs()
        dest_name = "logo" + suffix
        (settings_mod.BRANDING_DIR / dest_name).write_bytes(raw)
        settings_mod.update_settings({"branding": {"logo": dest_name}})
        return {"ok": True, "file": dest_name}


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server
