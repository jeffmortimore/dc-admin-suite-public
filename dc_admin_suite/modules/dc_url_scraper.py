#!/usr/bin/env python3
"""Structure URL Generator v1.2 — DC Admin Suite module.

Inventories the structures of a Digital Commons instance by type (ETD,
Community, Book, Journal, Event, Gallery, Series) and writes a single
styled Excel workbook — one worksheet per structure type — containing any
combination of Configuration, Homepage, and Regeneration URLs.

Adapted from the standalone tkinter "URLscraper.py" tool. Changes for the
suite architecture:
  * MANIFEST + --port/--session module contract; branded browser UI.
  * base_url comes from config/session.json — nothing is hard-coded.
  * Attaches to the user's logged-in debug Chrome; NEVER kills, starts,
    or logs into Chrome (the shell owns Chrome guidance, the user owns
    authentication).
  * Scrape runs in a background thread with the suite's phase state
    machine; the page polls /api/state.
  * pandas replaced by openpyxl (lighter; styled output with hyperlinks).
  * Login redirects are detected and surfaced in the UI instead of
    pausing on a blocking popup.

Run standalone:  python3 modules/dc_url_scraper.py --port 8799 \
                     --session config/session.json
Or launch it from the Main Menu.
v1.1 (2026-07) — hardening pass for public distribution:
  * Local HTTP endpoints now reject non-local requests (Host/Origin guard
    against DNS rebinding and cross-site POSTs).
  * Workbook header text color is computed from the branding primary
    (black or white, whichever meets contrast) instead of hard-coded white.

v1.3 (2026-09-04) — identity refactor Step 3: this module no longer
carries an institution. The standalone session fallback (used only when
config/session.json is missing) is now blank rather than one institution's
URL and name, and the branding color fallbacks read the neutral
FALLBACK_BRAND constant. Real values come from the shell, which seeds them
from config/profile.json — see profiles/README.md.
"""

# ---------------------------------------------------------------------------
# MANIFEST — read by the shell with `ast`; keep it a dict of literals.
# ---------------------------------------------------------------------------
MANIFEST = {
    "id": "url-scraper",
    "name": "Structure URL Generator",
    "description": "Export all structure homepage, configuration, and regeneration URLs to a spreadsheet.",
    "version": "1.3.3",
    "requires": ["selenium", "beautifulsoup4", "openpyxl"],
}

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Structure types: (label, system code as it appears in <th> on the
# site-wide "Contents of this site" listing).
# ---------------------------------------------------------------------------
STRUCTURE_TYPES = [
    ("ETD Structures", "ir_etd"),
    ("Community Structures", "ir_community"),
    ("Book Structures", "ir_book"),
    ("Journal Structures", "ir_journal"),
    ("Event Structures", "ir_event_community"),
    ("Gallery Structures", "ir_gallery"),
    ("Series Structures", "ir_series"),
]
CODE_TO_LABEL = {code: label for (label, code) in STRUCTURE_TYPES}

REPORT_TYPES = [
    ("config", "Configuration URL"),
    ("home", "Homepage URL"),
    ("regen", "Regeneration URL"),
]
REPORT_LABELS = dict(REPORT_TYPES)


# ---------------------------------------------------------------------------
# URL builders — everything derives from session.json's base_url.
# ---------------------------------------------------------------------------
def listing_url(base_url: str) -> str:
    return "{0}/cgi/user_config.cgi?context={0}&x_showall=1".format(base_url)


def config_url(base_url: str, ctx: str) -> str:
    return "{0}/cgi/user_config.cgi?context={1}".format(base_url, ctx)


def home_url(base_url: str, ctx: str) -> str:
    return "{0}/{1}/".format(base_url, unquote(ctx).strip("/"))


def regen_url(base_url: str, ctx: str) -> str:
    return "{0}/cgi/user_config.cgi?context={1}&x_regenerate=1".format(base_url, ctx)


URL_BUILDERS = {"config": config_url, "home": home_url, "regen": regen_url}


# ---------------------------------------------------------------------------
# Chrome attachment — attach only; never open, close, or log into Chrome.
# ---------------------------------------------------------------------------
def attach_chrome(session: dict):
    """Return a Selenium WebDriver attached to the user's debug Chrome."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_experimental_option(
        "debuggerAddress", session["chrome"]["debugger_address"])
    return webdriver.Chrome(options=opts)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------
def find_cttypes_table(html: str):
    """Return the <table class="cttypes"> captioned 'Contents of this site'."""
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tbl in soup.find_all("table", class_="cttypes"):
        cap = tbl.find("caption")
        if cap and "contents of this site" in cap.get_text(strip=True).lower():
            return tbl
    return None


def looks_like_login_page(html: str, current_url: str) -> bool:
    """Heuristic: did DC bounce us to a login/account page?"""
    if "myaccount.cgi" in current_url or "/login" in current_url:
        return True
    lowered = html.lower()
    return 'name="password"' in lowered and 'name="login"' in lowered


def extract_contexts(html: str, selected_codes):
    """Return {structure_code: sorted [context, ...]} from the listing page.

    Per DC conventions: contexts containing "/" are sub-documents and are
    skipped; a context equal to the full base URL is the platform itself
    and is skipped too (it starts with "http").
    """
    table = find_cttypes_table(html)
    if table is None:
        return None  # signals "table not found" (vs. found-but-empty)

    found = {code: set() for code in selected_codes}
    for tr in table.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        code = th.get_text(strip=True)
        if code not in found:
            continue
        for a in td.find_all("a", href=True):
            qs = parse_qs(urlparse(a["href"]).query)
            for ctx in qs.get("context", []):
                ctx = ctx.strip()
                if ctx and "/" not in ctx and not ctx.startswith("http"):
                    found[code].add(ctx)
    return {c: sorted(v) for c, v in found.items() if v}


# ---------------------------------------------------------------------------
# Excel output (openpyxl) — suite xlsx conventions: dark header row,
# alternating row fill, hyperlinked URL cells, frozen header, auto-filter.
# ---------------------------------------------------------------------------
COL_WIDTHS = {"ctx": 32, "config": 72, "home": 52, "regen": 76}


def write_workbook(path, contexts_by_code, reports, base_url, header_rgb):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill("solid", fgColor=header_rgb)
    # Black or white header text, whichever contrasts with the branding fill
    # (a light custom primary color would make white text unreadable).
    header_font = Font(name="Calibri", size=11, bold=True,
                       color=contrast_text("#" + header_rgb).lstrip("#").upper())
    link_font = Font(name="Calibri", size=11, color="0563C1", underline="single")
    alt_fill = PatternFill("solid", fgColor="FDFAF5")

    wb = Workbook()
    wb.remove(wb.active)

    for code, ctxs in contexts_by_code.items():
        label = CODE_TO_LABEL.get(code, code)
        ws = wb.create_sheet(title=re.sub(r"[\\/*?:\[\]]", "_", label)[:31])

        headers = ["Context ID"] + [REPORT_LABELS[r] for r in reports]
        ws.append(headers)
        for col, _ in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col)
            cell.fill, cell.font = header_fill, header_font
            cell.alignment = Alignment(vertical="center")

        for i, ctx in enumerate(ctxs):
            row = [ctx] + [URL_BUILDERS[r](base_url, ctx) for r in reports]
            ws.append(row)
            r_idx = i + 2
            for col in range(1, len(row) + 1):
                cell = ws.cell(row=r_idx, column=col)
                if i % 2 == 1:
                    cell.fill = alt_fill
                if col > 1:  # URL columns: live hyperlinks
                    cell.hyperlink = cell.value
                    cell.font = link_font

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = "A1:{}{}".format(
            get_column_letter(len(headers)), max(len(ctxs) + 1, 2))
        ws.column_dimensions["A"].width = COL_WIDTHS["ctx"]
        for col, r in enumerate(reports, start=2):
            ws.column_dimensions[get_column_letter(col)].width = COL_WIDTHS[r]

    wb.save(path)


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s.
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",   # idle | starting | scraping | writing | done | error
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "last_file": None,
    "summary": "",
}
LOCK = threading.Lock()
DRIVER = None          # one driver for the module's lifetime
DRIVER_LOCK = threading.Lock()


RUNNING_PHASES = ("starting", "scraping", "writing")

# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary.
RUN_LOG_NAME = None
CLEAR_RESETS = {"last_file": None}


# ---- DC-LOG 1 -------------------------------------------------------------
# The whole log of the current run, and Clear for a new run. Shared by
# every module with a log and duplicated verbatim, because modules are
# standalone: modules/_verify_pages.py fails if any copy differs.
#
# The page keeps the last 200 lines, so Copy log used to copy whatever the
# box held: the tail of this run, or the end of the previous one and the
# start of this. Jeff withheld a log on 2026-10-01 for exactly that reason
# (downloader v1.34.2; the suite since 2026-10-02). RUN_LOG starts empty at
# the module's run claim and holds every line since. Each module defines,
# beside this block:
#   RUN_LOG_NAME   the .txt saved into a run's folder ("..._{}.txt"), or
#                  None for a module that makes no run folder
#   CLEAR_RESETS   the module's own STATE keys that Clear puts back
# ---------------------------------------------------------------------------
RUN_LOG = []
RUN_LOG_MAX = 100000      # far beyond any measured run; said if reached
_RUN_LOG_DROPPED = [0]
CLEAR_REFUSED = ("A job is running. Stop it first — its log and summary "
                 "stay until it ends.")


def _run_log_reset():
    """Empty the whole-run log. The caller holds LOCK."""
    del RUN_LOG[:]
    _RUN_LOG_DROPPED[0] = 0


def begin_run_log():
    """A new run's log starts here, at the run claim, and nowhere else."""
    with LOCK:
        _run_log_reset()


def run_log_add(line):
    """Keep one line for the whole-run log. The caller holds LOCK."""
    RUN_LOG.append(line)
    if len(RUN_LOG) > RUN_LOG_MAX:
        over = len(RUN_LOG) - RUN_LOG_MAX
        del RUN_LOG[:over]
        _RUN_LOG_DROPPED[0] += over


def run_log_text():
    """Every line of the current run, as one string ("" when none)."""
    with LOCK:
        lines = list(RUN_LOG)
        dropped = _RUN_LOG_DROPPED[0]
    if dropped:
        # Never a silent truncation: a copy that has lost its head must
        # say so at the head.
        lines.insert(0, "[the first {} line(s) of this run were not kept — "
                        "the log holds {} lines]".format(dropped,
                                                         RUN_LOG_MAX))
    return "\n".join(lines) + ("\n" if lines else "")


def save_run_log(folder, stamp):
    """Write the current run's whole log into its run folder.

    Returns the path, or "" when the module makes no run folder, when this
    run has none (it ended before making one), or when the write failed —
    which is said in the log rather than raised, because this runs on
    every way out of a run, including the ones an error caused.
    """
    if not RUN_LOG_NAME or not folder or not os.path.isdir(folder):
        return ""
    path = os.path.join(folder, RUN_LOG_NAME.format(stamp))
    try:
        log("The whole log of this run is saved as " + os.path.basename(path))
        Path(path).write_text(run_log_text(), encoding="utf-8")
        return path
    except OSError as e:
        log("WARNING: could not save this run's log — {}: {}".format(
            e.__class__.__name__, e))
        return ""


def clear_for_new_run():
    """Put the page back to a clean start, keeping what the next run needs.

    The log, the status line, the progress, the last run's summary and its
    download links go; whatever is loaded and every form setting stay, so
    a clean start can never become a silent change of destination. Only
    called when no run is active; /api/clear refuses otherwise.
    """
    with LOCK:
        STATE["log"] = []
        STATE["progress"] = {"current": 0, "total": 0, "msg": ""}
        STATE["summary"] = ""
        STATE["phase"] = "idle"
        for key, value in CLEAR_RESETS.items():
            STATE[key] = json.loads(json.dumps(value))
        STATE["cleared"] = STATE.get("cleared", 0) + 1
        _run_log_reset()


def send_run_log(handler):
    """GET /api/log: the whole current run, however far the page scrolled."""
    data = run_log_text().encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
# ---- /DC-LOG --------------------------------------------------------------


# ---- DC-RUN 1 -------------------------------------------------------------
# One run at a time, claimed atomically (module contract 10). Shared by
# every module that starts its runs this way, duplicated verbatim, checked
# by modules/_verify_pages.py. Until 2026-10-02 eight modules asked "is a
# run active?", released the lock, and started a thread whose first line
# said so — two requests in that gap started two workers, the defect the
# downloader fixed in v1.24. The test and the claim are now one LOCK, the
# whole-run log begins at the claim, and the Stop and Pause events are
# cleared only by the request that won, so a refused one can never cancel
# a running job's Stop.
# ---------------------------------------------------------------------------
def start_run(target, args, events=()):
    """Claim the run slot and start target(*args); False if it is taken."""
    with LOCK:
        if STATE["phase"] in RUNNING_PHASES:
            return False
        STATE["phase"] = "starting"
        if "paused" in STATE:
            STATE["paused"] = False
        _run_log_reset()
    for event in events:
        event.clear()

    def runner():
        try:
            target(*args)
        finally:
            # A worker that returns or raises without recording how it
            # ended would leave the module "running" for good, refusing
            # every later run. Say so, and give the slot back.
            with LOCK:
                stuck = STATE["phase"] in RUNNING_PHASES
                if stuck:
                    STATE["phase"] = "error"
            if stuck:
                log("ERROR: the job ended without recording how it ended. "
                    "The lines above are everything it logged.")

    threading.Thread(target=runner, daemon=True).start()
    return True
# ---- /DC-RUN --------------------------------------------------------------


def log(msg: str):
    line = time.strftime("[%H:%M:%S] ") + msg
    with LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-200]  # cap retained lines
        run_log_add(line)
    # Console echo AFTER the state update, and encoding-safe: Windows
    # consoles often use a charmap codec (cp1252/cp437) that can't encode
    # characters like "→", and a print() crash must never kill a worker.
    try:
        print(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"))


def set_state(**kw):
    with LOCK:
        STATE.update(kw)


def set_progress(current, total, msg):
    with LOCK:
        STATE["progress"] = {"current": current, "total": total, "msg": msg}


def fail(msg: str):
    log("ERROR: " + msg)
    set_state(phase="error", summary=msg)


def get_driver(session):
    """Return a validated driver, attaching (or re-attaching) as needed."""
    global DRIVER
    with DRIVER_LOCK:
        if DRIVER is not None:
            try:
                _ = DRIVER.title
                return DRIVER
            except Exception:
                try:
                    DRIVER.quit()  # detaches only; user's Chrome stays open
                except Exception:
                    pass
                DRIVER = None
        DRIVER = attach_chrome(session)
        return DRIVER


# ---------------------------------------------------------------------------
# Scrape worker (background thread)
# ---------------------------------------------------------------------------
def scrape_worker(session, codes, reports, out_dir):
    base_url = session["base_url"].rstrip("/")
    try:
        set_state(phase="starting", summary="", last_file=None)
        set_progress(0, 0, "Attaching to Chrome…")
        log("Attaching to debug Chrome at {}…".format(
            session["chrome"]["debugger_address"]))
        try:
            driver = get_driver(session)
        except Exception as e:
            return fail(
                "Could not attach to Chrome ({}). Start Chrome in debug mode "
                "from the suite Splash/Settings page, log into Digital "
                "Commons, then try again.".format(e.__class__.__name__))
        log("Attached.")

        set_state(phase="scraping")
        url = listing_url(base_url)
        set_progress(0, 0, "Loading site-wide structure listing…")
        log("Loading " + url)
        driver.get(url)
        time.sleep(3)  # DC listing pages are slow to settle
        html = driver.page_source

        if looks_like_login_page(html, driver.current_url):
            return fail(
                "Digital Commons redirected to a login page. Log in inside "
                "the debug Chrome window, then click Scrape URLs again.")

        set_progress(0, 0, "Parsing structure table…")
        contexts = extract_contexts(html, codes)
        if contexts is None:
            return fail(
                "Couldn't find the 'Contents of this site' table. Confirm "
                "you're logged in as an administrator in the debug Chrome "
                "window and that the base URL in Settings is correct.")
        if not contexts:
            return fail("No structures found for the selected types.")

        total_ctx = sum(len(v) for v in contexts.values())
        log("Found {} structures across {} type(s): {}".format(
            total_ctx, len(contexts),
            ", ".join("{} {}".format(len(v), CODE_TO_LABEL.get(c, c))
                      for c, v in contexts.items())))

        set_state(phase="writing")
        set_progress(0, len(contexts), "Writing workbook…")
        primary = session.get("branding", {}).get("colors", {}) \
                         .get("primary", FALLBACK_BRAND["primary"]).lstrip("#")
        fname = "DC_URL_Inventory_{}.xlsx".format(time.strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(out_dir, fname)
        write_workbook(path, contexts, reports, base_url, primary)
        log("Wrote " + path)

        set_progress(len(contexts), len(contexts), "Done")
        set_state(phase="done", last_file=path,
                  summary="{} structures across {} sheet(s) → {}".format(
                      total_ctx, len(contexts), fname))
    except Exception as e:
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# UI — branded page per suite conventions (WCAG 2.1 AA).
# Tokens (__NAME__ etc.) are substituted at startup; no str.format, so the
# CSS/JS braces below stay untouched.
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ — __SUITE__</title>
<style>
 :root{--primary:__PRIMARY__;--accent:__ACCENT__;--onprimary:__ONPRIMARY__}
 *{box-sizing:border-box}
 body{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f7f6f3;color:#1b1b1f}
 header{background:var(--primary);color:var(--onprimary);padding:1rem 1.25rem}
 header h1{margin:0;font-size:1.2rem}
 header p{margin:.25rem 0 0;font-size:.9rem}
 header a{color:var(--onprimary)}
 .bar{height:5px;background:var(--accent)}
 main{max-width:46rem;margin:0 auto;padding:1.25rem}
 fieldset{border:1px solid #d7d4cc;border-radius:6px;margin:0 0 1rem;padding:.75rem 1rem;background:#fff}
 legend{font-weight:600;padding:0 .35rem}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:.25rem .75rem}
 label{display:flex;gap:.45rem;align-items:center;padding:.15rem 0}
 input[type=text]{width:100%;padding:.45rem .6rem;border:1px solid #a9a396;border-radius:5px;font:inherit}
 button{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
        padding:.6rem 1.3rem;font:inherit;font-weight:600;cursor:pointer}
 button:disabled{opacity:.55;cursor:not-allowed}
 :focus-visible{outline:3px solid #1a5dc8;outline-offset:2px}
 #status{margin:1rem 0;padding:.7rem .9rem;border-radius:6px;background:#eef1f5;border:1px solid #d7d4cc}
 #status.done{background:#e8f0e6;border-color:#4a6741}
 #status.error{background:#f7e8e2;border-color:#a33a12}
 #dl{display:inline-block;margin-top:.4rem;font-weight:600}
 #log{background:#1b1b1f;color:#d8d8de;border-radius:6px;padding:.7rem .9rem;
      font:13px/1.5 ui-monospace,monospace;max-height:14rem;overflow:auto;white-space:pre-wrap}
 /* Dismissible contextual messages. A contextual error stays on screen until
    the user clears it with the X in its corner: nothing auto-hides it, and
    the state poller only ever writes to #status, so a visible message is
    never wiped by the 1.5 s refresh. .msg-x is the shared close button,
    reused by the inline message spans further down the page. */
 #cerr{display:none;position:relative;margin:1rem 0;padding:.7rem 2.6rem .7rem .9rem;
       border-radius:6px;background:#f7e8e2;border:1px solid #a33a12;color:#7a2a0d;
       font-weight:600}
 #cerr.show{display:block}
 #cerr.notice{background:#eef1f5;border-color:#d7d4cc;color:#1b1b1f;font-weight:400}
 .msg-x{background:none;border:1px solid transparent;border-radius:4px;color:inherit;
        font:inherit;font-size:1.1rem;font-weight:700;line-height:1;cursor:pointer;
        padding:.1rem .4rem;margin-left:.4rem}
 .msg-x:hover{background:rgba(60,60,70,.12)}
 #cerr .msg-x{position:absolute;top:.35rem;right:.4rem;margin:0}
 @media (prefers-reduced-motion: no-preference){ #status{transition:background .3s} }
/* DC-LOG 1 styles */
.logbar{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.logbar h2{margin:0}
.logbar #logcopy{margin-left:auto}
.logbar #logcopy,.logbar #logclear{padding:.25rem .6rem;font-size:.85rem}
.lognote{font-size:.85rem;color:#54524c}
#log{user-select:text;-webkit-user-select:text}
.clearask{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.4rem 0;
  padding:.5rem .7rem;border-radius:6px;border:1px solid #9aa5ad;background:#eef1f5}
.logfull{width:100%;margin-top:.4rem;font:12px/1.4 ui-monospace,monospace}
/* /DC-LOG styles */
/* DC-PALETTE 2 — one notification palette for the shell and every module.
   Gray: instructions and neutral state. Green: something succeeded.
   Red: errors and warnings. The same block, byte for byte, in every page;
   modules/_verify_pages.py fails the build if any copy differs, and checks
   each ink against its background for WCAG 2.1 AA contrast. It sits last in
   each page's <style>, so it wins over the older per-page colors. */
:root{--dc-info-bg:#eef1f5;--dc-info-line:#9aa5ad;--dc-info-ink:#2c3440;
 --dc-ok-bg:#eaf5ec;--dc-ok-line:#1d6b34;--dc-ok-ink:#14522a;
 --dc-bad-bg:#fbecec;--dc-bad-line:#a3252c;--dc-bad-ink:#7c1c22}
#status,#status.waiting,#cerr.notice,.sum,.sum.warn,.status.info{
 background:var(--dc-info-bg);border-color:var(--dc-info-line);color:var(--dc-info-ink)}
#status.done,#cerr.ok,.sum.loaded,.status.good,.notice{
 background:var(--dc-ok-bg);border-color:var(--dc-ok-line);color:var(--dc-ok-ink)}
#status.error,#cerr,.sum.bad,.status.bad,.status.warn,.notice.err{
 background:var(--dc-bad-bg);border-color:var(--dc-bad-line);color:var(--dc-bad-ink)}
.dc-ok{color:var(--dc-ok-ink)}
.dc-bad{color:var(--dc-bad-ink)}
.dc-info{color:var(--dc-info-ink)}
/* Version 2: an element marked hidden stays hidden, whatever display its
   class gives it. v1.34.2's Clear panel had display:flex, which beat the
   attribute, so it showed all the time. */
[hidden]{display:none!important}
/* /DC-PALETTE */
</style></head><body>
<header><h1>__NAME__</h1>
<p>__INSTITUTION__ · <a href="__HUB__" target="dcAdminSuiteHub">back to suite</a> ·
   working on <strong>__BASE_URL__</strong></p></header>
<div class="bar" role="presentation"></div>
<main>

<div id="cerr" role="alert">
 <span id="cerr-msg"></span>
 <button type="button" class="msg-x" id="cerr-x"
         aria-label="Dismiss this message">&times;</button>
</div>

<form id="form">
 <fieldset><legend>Structure types</legend>
  <div class="grid" id="types">__TYPE_CHECKBOXES__</div>
 </fieldset>
 <fieldset><legend>URL report types (columns)</legend>
  <div class="grid">
   <label><input type="checkbox" name="report" value="config" checked> Configuration URLs</label>
   <label><input type="checkbox" name="report" value="home" checked> Homepage URLs</label>
   <label><input type="checkbox" name="report" value="regen"> Regeneration URLs</label>
  </div>
 </fieldset>
 <fieldset><legend>Output</legend>
  <label for="outdir" style="display:block;margin-bottom:.3rem">Output folder</label>
  <input type="text" id="outdir" name="outdir" value="~/Desktop"
         autocomplete="off" spellcheck="false">
 </fieldset>
 <button type="submit" id="go">Scrape URLs</button>
</form>
<div id="status" role="status" aria-live="polite">Idle — choose options and click Scrape URLs.</div>
<!-- DC-LOG 1: the log bar, the inline Clear question, the box, and the
     whole-run fallback for a browser that will not write the clipboard.
     Shared verbatim; see the script's DC-LOG block. -->
<div class="logbar">
 <h2 style="font-size:1rem">Log</h2>
 <span id="logheld" class="lognote" hidden></span>
 <span id="logmsg" class="lognote" role="status" aria-live="polite"></span>
 <button type="button" id="logcopy" class="secondary">Copy log</button>
 <button type="button" id="logclear" class="secondary" disabled>Clear for a new run</button>
</div>
<div id="clearask" class="clearask" hidden>
 <span>Clear the log, the status line and the last run's summary? The
 settings on this page, and anything already loaded into it, stay as
 they are.</span>
 <button type="button" id="clearyes">Clear</button>
 <button type="button" id="clearno" class="secondary">Keep</button>
</div>
<div id="log" tabindex="0" aria-label="Activity log"></div>
<textarea id="logfull" class="logfull" hidden readonly rows="8"
 aria-label="The whole log of the current run, selected for copying"></textarea>
<!-- /DC-LOG -->
</main>
<script>
/* ---- Activity log rendering ---------------------------------------------
   Rewriting the whole log on every poll destroyed the reader's selection
   and snapped the scroll back to the tail. So: render only on change,
   append rather than replace, follow the tail only from the tail, and do
   not touch the DOM at all while a selection is live inside the box.
   Reported by Jeff on 2026-09-08 while trying to copy a log line.
------------------------------------------------------------------------ */
var logShown = null;
var LOG_TAIL_SLOP = 24;      /* px from the bottom that still counts as "at the tail" */

function logEl(){ return document.getElementById('log'); }

function logSelectionLive(){
  const box = logEl(), sel = window.getSelection();
  if(!box || !sel || sel.isCollapsed || !sel.rangeCount) return false;
  return box.contains(sel.getRangeAt(0).commonAncestorContainer);
}

function logAtTail(){
  const box = logEl();
  if(!box) return true;
  return box.scrollHeight - box.scrollTop - box.clientHeight <= LOG_TAIL_SLOP;
}

function logHeldNote(){
  const note = document.getElementById('logheld');
  if(!note) return;
  const why = logSelectionLive() ? 'paused — text selected'
            : (!logAtTail() ? 'paused — scrolled up' : '');
  note.textContent = why;
  note.hidden = !why;
}

function renderLog(lines){
  const box = logEl();
  if(!box) return;
  const txt = (lines || []).join('\\n');
  if(txt === logShown){ logHeldNote(); return; }
  /* Never interrupt a copy in progress. The next poll picks it up. */
  if(logSelectionLive()){ logHeldNote(); return; }
  const tail = logAtTail(), keep = box.scrollTop;
  if(logShown !== null && txt.indexOf(logShown) === 0){
    box.appendChild(document.createTextNode(txt.slice(logShown.length)));
  }else{
    box.textContent = txt;      /* first paint, or the server trimmed the head */
  }
  logShown = txt;
  box.scrollTop = tail ? box.scrollHeight : keep;
  logHeldNote();
}

function logSay(msg){
  const el = document.getElementById('logmsg');
  if(!el) return;
  el.textContent = msg;
  setTimeout(function(){ if(el.textContent === msg) el.textContent = ''; }, 4000);
}

function logSelectAll(){
  const box = logEl();
  if(!box) return;
  const r = document.createRange();
  r.selectNodeContents(box);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  box.focus();
}

/* DC-LOG 1: Copy log and Clear for a new run. Shared by every module with
   a log and duplicated verbatim; modules/_verify_pages.py fails if any
   copy differs. There is no backslash anywhere in it, because some pages
   are raw strings and some are not, and the same bytes must serve both.
   Copy log copies the WHOLE current run from the module (/api/log), not
   whatever the box holds: the box keeps the last 200 lines and may still
   show the end of the previous run. Every page's poll() calls
   logStateSeen(s) with the state it just read. */
var LOG_NL = String.fromCharCode(10);
var logLastCleared = null;
function logLines(txt){
  if(!txt) return 0;
  var t = txt.charAt(txt.length - 1) === LOG_NL ? txt.slice(0, -1) : txt;
  return t.split(LOG_NL).length;
}
function logOffer(txt){
  var ta = document.getElementById('logfull');
  if(!ta){ logSelectAll(); return; }
  ta.value = txt; ta.hidden = false; ta.focus(); ta.select();
  var n = logLines(txt);
  logSay('The whole current run (' + n + ' line' + (n === 1 ? '' : 's') +
         ') is selected below. Press Ctrl+C (Cmd+C) to copy.');
}
function copyLog(){
  fetch('/api/log', {cache:'no-store'})
    .then(function(r){ return r.ok ? r.text() : Promise.reject(r.status); })
    .then(function(txt){
      if(!txt){ logSay('Nothing has been logged since the last clean start.'); return; }
      var n = logLines(txt);
      var said = 'The whole current run, ' + n + ' line' +
                 (n === 1 ? '' : 's') + ', copied to the clipboard.';
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(
          function(){ logSay(said); }, function(){ logOffer(txt); });
      }else{
        logOffer(txt);
      }
    }, function(){
      logSay('Could not read the log from the module. Is it still running?');
    });
}
/* A run is anything but idle, done or error: the same rule as every
   module's RUNNING_PHASES, which _verify_pages.py checks. */
function logRunning(s){
  return !!s && ['idle', 'done', 'error'].indexOf(s.phase) < 0;
}
function logStateSeen(s){
  var running = logRunning(s),
      btn = document.getElementById('logclear'),
      ask = document.getElementById('clearask');
  if(btn) btn.disabled = running;
  if(running && ask) ask.hidden = true;
  /* Another tab (or this one) cleared the module: wipe this page's own
     view of it too, the callout and the copy box, not only the server's. */
  var seen = (s && s.cleared) || 0;   /* absent until the first Clear */
  if(s && seen !== logLastCleared){
    if(logLastCleared !== null){
      var full = document.getElementById('logfull');
      if(full){ full.value = ''; full.hidden = true; }
      var box = document.getElementById('cerr');
      if(box && !box.classList.contains('ok') && typeof clearErr === 'function') clearErr();
    }
    logLastCleared = seen;
  }
}
(function(){
  var btn = document.getElementById('logclear'),
      ask = document.getElementById('clearask'),
      yes = document.getElementById('clearyes'),
      no = document.getElementById('clearno');
  if(!btn || !ask || !yes || !no) return;
  btn.addEventListener('click', function(){
    if(btn.disabled) return;
    ask.hidden = false; yes.focus();
  });
  no.addEventListener('click', function(){ ask.hidden = true; btn.focus(); });
  yes.addEventListener('click', function(){
    fetch('/api/clear', {method:'POST', cache:'no-store',
                         headers:{'Content-Type':'application/json'}, body:'{}'})
      .then(function(r){
        return r.json().then(function(j){ return {ok:r.ok, j:j}; },
                             function(){ return {ok:r.ok, j:{}}; });
      })
      .then(function(res){
        ask.hidden = true;
        if(!res.ok){ showErr(res.j.error || 'Could not clear.'); return; }
        var full = document.getElementById('logfull');
        if(full){ full.value = ''; full.hidden = true; }
        showOk('Cleared for a new run. The settings on this page, and ' +
               'anything already loaded into it, are as they were.');
        if(typeof poll === 'function') poll();
      }, function(){
        ask.hidden = true;
        showErr('Could not reach the module to clear it. Is it still running?');
      });
  });
})();
/* /DC-LOG */

(function(){
  const box = logEl(), btn = document.getElementById('logcopy');
  if(btn) btn.addEventListener('click', copyLog);
  /* The hold note has to react to scrolling too, not only to new lines:
     a reader who scrolls up while the job is quiet would otherwise get no
     indication that the log has stopped following. */
  if(box){
    box.addEventListener('scroll', logHeldNote);
    document.addEventListener('selectionchange', logHeldNote);
  }
})();

const form=document.getElementById('form'), go=document.getElementById('go'),
      status=document.getElementById('status'), logBox=document.getElementById('log');
let polling=null;

/* ---- dismissible contextual messages -----------------------------------
   showErr() paints the callout above the status line and leaves it there;
   only the X (or the next action that clears it) takes it down. Never put a
   contextual error in #status: the poller rewrites that element every 1.5 s
   and the message would vanish before it could be read. showNote() is the
   same callout in a neutral tone, for non-error feedback that the user also
   needs time to read. */
const cerrBox=document.getElementById('cerr'),
      cerrMsg=document.getElementById('cerr-msg'),
      cerrX=document.getElementById('cerr-x');
/* Three tones, from the suite's one palette (DC-PALETTE): an error or
   warning is red, a success is green, an instruction is gray. */
function showMsg(msg,isError,isOk){
  cerrMsg.textContent=msg;
  cerrBox.classList.toggle('notice',!isError&&!isOk);
  cerrBox.classList.toggle('ok',!isError&&!!isOk);
  cerrBox.classList.add('show');
  cerrBox.scrollIntoView({block:'nearest'});
}
function showErr(msg){ showMsg(msg,true); }
function showNote(msg){ showMsg(msg,false); }
function showOk(msg){ showMsg(msg,false,true); }
function clearErr(){ cerrBox.classList.remove('show'); cerrMsg.textContent=''; }
cerrX.addEventListener('click',clearErr);
/* Inline message spans (registry / template / endpoint rows). Errors get a
   close button and stay; only confirmations fade on the timer. */
const _msgTimers=new WeakMap();
function setMsg(el,text,isError,isOk){
  const t=_msgTimers.get(el); if(t){clearTimeout(t); _msgTimers.delete(el);}
  el.textContent='';
  if(!text) return;
  const span=document.createElement('span'); span.textContent=text;
  span.className=isError?'dc-bad':(isOk?'dc-ok':'dc-info');
  el.appendChild(span);
  if(isError){
    const x=document.createElement('button');
    x.type='button'; x.className='msg-x'; x.innerHTML='&times;';
    x.setAttribute('aria-label','Dismiss this message');
    x.addEventListener('click',()=>{el.textContent='';});
    el.appendChild(x);
  }else{
    _msgTimers.set(el,setTimeout(()=>{el.textContent='';},4000));
  }
}
form.addEventListener('submit', async (e)=>{
  e.preventDefault();
  clearErr();   // a fixed problem takes its message down
  const types=[...form.querySelectorAll('input[name=type]:checked')].map(i=>i.value);
  const reports=[...form.querySelectorAll('input[name=report]:checked')].map(i=>i.value);
  const outdir=form.outdir.value.trim();
  if(!types.length){showErr('Select at least one structure type.');return;}
  if(!reports.length){showErr('Select at least one URL report type.');return;}
  if(!outdir){showErr('Enter an output folder.');return;}
  go.disabled=true;
  const r=await fetch('/api/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({types,reports,out_dir:outdir})});
  const j=await r.json();
  if(!r.ok){showErr(j.error||'Could not start.');go.disabled=false;return;}
  if(!polling) polling=setInterval(poll,1500);
  poll();
});

async function poll(){
  const s=await (await fetch('/api/state')).json();
  renderLog(s.log); logStateSeen(s);
  const p=s.progress&&s.progress.msg?' — '+s.progress.msg:'';
  if(s.phase==='done'){
    status.className='done';
    status.innerHTML='Done: '+esc(s.summary)+
      '<br><a id="dl" href="/download">Download workbook</a>';
    go.disabled=false;
  }else if(s.phase==='error'){
    status.className='error'; status.textContent='Error: '+s.summary; go.disabled=false;
  }else if(s.phase==='idle'){
    status.className=''; status.textContent='Idle — choose options and click Scrape URLs.';
  }else{
    status.className=''; status.textContent=cap(s.phase)+p; go.disabled=true;
  }
}
const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
const cap=t=>t.charAt(0).toUpperCase()+t.slice(1)+'…';
poll(); polling=setInterval(poll,1500);
</script>
</body></html>"""


def contrast_text(hex_color: str) -> str:
    """White or black header text, whichever meets contrast on hex_color."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if lum > 140 else "#ffffff"


def build_page(session: dict) -> bytes:
    brand = session.get("branding", {})
    colors = brand.get("colors", {})
    primary = colors.get("primary", FALLBACK_BRAND["primary"])
    boxes = "\n".join(
        '<label><input type="checkbox" name="type" value="{}" checked> {}</label>'
        .format(code, label) for label, code in STRUCTURE_TYPES)
    page = PAGE
    for token, value in {
        "__NAME__": MANIFEST["name"],
        "__SUITE__": brand.get("suite_name", "DC Admin Suite"),
        "__INSTITUTION__": brand.get("institution", ""),
        "__PRIMARY__": primary,
        "__ACCENT__": colors.get("accent", FALLBACK_BRAND["accent"]),
        "__ONPRIMARY__": contrast_text(primary),
        "__HUB__": session.get("hub_url", "#"),
        "__BASE_URL__": session.get("base_url", ""),
        "__TYPE_CHECKBOXES__": boxes,
    }.items():
        page = page.replace(token, value)
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
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
        if origin.lower() == "null":     # sandboxed/file: pages - reject
            return False
        if (urlparse(origin).hostname or "").lower() not in _LOCAL_HOSTS:
            return False
    return True


def make_handler(session: dict, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not request_allowed(self):
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/log":
                return send_run_log(self)
            if self.path == "/api/state":
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
                if not path or not os.path.isfile(path):
                    return self._json({"error": "No file available."}, 404)
                data = Path(path).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/vnd.openxmlformats-officedocument"
                                 ".spreadsheetml.sheet")
                self.send_header("Content-Disposition",
                                 'attachment; filename="{}"'
                                 .format(os.path.basename(path)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            # default: the app page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if not request_allowed(self):
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/clear":
                with LOCK:
                    busy = STATE["phase"] in RUNNING_PHASES
                if busy:
                    return self._json({"error": CLEAR_REFUSED}, 409)
                clear_for_new_run()
                return self._json({"ok": True})
            if self.path != "/api/start":
                return self._json({"error": "Not found."}, 404)
            with LOCK:
                if STATE["phase"] in ("starting", "scraping", "writing"):
                    return self._json({"error": "A scrape is already running."}, 409)
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                codes = [c for c in req["types"] if c in CODE_TO_LABEL]
                reports = [r for r in req["reports"] if r in REPORT_LABELS]
                out_dir = os.path.expanduser(str(req["out_dir"]).strip())
            except (KeyError, ValueError, json.JSONDecodeError):
                return self._json({"error": "Bad request."}, 400)
            if not codes or not reports:
                return self._json({"error": "Nothing selected."}, 400)
            if not os.path.isdir(out_dir):
                return self._json(
                    {"error": "Output folder does not exist: " + out_dir}, 400)
            if not start_run(scrape_worker, (session, codes, reports, out_dir)):
                return self._json({"error": "A scrape is already running."}, 409)
            return self._json({"ok": True})

        def log_message(self, *a):
            pass

    return Handler


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
    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 make_handler(session, build_page(session)))
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
