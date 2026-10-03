"""
Module registry — discover, launch, install, and remove modules.

THE MODULE CONTRACT
-------------------
A module is a single self-contained .py file saved in the modules/ directory.
It must define a top-level MANIFEST dictionary of literals:

    MANIFEST = {
        "id":          "structure-inventory",     # unique, url-safe
        "name":        "Structure Inventory",     # shown on the Main Menu
        "description": "Exports every publication structure to a spreadsheet.",
        "version":     "1.0",
        "requires":    ["selenium", "openpyxl"],  # pip package names
    }

and accept two command-line arguments when launched:

    python3 module.py --port 8751 --session /path/to/config/session.json

session.json gives the module everything it needs to run standalone after
launch: the Chrome debugger address, the Digital Commons base URL, and the
suite's look-and-feel settings. See modules/_template_module.py for a fully
worked, copy-ready example.

DISCOVERY RULES
---------------
* Only top-level .py files in modules/ are scanned.
* Files whose names start with "_" are ignored (use this for templates,
  helpers, or to temporarily disable a module).
* Manifests are read with `ast` — module code is NEVER executed during a
  scan, so a module with missing dependencies or syntax errors can't break
  the shell; it just shows up with an error note.

Install a module  = save its .py file into modules/ (UI or file manager).
Remove a module   = the UI moves it to modules/_removed/ (recoverable),
                    or simply delete the file yourself.
"""

import ast
import base64
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import settings as settings_mod

MODULES_DIR = settings_mod.MODULES_DIR
REMOVED_DIR = MODULES_DIR / "_removed"

REQUIRED_KEYS = ("id", "name", "description")

# Track processes we started this session: module id → subprocess.Popen
RUNNING = {}
# ...and the URL each one serves, so opening a module that is already
# running returns to it (shell v1.11). See launch_module().
RUNNING_URLS = {}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _manifest_from_source(source: str) -> dict:
    """Extract MANIFEST from module source code without executing it."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MANIFEST":
                    value = ast.literal_eval(node.value)  # literals only
                    if not isinstance(value, dict):
                        raise ValueError("MANIFEST must be a dict")
                    return value
    raise ValueError("no top-level MANIFEST found")


def _parse_manifest(path: Path) -> dict:
    """Extract MANIFEST from a module file without executing it."""
    return _manifest_from_source(
        path.read_text(encoding="utf-8", errors="replace"))


def scan_modules() -> list:
    """Fresh scan of modules/ (called on every request that needs the list,
    so adding/removing files mid-session is picked up automatically)."""
    settings_mod.ensure_dirs()
    modules = []
    for path in sorted(MODULES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        entry = {
            "file": path.name, "id": path.stem, "name": path.stem,
            "description": "", "version": "", "requires": [], "error": "",
        }
        try:
            manifest = _parse_manifest(path)
            missing = [k for k in REQUIRED_KEYS if not manifest.get(k)]
            if missing:
                raise ValueError(
                    "MANIFEST missing required key(s): " + ", ".join(missing)
                )
            entry.update({
                "id": str(manifest["id"]),
                "name": str(manifest["name"]),
                "description": str(manifest["description"]),
                "version": str(manifest.get("version", "")),
                "requires": [str(r) for r in manifest.get("requires", [])],
            })
        except (SyntaxError, ValueError, OSError) as exc:
            entry["error"] = str(exc)
        entry["running"] = _is_running(entry["id"])
        modules.append(entry)

    # Duplicate-id guard: keep first, flag the rest
    seen = set()
    for m in modules:
        if m["id"] in seen and not m["error"]:
            m["error"] = "duplicate module id '{}' (also used by another file)".format(m["id"])
        seen.add(m["id"])

    # Apply the saved Main Menu display order. Modules absent from the saved
    # order fall to the end (alphabetically by name), so installing a module
    # mid-session never disturbs a previously set order.
    return settings_mod.order_modules(modules)


def _is_running(module_id: str) -> bool:
    proc = RUNNING.get(module_id)
    return bool(proc and proc.poll() is None)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
def _free_port(start: int) -> int:
    """First available localhost port at or above `start`."""
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port found near {}".format(start))


def launch_module(module_id: str, settings: dict) -> dict:
    """
    Launch a module as an independent subprocess. The module receives the
    current session context (Chrome address, base URL, branding) and its own
    port, then runs standalone — it keeps working even if the shell is closed.
    """
    modules = scan_modules()
    target = next((m for m in modules if m["id"] == module_id), None)
    if target is None:
        return {"ok": False, "error": "Module '{}' not found.".format(module_id)}

    # Already running? Go back to it. 2026-09-30: a module tab was closed
    # during a long download, and opening the module again started a SECOND
    # copy on the next port — an idle page that looked as if the job had
    # vanished, while the real one ran on unseen. Worse, that copy would
    # have accepted Start. A module runs until its process ends, so the
    # process we started is the one to return to.
    if _is_running(module_id) and RUNNING_URLS.get(module_id):
        proc = RUNNING[module_id]
        return {"ok": True, "url": RUNNING_URLS[module_id],
                "port": int(RUNNING_URLS[module_id].rsplit(":", 1)[1]),
                "pid": proc.pid, "name": target["name"], "reused": True}
    if target["error"]:
        return {"ok": False,
                "error": "Module can't be launched: " + target["error"]}

    # Refresh the session context so the module starts with current settings.
    session_file = settings_mod.write_session_context(settings)
    port = _free_port(int(settings["hub_port"]) + 1)
    path = MODULES_DIR / target["file"]

    try:
        proc = subprocess.Popen(
            [sys.executable, str(path),
             "--port", str(port),
             "--session", str(session_file)],
            cwd=str(MODULES_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"ok": False, "error": "Failed to start module: {}".format(exc)}

    RUNNING[module_id] = proc
    RUNNING_URLS[module_id] = "http://127.0.0.1:{}".format(port)
    time.sleep(0.6)  # give it a moment; report early exit as an error
    if proc.poll() is not None:
        return {"ok": False, "error":
                "Module exited immediately (code {}). Run it by hand to see "
                "the error:  python3 modules/{} --port {} --session "
                "config/session.json".format(proc.returncode, target["file"], port)}

    return {"ok": True, "url": RUNNING_URLS[module_id],
            "port": port, "pid": proc.pid, "name": target["name"],
            "reused": False}


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*\.py$")


def install_module(filename: str, data_b64: str) -> dict:
    """Save an uploaded module file into modules/ (validating it first)."""
    settings_mod.ensure_dirs()
    filename = Path(filename).name  # strip any path components
    if not _SAFE_NAME.match(filename):
        return {"ok": False, "error":
                "File name must be a .py file (letters, digits, - and _ "
                "only, not starting with _)."}
    try:
        raw = base64.b64decode(data_b64)
        text = raw.decode("utf-8")
    except Exception:
        return {"ok": False, "error": "File is not valid UTF-8 Python text."}

    # Validate IN MEMORY before touching the filesystem: the file must parse
    # and must carry a usable MANIFEST.
    try:
        manifest = _manifest_from_source(text)
        missing = [k for k in REQUIRED_KEYS if not manifest.get(k)]
        if missing:
            raise ValueError(
                "MANIFEST missing required key(s): " + ", ".join(missing))
    except (SyntaxError, ValueError) as exc:
        return {"ok": False, "error": "Not a valid module: {}".format(exc)}

    dest = MODULES_DIR / filename
    replaced = dest.exists()
    dest.write_text(text, encoding="utf-8")
    return {"ok": True, "file": filename, "name": manifest["name"],
            "replaced": replaced}


def remove_module(module_id: str) -> dict:
    """Move a module file to modules/_removed/ (recoverable delete)."""
    target = next((m for m in scan_modules() if m["id"] == module_id), None)
    if target is None:
        return {"ok": False, "error": "Module '{}' not found.".format(module_id)}
    REMOVED_DIR.mkdir(parents=True, exist_ok=True)
    src = MODULES_DIR / target["file"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = REMOVED_DIR / "{}.{}".format(target["file"], stamp)
    try:
        src.replace(dest)
    except OSError as exc:
        return {"ok": False, "error": "Could not remove: {}".format(exc)}
    return {"ok": True, "file": target["file"],
            "moved_to": "modules/_removed/" + dest.name}
