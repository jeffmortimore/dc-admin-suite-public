"""
Shell bootstrap: load settings, start the HTTP server, open the browser.
"""

import argparse
import webbrowser
import threading

from . import settings as settings_mod
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="DC Admin Suite shell")
    parser.add_argument("--port", type=int, default=None,
                        help="port for the shell UI (default: from settings)")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser tab automatically")
    args = parser.parse_args()

    settings = settings_mod.load_settings()
    if args.port:
        settings = settings_mod.update_settings({"hub_port": args.port})
    port = int(settings["hub_port"])

    # Write the initial session context so modules launched by hand also work.
    settings_mod.write_session_context(settings)

    server = serve(port)
    url = "http://127.0.0.1:{}/".format(port)
    print("DC Admin Suite running at {}  (Ctrl+C to stop)".format(url))

    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
