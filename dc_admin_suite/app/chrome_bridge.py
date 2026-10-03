"""
Chrome bridge — connect to a user-started Chrome running in debug mode.

Design principle: the SHELL never opens Chrome and never logs the user in.
The user starts Chrome with --remote-debugging-port themselves, logs into
Digital Commons as an administrator, and clicks the acknowledgment button.
The shell then verifies the connection over Chrome's DevTools HTTP endpoint
(pure standard library — no selenium needed for the check itself).

Modules attach with Selenium via Options.debugger_address using the values
in config/session.json. A ready-made helper for that lives in the module
template (modules/_template_module.py).
"""

import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

from . import dependencies


def _fetch_json(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe_devtools(host: str, port: int) -> dict:
    """Hit DevTools /json/version. Returns browser info or raises."""
    return _fetch_json("http://{}:{}/json/version".format(host, port))


def list_tabs(host: str, port: int) -> list:
    """List open page targets (tabs) in the debug Chrome."""
    data = _fetch_json("http://{}:{}/json/list".format(host, port))
    return [t for t in data if t.get("type") == "page"]


# ---------------------------------------------------------------------------
# The attach attempt.
#
# Until 2026-09-09 the check below verified the debug port, Chrome's version,
# the tab count, that a Digital Commons tab was open, and that selenium was
# IMPORTABLE — and then told the user "you're set". It never attempted a
# session, which is the one thing every module actually does, so a stale
# chromedriver produced a green splash and a module that could not start.
# A check that verifies the adjacent thing is worse than no check, because
# it produces confidence.
#
# The driver created here is a THROWAWAY and must never disturb the user's
# browser. driver.quit() on an attached session asks Chrome to close, so it
# is not used: the chromedriver process is released with
# driver.service.stop(), which leaves the window exactly as it was found.
# ---------------------------------------------------------------------------
def try_attach(host: str, port: int, page_load_timeout: float = 20.0) -> dict:
    """Attach a throwaway Selenium driver to the debug Chrome, then let go.

    Returns {"ok", "error", "driver_version", "title"}. `error` carries the
    UNDERLYING exception text, not a friendly paraphrase: Selenium's
    SessionNotCreatedException names both versions and the remedy, and a
    tidied-up message throws all of that away. That cost a false start once
    already.
    """
    out = {"ok": False, "error": "", "driver_version": "", "title": ""}
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception as e:                    # noqa: BLE001 — reported, not swallowed
        out["error"] = "selenium could not be imported: {}: {}".format(
            e.__class__.__name__, e)
        return out

    try:
        opts = Options()
        opts.add_experimental_option(
            "debuggerAddress", "{}:{}".format(host, port))
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(page_load_timeout)
        # Read something trivial from the live session WITHOUT navigating —
        # the user's tab must be exactly where they left it.
        out["title"] = driver.title or ""
        caps = getattr(driver, "capabilities", None) or {}
        out["driver_version"] = str(
            (caps.get("chrome") or {}).get("chromedriverVersion", "")
        ).split(" ")[0]
        out["ok"] = True
    except Exception as e:                    # noqa: BLE001 — reported, not swallowed
        out["error"] = "{}: {}".format(e.__class__.__name__, e)
    finally:
        # Release the chromedriver process, never the browser.
        if driver is not None:
            try:
                driver.service.stop()
            except Exception:                 # noqa: BLE001 — best effort
                pass
    return out


def check_connection(settings: dict) -> dict:
    """
    Full connection check used by the Splash acknowledgment button and the
    Settings page. Returns a dict the UI renders directly:
      ok            — DevTools endpoint reachable
      browser       — Chrome version string (when reachable)
      tabs          — number of open tabs
      dc_tab_found  — a tab on the Digital Commons instance's domain is open
      selenium_ok   — selenium is importable (necessary, nowhere near enough)
      attach_ok     — a real Selenium session was opened against that Chrome
                      and released again; this is the check that matters
      attach_error  — the underlying exception text when it was not
      driver_version— the chromedriver Selenium actually used
      chromedriver  — a chromedriver found on PATH, which shadows Selenium
                      Manager and is the usual cause of attach_error
      advice        — list of troubleshooting strings (empty when all good)
    """
    host = settings["chrome"]["host"]
    port = int(settings["chrome"]["port"])
    base_url = settings.get("base_url", "").rstrip("/")
    result = {
        "ok": False, "browser": "", "tabs": 0,
        "dc_tab_found": False, "selenium_ok": False,
        "attach_ok": False, "attach_error": "", "driver_version": "",
        "chromedriver": {"present": False}, "advice": [],
    }

    # 1. Is anything listening on the debug port?
    try:
        version = probe_devtools(host, port)
        result["ok"] = True
        result["browser"] = version.get("Browser", "unknown")
    except (urllib.error.URLError, OSError, ValueError):
        result["advice"] = [
            "Nothing is answering on {}:{}. Chrome is probably not running "
            "in debug mode.".format(host, port),
            "Start Chrome with the exact command from the instructions above, "
            "including the --user-data-dir option. Recent versions of Chrome "
            "refuse remote debugging on your default profile, so a separate "
            "data directory is required. (You do NOT need to close your "
            "other Chrome windows — the debug instance runs alongside them.)",
            "If the command failed with a 'path not found' or 'not "
            "recognized' error, Chrome is installed in a non-standard "
            "location — replace only the quoted path at the start of the "
            "command. (Windows: right-click your Chrome shortcut, choose "
            "Properties, and copy the Target box. Mac: a per-user install "
            "lives at $HOME/Applications/Google Chrome.app.)",
            "Check that the port number in the command matches the port "
            "configured here ({}).".format(port),
            "If a firewall prompt appeared when Chrome started, allow the "
            "connection (local only).",
        ]
        return result

    # 2. Is a tab open on the Digital Commons instance? (login reminder)
    try:
        tabs = list_tabs(host, port)
        result["tabs"] = len(tabs)
        dc_host = urlparse(base_url).netloc.lower()
        if dc_host:
            result["dc_tab_found"] = any(
                urlparse(t.get("url", "")).netloc.lower() == dc_host
                for t in tabs
            )
        if not result["dc_tab_found"]:
            result["advice"].append(
                "Connected to Chrome, but no tab is open on {}. In the debug "
                "Chrome window, browse to your instance and log in as an "
                "administrator, then check again.".format(dc_host or base_url)
            )
    except (urllib.error.URLError, OSError, ValueError):
        result["advice"].append(
            "Connected to Chrome but could not list tabs — try again in a "
            "few seconds."
        )

    # 3. Can modules attach? (selenium present)
    try:
        import importlib.util
        result["selenium_ok"] = (
            importlib.util.find_spec("selenium") is not None
        )
    except Exception:
        result["selenium_ok"] = False
    if not result["selenium_ok"]:
        result["advice"].append(
            "Chrome is reachable, but the 'selenium' package is not "
            "installed, so modules will not be able to attach. Install it "
            "with:  pip install selenium"
        )
        return result

    # 4. Can a module attach IN FACT? Everything above is preliminary; this
    #    opens the same kind of session a module opens, and releases it.
    driver_info = dependencies.chromedriver_on_path()
    result["chromedriver"] = driver_info
    attach = try_attach(host, port)
    result["attach_ok"] = attach["ok"]
    result["attach_error"] = attach["error"]
    result["driver_version"] = attach["driver_version"]

    if not attach["ok"]:
        result["advice"].append(
            "Chrome is reachable, but opening a session failed, so modules "
            "will not be able to attach. The error was:  " + attach["error"]
        )
        result["advice"].extend(
            dependencies.chromedriver_advice(driver_info, result["browser"]))
        if not driver_info.get("present"):
            result["advice"].append(
                "No chromedriver is on your PATH, which is correct — "
                "Selenium fetches a matching one itself. If the error above "
                "mentions versions, let it re-fetch by clearing Selenium's "
                "cache (~/.cache/selenium on Mac and Linux, "
                "%USERPROFILE%\\.cache\\selenium on Windows)."
            )
    else:
        # Attaching works today, so a chromedriver on PATH is not breaking
        # anything yet — but it is the thing that will break next.
        result["advice"].extend(
            dependencies.chromedriver_advice(driver_info, result["browser"]))

    return result


# ---------------------------------------------------------------------------
# Platform-specific instructions shown in the UI (kept here so the UI and any
# future CLI share one source of truth).
# ---------------------------------------------------------------------------
def chrome_instructions(port: int) -> dict:
    win_cmd = (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port={p} '
        '--user-data-dir="%LOCALAPPDATA%\\DCAdminSuiteChrome"'
    ).format(p=port)
    mac_cmd = (
        '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
        "--remote-debugging-port={p} "
        '--user-data-dir="$HOME/DCAdminSuiteChrome"'
    ).format(p=port)
    return {
        "windows": {
            "steps": [
                "You do NOT need to close Chrome windows you already have "
                "open — the command below starts a separate debug instance "
                "with its own profile that runs alongside your normal Chrome.",
                "Press Windows key + R, type cmd, and press Enter.",
                "Paste the command below and press Enter. A new Chrome window "
                "opens using a separate profile just for this suite.",
                "If you see 'The system cannot find the path specified' or "
                "'not recognized', Chrome is installed somewhere other than "
                "the standard location — see the note below.",
                "In that Chrome window, go to your Digital Commons instance "
                "and log in as an administrator.",
            ],
            "command": win_cmd,
        },
        "mac": {
            "steps": [
                "You do NOT need to quit Chrome first — the command below "
                "starts a separate debug instance with its own profile that "
                "runs alongside your normal Chrome.",
                "Open Terminal (Applications → Utilities → Terminal).",
                "Paste the command below and press Return. A new Chrome "
                "window opens using a separate profile just for this suite.",
                "If Terminal says 'No such file or directory', Chrome is "
                "installed somewhere other than /Applications — see the note "
                "below.",
                "In that Chrome window, go to your Digital Commons instance "
                "and log in as an administrator.",
            ],
            "command": mac_cmd,
        },
        # Rendered with innerHTML in the shell UI (staff-approved wording,
        # 2026-07) — keep it trusted, server-side text only; no user input.
        "note": (
            "<strong>Note:</strong> <em>The provided Windows and Mac "
            "commands include the standard Chrome install path. If your "
            "install path differs, replace only the quoted path at the "
            "start of the command. Current versions of Chrome do not allow "
            "remote debugging on your default profile. The --user-data-dir "
            "profile is required for the debug instance to run alongside "
            "other instances of Chrome.</em>"
        ),
    }
