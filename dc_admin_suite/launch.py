#!/usr/bin/env python3
"""
DC Admin Suite — Launcher
=========================
The only file at the top level of the suite. Everything else lives in
subdirectories:

    dc_admin_suite/
    ├── launch.py        ← you are here (double-click or `python3 launch.py`)
    ├── app/             ← the suite "shell" (server, UI, settings, helpers)
    ├── modules/         ← drop-in admin modules (self-contained files)
    └── config/          ← created at runtime: settings.json, session.json, logo

Usage:
    python3 launch.py                 # start the shell, open browser to Splash
    python3 launch.py --port 8750     # use a specific port
    python3 launch.py --no-browser    # start without opening a browser tab

The suite is pure standard library. Modules may declare third-party
dependencies (e.g. selenium); the shell checks those for you at startup.

Cross-platform: Windows / macOS (and Linux). Python 3.9+.
"""

import sys
from pathlib import Path

# Make the suite importable no matter where it was launched from,
# so the whole directory stays transportable.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
