#!/usr/bin/env python3
"""
MODULE TEMPLATE — copy this file, rename it (no leading underscore), and
build your module inside it. Files starting with "_" are ignored by the
shell's module scanner, so this template never appears on the Main Menu.

THE CONTRACT (everything a module must do):

1. Define a top-level MANIFEST dict of literals (the shell reads it with
   `ast` and never executes your code during a scan).

2. Accept two CLI arguments:
       --port N            port to serve your UI on
       --session PATH      path to config/session.json

   session.json gives you everything needed to run standalone:
       base_url                  Digital Commons instance base URL
       chrome.debugger_address   e.g. "127.0.0.1:9222"
       branding                  suite look-and-feel (name, colors, logo)

3. Run standalone: launched directly with those args (or with none — this
   template falls back to ../config/session.json and picks a free port), a
   module must work with no shell running at all.

Everything below is a small, working example: it serves a page in the suite's
branding, shows the session context it received, and includes the standard
`attach_chrome()` helper your real modules can reuse to get a Selenium driver
attached to the user's logged-in Chrome.
"""

# --------------------------------------------------------------------------
# 1. MANIFEST — the shell reads ONLY this during a scan.
#    "requires" lists pip package names; the shell's dependency checker
#    aggregates these across all installed modules.
# --------------------------------------------------------------------------
MANIFEST = {
    "id": "template-example",
    "name": "Template Example",
    "description": "A working example module. Copy, rename, and edit me.",
    "version": "1.0",
    "requires": [],            # e.g. ["selenium", "beautifulsoup4", "openpyxl"]
}

import argparse
import json
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

# --------------------------------------------------------------------------
# 2. Session context — how a module receives the Chrome address, base URL,
#    and branding from the shell (or finds them on its own when standalone).
# --------------------------------------------------------------------------
DEFAULT_SESSION = Path(__file__).resolve().parent.parent / "config" / "session.json"


# Neutral fallbacks, used ONLY when session.json is missing or unreadable —
# a standalone launch before the shell has ever written one. They are
# deliberately nobody's institution: this install's real identity lives in
# config/settings.json, seeded from config/profile.json (see
# app/settings.py and profiles/README.md). Identity refactor Step 3.
# Duplicated per module rather than imported, per the self-contained-module
# convention; the reference copy is in _template_module.py.
FALLBACK_BRAND = {"primary": "#1F4E79", "accent": "#946B2D",
                  "neutral": "#9AA5AD"}


def load_session(path: Path) -> dict:
    """Load session context, with safe fallbacks so the module always runs."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "base_url": "",
            "chrome": {"host": "127.0.0.1", "port": 9222,
                       "debugger_address": "127.0.0.1:9222"},
            "branding": {"suite_name": "DC Admin Suite",
                         "institution": "",
                         "colors": dict(FALLBACK_BRAND)},
        }


# --------------------------------------------------------------------------
# 3. Chrome attachment helper — reuse this in real modules.
#    Attaches Selenium to the ALREADY RUNNING, ALREADY LOGGED IN Chrome the
#    user started in debug mode. Never opens Chrome, never logs in.
# --------------------------------------------------------------------------
def attach_chrome(session: dict):
    """Return a Selenium WebDriver attached to the user's debug Chrome.

    NOTE: driver.quit() on an attached driver detaches WITHOUT closing the
    user's browser, but the polite pattern is to keep one driver for the
    module's lifetime. Requires:  pip install selenium
    """
    from selenium import webdriver                      # declared in requires
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_experimental_option(
        "debuggerAddress", session["chrome"]["debugger_address"])
    return webdriver.Chrome(options=opts)


# --------------------------------------------------------------------------
# 4. A minimal branded UI so you can see the wiring work end-to-end.
#    Real modules replace PAGE and the handler with their own logic
#    (keep the aria-live/status patterns for WCAG 2.1 AA).
#
#    SUITE RULE — CONTEXTUAL ERRORS MUST NOT AUTO-HIDE.
#    A contextual error (bad input, a folder that doesn't exist, a request
#    the server refused) stays on screen until the user clears it with the X
#    in its corner. Two things are therefore banned:
#
#      * writing an error into #status — the state poller rewrites that
#        element every 1.5 s, so the message vanishes before it can be read;
#      * setTimeout(..., N) that clears an error. Confirmations may fade;
#        errors may not.
#
#    Every module with a #status line carries the same three pieces. Copy
#    them verbatim into a new module rather than reinventing the styling:
#
#    CSS   #cerr / #cerr.show / #cerr.notice / .msg-x   (see any dc_*.py)
#    HTML  <div id="cerr" role="alert">
#            <span id="cerr-msg"></span>
#            <button type="button" class="msg-x" id="cerr-x"
#                    aria-label="Dismiss this message">&times;</button>
#          </div>
#          — placed immediately above <div id="status">.
#    JS    showErr(msg)  — error tone, stays until dismissed
#          showNote(msg) — neutral tone, same dismiss behaviour
#          clearErr()    — call at the top of each action handler, so a
#                          problem the user has fixed takes its message down
#          setMsg(el, text, isError) — for inline message spans: an error
#                          gets a close button and stays, a confirmation
#                          still fades after 4 s
# --------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — {suite}</title>
<style>
 body{{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f7f6f3;color:#1b1b1f}}
 header{{background:{primary};color:{onprimary};padding:1rem 1.25rem}}
 header h1{{margin:0;font-size:1.2rem}}
 header a{{color:{onprimary}}}
 .bar{{height:5px;background:{accent}}}
 main{{max-width:46rem;margin:0 auto;padding:1.25rem}}
 pre{{background:#eef1f5;border:1px solid #d7d4cc;border-radius:6px;
     padding:.8rem;overflow-x:auto;font-size:.85rem}}
 :focus-visible{{outline:3px solid #1a5dc8;outline-offset:2px}}
</style></head><body>
<header><h1>{name}</h1>
<p>{institution} · <a href="{hub}" target="dcAdminSuiteHub">back to suite</a></p></header>
<div class="bar" role="presentation"></div>
<main>
<h2>This module is running standalone</h2>
<p>It received the session context below from the shell and no longer
depends on it. Working on: <strong>{base_url}</strong></p>
<pre tabindex="0">{ctx}</pre>
</main></body></html>"""


# --------------------------------------------------------------------------
# 4b. Local-request guard — copy this into every module. The server binds to
#     127.0.0.1, but a malicious web page could still reach it via DNS
#     rebinding (bad Host header) or a cross-site form/fetch (foreign Origin
#     header). Call request_allowed() first in do_GET and do_POST.
# --------------------------------------------------------------------------
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "")


def request_allowed(handler) -> bool:
    host = (handler.headers.get("Host") or "").rsplit(":", 1)[0].lower()
    if host not in _LOCAL_HOSTS:
        return False
    origin = handler.headers.get("Origin")
    if origin:
        if origin.lower() == "null":     # sandboxed/file: pages - reject
            return False
        if (urlparse(origin).hostname or "").lower() not in _LOCAL_HOSTS:
            return False
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=MANIFEST["name"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--session", default=str(DEFAULT_SESSION))
    args = parser.parse_args()

    session = load_session(Path(args.session))
    port = args.port or free_port()
    brand = session.get("branding", {})
    colors = brand.get("colors", {})

    html = PAGE.format(
        name=MANIFEST["name"],
        suite=brand.get("suite_name", "DC Admin Suite"),
        institution=brand.get("institution", ""),
        primary=colors.get("primary", FALLBACK_BRAND["primary"]),
        accent=colors.get("accent", FALLBACK_BRAND["accent"]),
        onprimary="#ffffff",
        hub=session.get("hub_url", "#"),
        base_url=session.get("base_url", ""),
        ctx=json.dumps(session, indent=2)
            .replace("&", "&amp;").replace("<", "&lt;"),
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not request_allowed(self):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:{}".format(port)
    print("{} running at {}".format(MANIFEST["name"], url))
    if args.port is None:  # standalone launch → open a browser ourselves
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
