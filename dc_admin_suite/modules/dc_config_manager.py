#!/usr/bin/env python3
"""Configuration Manager v1.2 — DC Admin Suite module.

Reads and writes Digital Commons structure configuration settings in bulk.

SCRAPE — pick structures (load a DC_URL_Inventory_*.xlsx written by the URL
Scraper, or harvest the site-wide listing fresh), then visit each structure's
configuration page and record every form field. Output is one styled
workbook per run — DC_Config_Report_<timestamp>.xlsx — with one sheet per
structure type: Context ID | Config URL | one column per form field.

UPDATE — load a config report (usually a scraped one you've edited), then:
  1. DRY RUN (required first): every structure's live config page is
     re-read and diffed against the sheet values. Nothing is changed.
     The proposed changes are written to DC_Config_DryRun_<timestamp>.xlsx
     and summarized in the UI.
  2. APPLY: with the dry-run plan on screen, a second confirmed start
     waits for each page's scripts to finish (as scrape and dry run do),
     sets exactly those changes in each config form (never typed: a
     typed tab or line break is a keypress), reads them back, presses
     Save Changes, reads the page again and records what it holds, and
     (optional toggle, default ON) regenerates a structure saved as
     planned. Every attempt is recorded in
     DC_Config_UpdateLog_<timestamp>.xlsx.

Stop or a mid-run login redirect still writes a partial workbook
(_PARTIAL filename suffix). Pause/Resume/Stop + ETA per the suite patterns.

Adapted from the standalone tkinter "ConfigManager.py" tool.
Changes for the suite architecture:
  * MANIFEST + --port/--session module contract; branded browser UI.
  * base_url comes from config/session.json — nothing is hard-coded.
  * Attaches to the user's logged-in debug Chrome; NEVER kills, starts,
    or logs into Chrome (no psutil, no subprocess, no tempfile profile).
  * pandas replaced by openpyxl (suite xlsx conventions).
  * Old per-input-file *_structured.xlsx outputs replaced by one workbook
    per run with one sheet per structure type; the same workbook is the
    input format for update mode, so scrape → edit → update round-trips.
  * NEW: update mode requires a dry-run diff first — the old tool wrote
    blindly; this module previews exactly what will change.
  * The old tool's radio-group scrape bug (a later unchecked radio
    clobbered the checked value) is fixed in extract_fields().
  * Background worker + phase state machine; the page polls /api/state.

Run standalone:  python3 modules/dc_config_manager.py --port 8799 \
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
    "id": "config-manager",
    "name": "Configuration Manager",
    "description": "Scrape, preview, and update most structure configurations using a spreadsheet.",
    "version": "1.5.8",
    "requires": ["selenium", "beautifulsoup4", "openpyxl"],
}

import argparse
import glob
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
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------
DEFAULT_SESSION = Path(__file__).resolve().parent.parent / "config" / "session.json"

INVENTORY_GLOB = "DC_URL_Inventory_*.xlsx"   # written by the Structure URL Generator
REPORT_GLOB = "DC_Config_Report_*.xlsx"      # written by this module (scrape)


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
# Structure types (system code on the site-wide listing → friendly label).
# ---------------------------------------------------------------------------
STRUCTURE_TYPES = [
    ("ETD", "ir_etd"),
    ("Community", "ir_community"),
    ("Book", "ir_book"),
    ("Journal", "ir_journal"),
    ("Event", "ir_event_community"),
    ("Gallery", "ir_gallery"),
    ("Series", "ir_series"),
]
CODE_TO_LABEL = {code: label for (label, code) in STRUCTURE_TYPES}


# ---------------------------------------------------------------------------
# URL builders — everything derives from session.json's base_url.
# ---------------------------------------------------------------------------
def listing_url(base_url: str) -> str:
    return "{0}/cgi/user_config.cgi?context={0}&x_showall=1".format(base_url)


def config_url(base_url: str, ctx: str) -> str:
    return "{0}/cgi/user_config.cgi?context={1}".format(base_url, ctx)


def regen_url(base_url: str, ctx: str) -> str:
    return "{0}/cgi/user_config.cgi?context={1}&x_regenerate=1".format(
        base_url, ctx)


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
# HTML parsing (bs4). Helpers shared with the other modules are duplicated
# by design — modules stay self-contained.
# ---------------------------------------------------------------------------
def _soup(html: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


# ---- DC-INDEX 1 ------------------------------------------------------------
# The site-wide structure listing (user_config.cgi?...&x_showall=1) is one
# <table class="cttypes"> in both layouts Digital Commons has served: one
# captioned "Contents of this site" whose row headers are structure codes
# (ir_series, ...), and one captioned "Site index" whose row headers are
# plural headings (Series, ...). Both are read. A row whose header is
# neither is reported by index_unknown_headers(), never guessed at, and a
# page without the table is described by what it showed.
INDEX_CAPTIONS = ("site index", "contents of this site")
INDEX_HEADINGS = {
    "communities": "ir_community",
    "journals": "ir_journal",
    "etds": "ir_etd",
    "galleries": "ir_gallery",
    "series": "ir_series",
    "books": "ir_book",
    "conferences": "ir_event_community",
}


def _index_soup(html):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def find_cttypes_table(html):
    """Return the site-wide listing table (either caption), or None."""
    for tbl in _index_soup(html).find_all("table", class_="cttypes"):
        cap = tbl.find("caption")
        text = " ".join(cap.get_text(" ", strip=True).split()).lower() \
            if cap else ""
        if any(c in text for c in INDEX_CAPTIONS):
            return tbl
    return None


def index_row_code(header):
    """The structure code a row header names: the code itself (the older
    layout) or a plural heading (the newer). None when it is neither."""
    h = " ".join(header.split())
    if h.startswith("ir_"):
        return h
    return INDEX_HEADINGS.get(h.lower())


def index_rows(table):
    """[(code or None, header, [context, ...])] for each row of the table.
    Sub-documents (a context with "/") and the platform itself (a context
    starting "http") are skipped, per Digital Commons conventions."""
    rows = []
    for tr in table.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        header = " ".join(th.get_text(" ", strip=True).split())
        ctxs = []
        for a in td.find_all("a", href=True):
            qs = parse_qs(urlparse(a["href"]).query)
            for ctx in qs.get("context", []):
                ctx = ctx.strip()
                if (ctx and "/" not in ctx and not ctx.startswith("http")
                        and ctx not in ctxs):
                    ctxs.append(ctx)
        rows.append((index_row_code(header), header, ctxs))
    return rows


def index_unknown_headers(html):
    """[(header, count)] for rows with structures whose header names no
    known structure type; [] when there are none or no table at all."""
    table = find_cttypes_table(html)
    if table is None:
        return []
    return [(h, len(c)) for code, h, c in index_rows(table)
            if code is None and c]


def index_unknown_note(header, count):
    return ('A row of the site index is headed "{}", which is not a '
            "structure type this module knows, so its {} structure(s) "
            "cannot be chosen by type.".format(header, count))


def index_missing_message(html):
    """What to say when the listing table is not on the page: what the
    page showed, then the possible causes, none of them asserted."""
    soup = _index_soup(html)
    title = " ".join(soup.title.get_text(" ", strip=True).split()) \
        if soup.title else ""
    caps = [" ".join(c.get_text(" ", strip=True).split())[:60]
            for c in soup.find_all("caption")]
    seen = ('The page served was titled "{}"'.format(title) if title
            else "The page served had no title")
    seen += ("; its table captions were " + ", ".join(
        '"{}"'.format(c) for c in caps) if caps
        else "; it had no captioned table")
    return ("Couldn't find the structure listing (the site index) on the "
            "listing page. {}. Check that you are logged in as an "
            "administrator in the debug Chrome window and that the base URL "
            "in Settings is correct; if both are, Digital Commons may have "
            "changed this page.".format(seen))
# ---- /DC-INDEX ---------------------------------------------------------------


def looks_like_login_page(html: str, current_url: str) -> bool:
    """Heuristic: did DC bounce us to a login/account page?"""
    if "myaccount.cgi" in current_url or "/login" in current_url:
        return True
    lowered = html.lower()
    return 'name="password"' in lowered and 'name="login"' in lowered


def extract_contexts(html: str):
    """Return {type_label: sorted [context, ...]} from the listing page, or
    None if the site index table (DC-INDEX) is missing (login/permissions).
    Sub-documents (contain "/") and the platform itself (starts with "http")
    are skipped per DC conventions."""
    table = find_cttypes_table(html)
    if table is None:
        return None

    found = {}
    for code, header, ctxs in index_rows(table):
        label = CODE_TO_LABEL.get(code, header)
        if ctxs:
            found.setdefault(label, set()).update(ctxs)
    return {label: sorted(v) for label, v in found.items() if v}


def extract_fields(html: str):
    """Extract configuration form fields from a DC config page.

    Returns (fields_dict, scoped) where `scoped` is True when the parse was
    limited to the element with id="user-config" (the config table DC renders
    around the settings form). When that element is missing the whole page is
    parsed instead — noisier, but never empty-handed.

    Field semantics (round-trip compatible with apply_worker):
      text/textarea → the current value ("" if blank)
      checkbox      → the input's value attr (or "on") when checked, else ""
      radio         → the CHECKED input's value attr, else "" (unlike the old
                      tool, a later unchecked radio never clobbers this)
      select        → the selected option's visible text (first option if
                      nothing is explicitly selected)
      select multiple → its items joined by "; " (multi_value; never the
                      first option)
    """
    soup = _soup(html)
    scope = soup.find(id="user-config")
    scoped = scope is not None
    if scope is None:
        scope = soup

    fields = {}

    for tag in scope.find_all("input"):
        itype = (tag.get("type") or "text").lower()
        if itype in ("hidden", "submit", "button", "image", "reset"):
            continue
        name = tag.get("name")
        if not name:
            continue
        if itype in ("checkbox", "radio"):
            if tag.has_attr("checked"):
                fields[name] = (tag.get("value") or "on").strip()
            else:
                fields.setdefault(name, "")
        else:
            fields[name] = (tag.get("value") or "").strip()

    for tag in scope.find_all("textarea"):
        name = tag.get("name")
        if name:
            fields[name] = tag.get_text().strip()

    for tag in scope.find_all("select"):
        name = tag.get("name")
        if not name:
            continue
        if tag.has_attr("multiple"):
            fields[name] = multi_value(tag)
            continue
        selected = tag.find("option", selected=True)
        if selected is None:
            selected = tag.find("option")
        fields[name] = selected.get_text(strip=True) if selected else ""

    return fields, scoped


def multi_value(tag):
    """A multiple-choice list as the items it holds, joined by "; ". The
    disciplines widget lists a structure's items in its "Selected" list
    (class selectedMultiSelectOptions), read as every item listed there,
    beside one empty-valued placeholder option (with text until its script
    has run, emptied after) that is never an item; any
    other multiple list is read as its selected options. Never the first
    option, as a single select is: a multiple list with nothing chosen
    submits nothing."""
    if "selectedMultiSelectOptions" in (tag.get("class") or []):
        opts = [o for o in tag.find_all("option")
                if (o.get("value") or "") != ""]
    else:
        opts = [o for o in tag.find_all("option") if o.has_attr("selected")]
    return "; ".join(o.get_text(strip=True) for o in opts)


def multi_fields(html):
    """Names of the multiple-choice lists on a config page."""
    soup = _soup(html)
    scope = soup.find(id="user-config") or soup
    return {t.get("name") for t in scope.find_all("select")
            if t.get("name") and t.has_attr("multiple")}


def item_count(value):
    """How many items a multiple-choice value holds."""
    return len([x for x in (value or "").split(";") if x.strip()])


def cell_str(value) -> str:
    """Normalize a worksheet cell to the string a form field would hold.
    openpyxl hands back int/float for numeric-looking cells; 5.0 → "5" so
    diffs against live form values don't produce false positives."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# ---------------------------------------------------------------------------
# Target model — which structures to work on, whatever the source.
#
#   model = {
#     "types":  {type_label: [ctx, ...]},        sorted contexts per type
#     "rows":   {type_label: {ctx: {field: str}}} sheet values (update only;
#                                                 {} for scrape targets)
#     "fields": {type_label: [field, ...]}        sheet field columns
#                                                 (update only)
#     "source": human-readable description shown in the UI,
#   }
# SCRAPE_MODEL comes from an inventory workbook or a fresh harvest.
# UPD_MODEL comes from a config report workbook; PLAN is built from it by a
# dry run and consumed by apply.
# ---------------------------------------------------------------------------
def parse_inventory(path: str):
    """Return {type_label: [ctx, ...]} from a Structure URL Generator inventory workbook.
    Raises ValueError with a user-facing message on any problem."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise ValueError("Couldn't open the workbook ({})."
                         .format(e.__class__.__name__))
    types = {}
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if (not header
                    or not str(header[0] or "").lower().startswith("context")):
                continue
            ctxs = []
            for raw in rows_iter:
                ctx = cell_str(raw[0] if raw else None)
                if ctx and "/" not in ctx and not ctx.startswith("http"):
                    ctxs.append(ctx)
            if ctxs:
                types[ws.title] = sorted(set(ctxs))
    finally:
        wb.close()

    if not types:
        raise ValueError(
            "That workbook doesn't look like a Structure URL Generator inventory "
            "(no sheet has a 'Context ID' first column with structures).")
    return types


def parse_config_report(path: str):
    """Return (types, rows, fields) from a config report workbook:
    one sheet per type, headers Context ID | Config URL | field columns.
    Raises ValueError with a user-facing message on any problem."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise ValueError("Couldn't open the workbook ({})."
                         .format(e.__class__.__name__))
    types, rows, fields = {}, {}, {}
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            header = next(rows_iter, None)
            if (not header or len(header) < 3
                    or not str(header[0] or "").lower().startswith("context")
                    or not str(header[1] or "").lower().startswith("config")):
                continue
            cols = [str(h).strip() for h in header[2:] if h is not None]
            sheet_rows = {}
            for raw in rows_iter:
                ctx = cell_str(raw[0] if raw else None)
                if not ctx:
                    continue
                values = {}
                for i, col in enumerate(cols, start=2):
                    values[col] = cell_str(raw[i]) if i < len(raw) else ""
                sheet_rows[ctx] = values
            if sheet_rows:
                types[ws.title] = sorted(sheet_rows)
                rows[ws.title] = sheet_rows
                fields[ws.title] = cols
    finally:
        wb.close()

    if not types:
        raise ValueError(
            "That workbook doesn't look like a config report (expected "
            "sheets with headers: Context ID | Config URL | field columns).")
    return types, rows, fields


def scan_workbooks(folder: str, pattern: str):
    """List matching workbooks in `folder`, newest first (max 25)."""
    hits = []
    for p in glob.glob(os.path.join(folder, pattern)):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        hits.append({"name": os.path.basename(p), "path": p,
                     "modified": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(mtime)),
                     "_m": mtime})
    hits.sort(key=lambda h: h["_m"], reverse=True)
    for h in hits:
        del h["_m"]
    return hits[:25]


# ---------------------------------------------------------------------------
# Excel output (openpyxl) — suite conventions: dark header row, alternating
# row fill, frozen header, auto-filter, hyperlinked URL cells.
# ---------------------------------------------------------------------------
def _styles(header_rgb):
    from openpyxl.styles import Alignment, Font, PatternFill
    return {
        "header_fill": PatternFill("solid", fgColor=header_rgb),
        # Black or white header text, whichever contrasts with the fill.
        "header_font": Font(name="Calibri", size=11, bold=True,
                            color=contrast_text("#" + header_rgb)
                            .lstrip("#").upper()),
        "alt_fill": PatternFill("solid", fgColor="FDFAF5"),
        "link_font": Font(name="Calibri", size=11, underline="single",
                          color="0563C1"),
        "valign": Alignment(vertical="center"),
    }


def _sheet_title(label: str) -> str:
    return re.sub(r"[\\/*?:\[\]]", "_", label)[:31]


def _style_header(ws, ncols, st):
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill, cell.font = st["header_fill"], st["header_font"]
        cell.alignment = st["valign"]


def _finish_sheet(ws, ncols, nrows, widths):
    from openpyxl.utils import get_column_letter
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:{}{}".format(
        get_column_letter(ncols), max(nrows + 1, 2))
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def write_config_workbook(path, records_by_type, base_url, header_rgb):
    """Config report: one sheet per type.
    records_by_type = {type_label: [(ctx, {field: value}), ...]}
    Field columns are the union across the sheet's records, in first-seen
    (form document) order, so the layout mirrors the config page."""
    from openpyxl import Workbook

    st = _styles(header_rgb)
    wb = Workbook()
    wb.remove(wb.active)

    for label, records in records_by_type.items():
        cols = []
        for _, fields in records:
            for name in fields:
                if name not in cols:
                    cols.append(name)

        ws = wb.create_sheet(title=_sheet_title(label))
        headers = ["Context ID", "Config URL"] + cols
        ws.append(headers)
        _style_header(ws, len(headers), st)

        for i, (ctx, fields) in enumerate(records):
            url = config_url(base_url, ctx)
            ws.append([ctx, url] + [fields.get(c, "") for c in cols])
            r = i + 2
            link_cell = ws.cell(row=r, column=2)
            link_cell.hyperlink = url
            link_cell.font = st["link_font"]
            if i % 2 == 1:
                for col in range(1, len(headers) + 1):
                    ws.cell(row=r, column=col).fill = st["alt_fill"]

        _finish_sheet(ws, len(headers), len(records),
                      [32, 72] + [28] * len(cols))

    wb.save(path)


def write_dryrun_workbook(path, changes, header_rgb):
    """Dry-run plan: one row per proposed field change, in visit order.
    changes = [(order, ctx, type_label, field, old, new, url), ...]"""
    from openpyxl import Workbook

    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Dry Run"

    headers = ["Order", "Context ID", "Type", "Field",
               "Current Value", "New Value", "Config URL"]
    ws.append(headers)
    _style_header(ws, len(headers), st)

    for i, (order, ctx, label, field, old, new, url) in enumerate(changes):
        ws.append([order, ctx, label, field, old, new, url])
        r = i + 2
        link_cell = ws.cell(row=r, column=7)
        link_cell.hyperlink = url
        link_cell.font = st["link_font"]
        if i % 2 == 1:
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).fill = st["alt_fill"]

    _finish_sheet(ws, len(headers), len(changes),
                  [8, 32, 22, 30, 44, 44, 72])
    wb.save(path)


def write_updatelog_workbook(path, results, header_rgb):
    """Update run log: one row per structure attempted, in execution order.
    results = [(order, ctx, type_label, fields_str, status, read_back,
                timestr, url)]. Status and Read Back say what the page held
    when it was read again after Save Changes."""
    from openpyxl import Workbook

    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Update Log"

    headers = ["Order", "Context ID", "Type", "Fields Planned",
               "Status", "Read Back", "Time", "Config URL"]
    ws.append(headers)
    _style_header(ws, len(headers), st)

    for i, (order, ctx, label, fields_str, status, read_back, timestr,
            url) in enumerate(results):
        ws.append([order, ctx, label, fields_str, status, read_back,
                   timestr, url])
        r = i + 2
        link_cell = ws.cell(row=r, column=8)
        link_cell.hyperlink = url
        link_cell.font = st["link_font"]
        if i % 2 == 1:
            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).fill = st["alt_fill"]

    _finish_sheet(ws, len(headers), len(results),
                  [8, 32, 22, 40, 30, 60, 12, 72])
    wb.save(path)


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s.
# "targets" (scrape) and "report" (update) carry version counters; when one
# changes, the page refetches GET /api/targets to rebuild the type lists.
# "plan" summarizes the last dry run; apply is enabled only while
# plan.report_version still matches report.version.
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",   # idle | starting | harvesting | scraping | checking | updating | writing | done | error
    "paused": False,
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "last_file": None,      # last output workbook (report / update log)
    "plan_file": None,      # dry-run workbook
    "summary": "",
    "targets": {"loaded": False, "source": "", "count": 0, "version": 0},
    "report": {"loaded": False, "source": "", "count": 0, "version": 0},
    "plan": {"ready": False, "structures": 0, "changes": 0,
             "skipped": 0, "report_version": 0, "version": 0},
}
LOCK = threading.Lock()
SCRAPE_MODEL = None    # {"types": {...}, "source": str}
UPD_MODEL = None       # {"types", "rows", "fields", "source"}
PLAN = None            # {"items": [(ctx, label, [(field, old, new), ...])],
                       #  "report_version": int}
DRIVER = None          # one driver for the module's lifetime
DRIVER_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()

RUNNING_PHASES = ("starting", "harvesting", "scraping", "checking",
                  "updating", "writing")


class StopRequested(Exception):
    """Raised inside a worker when the user clicks Stop."""


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary.
RUN_LOG_NAME = None
CLEAR_RESETS = {"last_file": None, "plan_file": None, "paused": False}


# ---- DC-LOG 2 -------------------------------------------------------------
# The whole log of the current run, and Clear for a new run. Shared by
# every module with a log and duplicated verbatim, because modules are
# standalone: modules/_verify_pages.py fails if any copy differs.
#
# The page keeps the last 200 lines, so Copy log used to copy whatever the
# box held: the tail of this run, or the end of the previous one and the
# start of this, so a copied log could not be shared as the record of one
# run (downloader v1.34.2; the suite since 1.0.0). RUN_LOG starts empty at
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


# ---- DC-TONE 1 -------------------------------------------------------------
# The color of a finished run (1.0.1, three tones). Green: the state
# matches what the operator intended - a run that did what was asked, or a
# Stop the operator pressed. Amber: the run finished, with problems the
# operator should look at. Red: the run ended in error, or nothing it
# attempted succeeded. The module decides and puts it in STATE["outcome"];
# the page only shows it (logTone() in the DC-TONE script). Every
# set_state(phase="done", ...) passes outcome=, which _verify_pages.py checks
# with ast in every module. Duplicated verbatim; the copies must agree.
_WARNING_LINE_RE = re.compile(r"^\[[0-9:]+\]\s+WARNING\b")


def run_warnings():
    """How many WARNING lines the current run has logged."""
    with LOCK:
        return sum(1 for line in RUN_LOG if _WARNING_LINE_RE.match(line))


def outcome_tone(problems, succeeded=None):
    """"green", "amber" or "red" for a run that finished.

    `problems` counts what went wrong - failed items, warnings, refusals.
    `succeeded` is how many items came through, where the module counts
    them; None means it does not, and then a run is never called red on
    that ground."""
    if problems and succeeded == 0:
        return "red"
    return "amber" if problems else "green"
# ---- /DC-TONE --------------------------------------------------------------


# The page's settings, by element id, remembered across launches (DC-FORM, 1.0.1).
FORM_FIELDS = ("opscrape", "opupdate", "invdir", "repdir", "regen", "outdir",
)
# The page's other controls, which are NOT kept, and why: the workbooks found by a scan, the log copy box.
# Every control on the page is in one list or the other (_verify_pages.py).
FORM_NOT_KEPT = (
    "invsel", "repsel", "logfull",
)


# ---- DC-FORM 1 -------------------------------------------------------------
# The page's settings, remembered across launches (1.0.1; the downloader kept
# them for one launch since v1.34.2). After a restart, the image describer's
# profile went back to the default and its report folder to the Desktop,
# so a rerun meant for "Alt text only" ran Archival and wrote its report
# somewhere unexpected. Every module that
# runs a job keeps its page's settings in STATE["form"] and in a file beside
# the suite's configuration (config/forms/<module id>.json). Clear for a new
# run keeps them. Each module defines FORM_FIELDS, the ids of its page's
# settings, injected into the page so the two lists cannot drift.
# Duplicated verbatim; _verify_pages.py checks the copies agree and drives
# each one through a restart.
FORM_TEXT_MAX = 65536
FORM_PARENTS_MAX = 5000
FORM_PATH = None        # tests point this elsewhere; None means config/forms


def form_file():
    """Where this module's remembered form lives."""
    if FORM_PATH is not None:
        return Path(FORM_PATH)
    return DEFAULT_SESSION.parent / "forms" / (MANIFEST["id"] + ".json")


def validate_form_state(body, known_only=False):
    """The form as the page sent it, checked; raises ValueError if not.

    Only fields in FORM_FIELDS, each a bool (a box or a radio) or a string
    (a text field or a choice), plus the checked parent structures as a
    list of strings. From the page, anything else is refused whole rather
    than half-stored. From a file an earlier version wrote (known_only), a
    field this version no longer has is dropped instead."""
    if not isinstance(body, dict):
        raise ValueError("the form must be an object")
    fields = body.get("fields", {})
    parents = body.get("parents", [])
    if not isinstance(fields, dict) or not isinstance(parents, list):
        raise ValueError("fields must be an object and parents a list")
    out = {}
    for key, value in fields.items():
        if key not in FORM_FIELDS:
            if known_only:
                continue
            raise ValueError("unknown form field: {!r}".format(key))
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, str) and len(value) <= FORM_TEXT_MAX:
            out[key] = value
        else:
            raise ValueError("form field {} has an unusable value"
                             .format(key))
    if len(parents) > FORM_PARENTS_MAX or not all(
            isinstance(p, str) and len(p) <= 256 for p in parents):
        raise ValueError("parents must be a list of structure ids")
    return {"fields": out, "parents": list(parents)}


def load_form():
    """The form an earlier launch saved, or None. A file that cannot be
    read or used is said in the log, and the page starts from its
    defaults - never half-restored."""
    path = form_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log("NOTE: the page's saved settings ({}) could not be read - {}; "
            "the page starts from its defaults.".format(
                path.name, e.__class__.__name__))
        return None
    try:
        return validate_form_state(raw, known_only=True)
    except ValueError as e:
        log("NOTE: the page's saved settings ({}) were not usable - {}; "
            "the page starts from its defaults.".format(path.name, e))
        return None


def save_form(form):
    """Write the form beside the configuration. "" or what went wrong."""
    path = form_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(form, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
        return ""
    except OSError as e:
        return "{}: {}".format(e.__class__.__name__, e)


def form_for_page():
    """Put the remembered form in STATE the first time a page asks."""
    with LOCK:
        if STATE.get("form_loaded"):
            return
    form = load_form()
    with LOCK:
        if not STATE.get("form_loaded"):
            STATE["form"] = form
            STATE["form_loaded"] = True


def remember_form_request(handler):
    """POST /api/form. Accepted during a run too: it changes nothing about
    the run, and a page reopened mid-run is the one that needs it.
    Returns (payload, status code)."""
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        form = validate_form_state(json.loads(
            handler.rfile.read(length).decode("utf-8") or "{}"))
    except ValueError as e:
        return {"error": str(e)}, 400
    with LOCK:
        STATE["form"] = form
        STATE["form_loaded"] = True
        said = STATE.get("form_save_said")
    why = save_form(form)
    if why and not said:
        # Kept for this launch; said once, not on every keystroke.
        log("NOTE: the page's settings could not be saved for the next "
            "launch - {}. They are kept until this module stops.".format(why))
        with LOCK:
            STATE["form_save_said"] = True
    return {"ok": True, "saved": not why}, 200
# ---- /DC-FORM --------------------------------------------------------------


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
    set_state(phase="error", summary=msg, paused=False)


def set_scrape_model(model):
    """Install scrape targets and bump the UI's version counter."""
    global SCRAPE_MODEL
    with LOCK:
        SCRAPE_MODEL = model
        STATE["targets"] = {
            "loaded": True,
            "source": model["source"],
            "path": model.get("path", ""),
            "count": sum(len(v) for v in model["types"].values()),
            "version": STATE["targets"]["version"] + 1,
        }


def set_upd_model(model):
    """Install an update report and bump its version counter.
    Any previous dry-run plan no longer matches → invalidate it."""
    global UPD_MODEL, PLAN
    with LOCK:
        UPD_MODEL = model
        PLAN = None
        STATE["report"] = {
            "loaded": True,
            "source": model["source"],
            "path": model.get("path", ""),
            "count": sum(len(v) for v in model["types"].values()),
            "version": STATE["report"]["version"] + 1,
        }
        STATE["plan"] = {"ready": False, "structures": 0, "changes": 0,
                         "skipped": 0, "report_version": 0,
                         "version": STATE["plan"]["version"] + 1}


def set_plan(items, skipped):
    """Install a dry-run plan tied to the current report version."""
    global PLAN
    with LOCK:
        report_version = STATE["report"]["version"]
        PLAN = {"items": items, "report_version": report_version}
        STATE["plan"] = {
            "ready": True,
            "structures": sum(1 for it in items if it[2]),
            "changes": sum(len(it[2]) for it in items),
            "skipped": skipped,
            "report_version": report_version,
            "version": STATE["plan"]["version"] + 1,
        }


def check_pause_stop():
    """Call between units of work: honors Stop immediately and blocks
    while paused (Stop still works during a pause)."""
    if STOP_EVENT.is_set():
        raise StopRequested()
    while PAUSE_EVENT.is_set():
        if STOP_EVENT.is_set():
            raise StopRequested()
        time.sleep(0.2)


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


def eta_text(done, total, t0):
    """mm:ss estimate from the average pace so far."""
    if not done:
        return "ETA --:--"
    remaining = int((time.time() - t0) / done * (total - done))
    return "ETA {:02d}:{:02d}".format(*divmod(remaining, 60))


def _attach_or_fail(session):
    """Shared preamble for all workers. Returns a driver or None (failed)."""
    set_progress(0, 0, "Attaching to Chrome…")
    log("Attaching to debug Chrome at {}…".format(
        session["chrome"]["debugger_address"]))
    try:
        driver = get_driver(session)
    except Exception as e:
        fail("Could not attach to Chrome ({}). Start Chrome in debug mode "
             "from the suite Splash/Settings page, log into Digital "
             "Commons, then try again.".format(e.__class__.__name__))
        return None
    log("Attached.")
    return driver


def _primary_rgb(session) -> str:
    return session.get("branding", {}).get("colors", {}) \
                  .get("primary", FALLBACK_BRAND["primary"]).lstrip("#")


# The late-2026 configuration pages finish themselves with scripts after
# they load. One loads the disciplines list, whose box is marked
# multiSelectLoading until it has (its empty-valued placeholder option
# stays afterwards, with its text emptied); a page read before then gives a
# placeholder for a value. Another marks the Save Changes bar ready, from an
# animation-frame callback that Chrome does not run while the window is
# covered by other windows, so nothing here waits for the bar: Apply presses
# Save Changes with a script click (CLICK_JS), which submits the form as the
# button does, whatever Chrome has drawn. SETTLE_JS answers
# [document loaded, lists still loading].
SETTLE_JS = (
    "return [document.readyState === 'complete',"
    " document.querySelectorAll('.multiSelectLoading').length];")
SETTLE_WAIT = 15.0    # seconds for a page's scripts to finish


def unsettled_reason(state):
    """What has not finished on a page, from SETTLE_JS's answer; '' when
    everything has."""
    loaded, left = state
    why = []
    if not loaded:
        why.append("the page is still loading")
    if left:
        why.append("the disciplines list has not loaded")
    return "; ".join(why)


def _wait_settled(driver, timeout=None):
    """'' once the page's scripts have finished; otherwise, when the time
    runs out, what had not."""
    end = time.time() + (SETTLE_WAIT if timeout is None else timeout)
    while True:
        try:
            why = unsettled_reason(driver.execute_script(SETTLE_JS))
        except Exception as e:
            why = "it could not be checked ({})".format(first_line(e))
        if not why or time.time() >= end:
            return why
        time.sleep(0.25)


def _load_config_page(driver, url):
    """GET a config page and wait for its scripts to finish. Returns
    (page_source, why): why is '' when the page settled, else what had not
    finished when SETTLE_WAIT ran out. One 2 s retry if no form markup
    appears."""
    driver.get(url)
    why = _wait_settled(driver)
    html = driver.page_source
    if "user-config" not in html and "<form" not in html.lower():
        time.sleep(2.0)
        html = driver.page_source
    return html, why


def _selected_targets(model, labels):
    """[(type_label, ctx), ...] for the checked type sheets, in order."""
    pairs = []
    for label in labels:
        for ctx in model["types"].get(label, []):
            pairs.append((label, ctx))
    return pairs


# ---------------------------------------------------------------------------
# Worker 1: harvest the structure list fresh from the site-wide listing
# (fast — a single page load) and install it as the scrape targets.
# ---------------------------------------------------------------------------
def harvest_worker(session):
    base_url = session["base_url"].rstrip("/")
    try:
        set_state(phase="starting", summary="", paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        set_state(phase="harvesting")
        url = listing_url(base_url)
        set_progress(0, 0, "Loading site-wide structure listing…")
        log("Loading " + url)
        driver.get(url)
        time.sleep(3)  # DC listing pages are slow to settle
        html = driver.page_source

        if looks_like_login_page(html, driver.current_url):
            return fail(
                "Digital Commons redirected to a login page. Log in inside "
                "the debug Chrome window, then try again.")

        types = extract_contexts(html)
        if types is None:
            return fail(index_missing_message(html))
        for header, count in index_unknown_headers(html):
            log(index_unknown_note(header, count))
        if not types:
            return fail("No structures found on the listing page.")

        count = sum(len(v) for v in types.values())
        set_scrape_model({"types": types,
                          "source": "harvested just now from the site-wide "
                                    "listing"})
        set_progress(0, 0, "")
        set_state(phase="idle",
                  summary="{} structures across {} type(s) harvested. Check "
                          "the types to scrape, then Start."
                          .format(count, len(types)))
        log("Harvested {} structures across {} type(s)."
            .format(count, len(types)))
    except StopRequested:
        set_state(phase="idle", paused=False)
        set_progress(0, 0, "")
        log("Harvest stopped.")
    except Exception as e:
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# Worker 2: SCRAPE — visit each config page, extract fields, write the
# config report workbook (one sheet per type).
# ---------------------------------------------------------------------------
def scrape_worker(session, out_dir, labels):
    base_url = session["base_url"].rstrip("/")
    with LOCK:
        model = SCRAPE_MODEL
    if model is None:
        return fail("No structure list is loaded.")

    pairs = _selected_targets(model, labels)
    total = len(pairs)
    if not total:
        return fail("Nothing to scrape — check at least one structure type.")

    records = {}          # {label: [(ctx, fields), ...]} in visit order
    primary = _primary_rgb(session)

    def finish_report(partial_reason=None):
        """Write the config report workbook (partial or complete)."""
        done = sum(len(v) for v in records.values())
        if not done:
            return None
        set_state(phase="writing")
        set_progress(done, total, "Writing config report…")
        suffix = "_PARTIAL" if partial_reason else ""
        fname = "DC_Config_Report_{}{}.xlsx".format(
            time.strftime("%Y%m%d_%H%M%S"), suffix)
        path = os.path.join(out_dir, fname)
        write_config_workbook(path, records, base_url, primary)
        log("Wrote {}{}".format(
            path, " ({})".format(partial_reason) if partial_reason else ""))
        set_state(last_file=path)
        return path

    try:
        set_state(phase="starting", summary="", last_file=None, paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        set_state(phase="scraping")
        log("Scraping configuration fields for {} structure(s) across "
            "{} type(s) (~2 s each; Pause/Stop available)…"
            .format(total, len(labels)))

        unscoped = 0
        unsettled = 0
        t0 = time.time()
        for idx, (label, ctx) in enumerate(pairs, start=1):
            check_pause_stop()

            url = config_url(base_url, ctx)
            html, why = _load_config_page(driver, url)

            if looks_like_login_page(html, driver.current_url):
                path = finish_report("session expired; {} of {} scraped"
                                     .format(idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and start again — the partial "
                    "report holds what was scraped.".format(ctx))

            if why:
                unsettled += 1
                log("WARNING: '{}': the page did not finish loading within "
                    "{:.0f} s ({}) — not recorded.".format(
                        ctx, SETTLE_WAIT, why))
                set_progress(idx, total, "{} · {} · not recorded · {}".format(
                    label, ctx, eta_text(idx, total, t0)))
                continue

            fields, scoped = extract_fields(html)
            if not scoped:
                unscoped += 1
                log("No id=\"user-config\" element on '{}' — parsed the "
                    "whole page instead.".format(ctx))
            if not fields:
                log("No form fields found on '{}' — recorded as an empty "
                    "row.".format(ctx))
            records.setdefault(label, []).append((ctx, fields))

            set_progress(idx, total, "{} · {} · {} field(s) · {}".format(
                label, ctx, len(fields), eta_text(idx, total, t0)))

        path = finish_report()
        set_progress(total, total, "Done")
        summary = "{} of {} structure(s) scraped".format(total - unsettled,
                                                       total)
        if unsettled:
            summary += ("; {} not recorded — the page did not finish "
                        "loading (see log)".format(unsettled))
        if unscoped:
            summary += " ({} parsed unscoped — see log)".format(unscoped)
        if path:
            summary += " → " + os.path.basename(path)
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(unscoped + run_warnings()))
    except StopRequested:
        done = sum(len(v) for v in records.values())
        log("Stop requested — {} of {} scraped.".format(done, total))
        path = finish_report("stopped early; {} of {} done"
                             .format(done, total))
        if path:
            set_state(phase="done", paused=False,
                      outcome=outcome_tone(run_warnings()),
                      summary="Stopped early ({} of {} scraped). Partial "
                              "report: {}".format(done, total,
                                                  os.path.basename(path)))
        else:
            set_state(phase="idle", paused=False)
            set_progress(0, 0, "")
            log("Stopped before any structure was scraped.")
    except Exception as e:
        finish_report("unexpected error")
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# Worker 3: DRY RUN — re-read each structure's live config page, diff it
# against the loaded report's values, write the dry-run workbook, and
# install the plan that apply will execute. Nothing is changed.
# ---------------------------------------------------------------------------
def dryrun_worker(session, out_dir, labels):
    base_url = session["base_url"].rstrip("/")
    with LOCK:
        model = UPD_MODEL
    if model is None:
        return fail("No config report is loaded.")

    pairs = _selected_targets(model, labels)
    total = len(pairs)
    if not total:
        return fail("Nothing to check — check at least one type sheet.")

    items = []            # [(ctx, label, [(field, old, new), ...]), ...]
    changes = []          # dry-run workbook rows
    skipped = 0           # fields in the sheet but absent on the live page
    unsettled = 0         # pages whose scripts did not finish: not checked
    unplanned = 0         # multiple-choice differences: never applied
    primary = _primary_rgb(session)

    def finish_plan_workbook(partial_reason=None):
        if not changes:
            return None
        set_state(phase="writing")
        set_progress(len(items), total, "Writing dry-run workbook…")
        suffix = "_PARTIAL" if partial_reason else ""
        fname = "DC_Config_DryRun_{}{}.xlsx".format(
            time.strftime("%Y%m%d_%H%M%S"), suffix)
        path = os.path.join(out_dir, fname)
        write_dryrun_workbook(path, changes, primary)
        log("Wrote {}{}".format(
            path, " ({})".format(partial_reason) if partial_reason else ""))
        set_state(plan_file=path)
        return path

    try:
        set_state(phase="starting", summary="", plan_file=None, paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        set_state(phase="checking")
        log("DRY RUN — diffing sheet values against {} live config pages. "
            "Nothing will be changed.".format(total))

        order = 0
        t0 = time.time()
        for idx, (label, ctx) in enumerate(pairs, start=1):
            check_pause_stop()

            url = config_url(base_url, ctx)
            html, why = _load_config_page(driver, url)

            if looks_like_login_page(html, driver.current_url):
                finish_plan_workbook("session expired; {} of {} checked"
                                     .format(idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and run the dry run again."
                    .format(ctx))

            if why:
                unsettled += 1
                log("WARNING: '{}': the page did not finish loading within "
                    "{:.0f} s ({}) — not checked, and not in the plan."
                    .format(ctx, SETTLE_WAIT, why))
                set_progress(idx, total, "{} · {} · not checked · {}".format(
                    label, ctx, eta_text(idx, total, t0)))
                continue

            live, scoped = extract_fields(html)
            multis = multi_fields(html)
            if not scoped:
                log("No id=\"user-config\" element on '{}' — comparing "
                    "against the whole page.".format(ctx))

            sheet = model["rows"][label][ctx]
            diffs = []
            for field, new in sheet.items():
                if field not in live:
                    skipped += 1
                    log("'{}': field '{}' is in the sheet but not on the "
                        "live page — skipped.".format(ctx, field))
                    continue
                old = live[field]
                if old != new and field in multis:
                    unplanned += 1
                    log("'{}': '{}' differs from the sheet ({} item(s) on "
                        "the page, {} in the sheet), but Apply does not "
                        "change a multiple-choice list — not in the plan."
                        .format(ctx, field, item_count(old), item_count(new)))
                    continue
                if old != new:
                    diffs.append((field, old, new))
                    order += 1
                    changes.append((order, ctx, label, field, old, new, url))

            items.append((ctx, label, diffs))
            set_progress(idx, total, "{} · {} · {} change(s) · {}".format(
                label, ctx, len(diffs), eta_text(idx, total, t0)))

        path = finish_plan_workbook()
        set_plan(items, skipped)
        set_progress(total, total, "Done")
        n_structs = sum(1 for it in items if it[2])
        summary = ("Dry run complete: {} proposed change(s) across {} of {} "
                   "structure(s){}").format(
                       len(changes), n_structs, total,
                       "; {} sheet field(s) not on the live pages — see "
                       "log".format(skipped) if skipped else "")
        if unsettled:
            summary += ("; {} structure(s) not checked — the page did not "
                        "finish loading (see log)".format(unsettled))
        if unplanned:
            summary += ("; {} difference(s) in multiple-choice lists not "
                        "planned — Apply does not change them (see log)"
                        .format(unplanned))
        if path:
            summary += " → " + os.path.basename(path)
        if changes:
            summary += ". Review, then Apply changes."
        else:
            summary += ". Nothing to apply."
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(skipped + unplanned + run_warnings()))
        log(summary)
    except StopRequested:
        finish_plan_workbook("stopped early; {} of {} checked"
                             .format(len(items), total))
        set_state(phase="idle", paused=False)
        set_progress(0, 0, "")
        log("Dry run stopped — no plan installed. Run the dry run again "
            "to completion before applying.")
    except Exception as e:
        finish_plan_workbook("unexpected error")
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# Worker 4: APPLY — execute the dry-run plan: set each structure's changed
# fields in its config form (never typed), read them back, press Save
# Changes, read the page again and record what it holds; regenerate only a
# structure saved as planned. Only structures the dry run found changes
# for are touched.
# ---------------------------------------------------------------------------
def apply_summary(results, total):
    """'N of M structure(s) saved as planned', then every other outcome
    counted by name, from the Status each structure's page was read back
    to. A save whose outcome is not known is never counted as a failure."""
    counts = {}
    regen_failed = 0
    for r in results:
        status = r[4]
        if "regeneration failed" in status:
            regen_failed += 1
        key = status.split(" (")[0].split(" +")[0]
        counts[key] = counts.get(key, 0) + 1
    text = "{} of {} structure(s) saved as planned".format(
        counts.pop("Saved", 0), total)
    words = (("NOT AS PLANNED", "{} changed NOT as planned — check "
              "these first"),
             ("PARTLY SAVED", "{} partly saved"),
             ("UNKNOWN", "{} unknown — run a dry run to see what the "
              "page holds"),
             ("NOT SAVED", "{} not saved"),
             ("ERROR before Save Changes", "{} stopped by an error before "
              "Save Changes"),
             ("LOGIN REQUIRED", "{} stopped by the login page"))
    for key, word in words:
        if counts.get(key):
            text += "; " + word.format(counts.pop(key))
    for key, n in sorted(counts.items()):
        text += "; {} {}".format(n, key)
    if regen_failed:
        text += "; {} saved but not regenerated (regeneration failed)".format(
            regen_failed)
    return text


def apply_worker(session, out_dir, regen):
    base_url = session["base_url"].rstrip("/")
    with LOCK:
        plan = PLAN
        plan_ok = (plan is not None
                   and plan["report_version"] == STATE["report"]["version"])
    if not plan_ok:
        return fail("No current dry-run plan. Run a dry run first.")

    todo = [(ctx, label, diffs) for (ctx, label, diffs) in plan["items"]
            if diffs]
    total = len(todo)
    if not total:
        return fail("The dry run found no changes — nothing to apply.")

    results = []          # update-log workbook rows
    primary = _primary_rgb(session)

    def finish_log(partial_reason=None):
        if not results:
            return None
        set_state(phase="writing")
        set_progress(len(results), total, "Writing update log…")
        suffix = "_PARTIAL" if partial_reason else ""
        fname = "DC_Config_UpdateLog_{}{}.xlsx".format(
            time.strftime("%Y%m%d_%H%M%S"), suffix)
        path = os.path.join(out_dir, fname)
        write_updatelog_workbook(path, results, primary)
        log("Wrote {}{}".format(
            path, " ({})".format(partial_reason) if partial_reason else ""))
        set_state(last_file=path)
        return path

    try:
        set_state(phase="starting", summary="", last_file=None, paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        set_state(phase="updating")
        log("APPLYING {} change(s) to {} structure(s){}…".format(
            sum(len(d) for (_, _, d) in todo), total,
            ", regenerating each afterward" if regen else ""))

        t0 = time.time()
        for idx, (ctx, label, diffs) in enumerate(todo, start=1):
            check_pause_stop()

            url = config_url(base_url, ctx)
            fields_str = "; ".join(f for (f, _, _) in diffs)
            timestr = time.strftime("%H:%M:%S")

            try:
                status, readback = _apply_one(driver, base_url, ctx,
                                              diffs, regen)
            except LoginRedirect:
                results.append((idx, ctx, label, fields_str,
                                "LOGIN REQUIRED",
                                "not known: run a dry run to see what the "
                                "page holds", timestr, url))
                finish_log("session expired; {} of {} done"
                           .format(idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window, run a fresh DRY RUN (some "
                    "structures may already be updated), then apply again."
                    .format(ctx))
            except Exception as e:
                status = "ERROR before Save Changes ({})".format(first_line(e))
                readback = "nothing was saved"
                log("'{}': {} — nothing was saved.".format(ctx, first_line(e)))

            results.append((idx, ctx, label, fields_str, status, readback,
                            time.strftime("%H:%M:%S"), url))
            set_progress(idx, total, "{} · {} · {} · {}".format(
                label, ctx, status, eta_text(idx, total, t0)))

        path = finish_log()
        set_progress(total, total, "Done")
        ok = sum(1 for r in results if r[4].startswith("Saved"))
        summary = apply_summary(results, total)
        if path:
            summary += " → " + os.path.basename(path)
        summary += ". Run a new SCRAPE to capture the current state."
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(total - ok + run_warnings(),
                                       succeeded=ok))

        # The plan is spent — require a fresh dry run before another apply.
        with LOCK:
            globals()["PLAN"] = None
            STATE["plan"] = {"ready": False, "structures": 0, "changes": 0,
                             "skipped": 0, "report_version": 0,
                             "version": STATE["plan"]["version"] + 1}
    except StopRequested:
        log("Stop requested after {} of {} structure(s).".format(
            len(results), total))
        path = finish_log("stopped early; {} of {} done"
                          .format(len(results), total))
        if path:
            set_state(phase="done", paused=False,
                      outcome=outcome_tone(
                          sum(1 for r in results
                              if not r[4].startswith("Saved"))
                          + run_warnings()),
                      summary="Stopped early after {} of {} structure(s): "
                              "{}. Partial update log: {}. Run a fresh dry "
                              "run before applying again.".format(
                                  len(results), total,
                                  apply_summary(results, total),
                                  os.path.basename(path)))
        else:
            set_state(phase="idle", paused=False)
            set_progress(0, 0, "")
            log("Stopped before any structure was tried.")
        with LOCK:
            globals()["PLAN"] = None
            STATE["plan"] = {"ready": False, "structures": 0, "changes": 0,
                             "skipped": 0, "report_version": 0,
                             "version": STATE["plan"]["version"] + 1}
    except Exception as e:
        finish_log("unexpected error; {} of {} done"
                   .format(len(results), total))
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


class LoginRedirect(Exception):
    """Raised by _apply_one when DC bounces to a login page mid-run."""


# Values are SET in the page, never typed. Typing sends each character as a
# keypress: a tab moves focus to the next field, and a line break typed into
# a one-line field submits the form half-filled.
SET_VALUE_JS = (
    "var el = arguments[0], v = arguments[1];"
    "var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype"
    " : HTMLInputElement.prototype;"
    "Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);"
    "el.dispatchEvent(new Event('input', {bubbles: true}));"
    "el.dispatchEvent(new Event('change', {bubbles: true}));")
READ_VALUE_JS = "return arguments[0].value;"
CLICK_JS = "arguments[0].click();"
# Which document is showing, and whether it has finished loading: a new
# page has another performance.timeOrigin. Read only.
DOC_JS = "return [document.readyState, performance.timeOrigin];"
IN_BAR_JS = "return !!arguments[0].closest('td.config-actions-sticky');"
TEXT_TYPES = ("text", "email", "url", "number", "password", "")
SAVE_WAIT = 20.0      # seconds for the page after Save Changes to load
# A page read back right after Save Changes was answered can still show the
# value from before the save, and the save show on a later read. Seconds to
# wait before each further read: about 2, 5 and 10 s after the first.
READ_AGAIN = (2.0, 3.0, 5.0)


def first_line(e):
    """An exception named by its class and the first line of its message.
    A Selenium message carries a stack trace that tells staff nothing."""
    lines = str(e).strip().splitlines()
    msg = lines[0].strip() if lines else ""
    if msg.startswith("Message:"):
        msg = msg[len("Message:"):].strip()
    name = e.__class__.__name__
    return "{}: {}".format(name, msg) if msg else name


def _lines(s):
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def form_holds(kind, new, observed):
    """True when a field set in the form holds its planned value. `kind`
    is "text", "checkbox", "radio" or "select"; `observed` is what the form
    reports: the value, whether the box is checked, the checked radio's
    value, or the selected option's (text, value)."""
    if kind == "text":
        return _lines(observed) == _lines(new)
    if kind == "checkbox":
        return bool(observed) == bool(new)
    if kind == "radio":
        return (observed or "") == new
    if kind == "select":
        text, value = observed
        return new in (text, value) if new else "" in (text, value)
    return False


def form_mismatch(kind, new, observed, one_line):
    """Why a field does not hold its planned value — sizes, never content."""
    if kind == "text":
        why = "the form holds {} character(s), planned {}".format(
            len(_lines(observed)), len(_lines(new)))
        if one_line and "\n" in _lines(new):
            why += "; a one-line field cannot hold a line break"
        return why
    return "the form shows {!r}, planned {!r}".format(observed, new)


def field_outcomes(diffs, live):
    """[(field, outcome)] for each planned field, by the dry run's own
    comparison with the page as read again after saving."""
    out = []
    for field, old, new in diffs:
        if field not in live:
            out.append((field, "not on the page after saving"))
        elif live[field] == new:
            out.append((field, "saved as planned"))
        elif live[field] == old:
            out.append((field, "unchanged"))
        else:
            out.append((field, "changed, NOT as planned: holds {} "
                        "character(s), planned {}".format(len(live[field]),
                                                          len(new))))
    return out


def saved_status(outcomes, skipped):
    """The update log's Status for a structure whose page was read back."""
    planned = sum(1 for _f, o in outcomes if o == "saved as planned")
    unchanged = sum(1 for _f, o in outcomes if o == "unchanged")
    if planned == len(outcomes):
        if skipped:
            return "Saved ({} field(s) skipped)".format(skipped)
        return "Saved"
    if unchanged == len(outcomes):
        return "NOT SAVED"
    if planned + unchanged == len(outcomes):
        return "PARTLY SAVED"
    return "NOT AS PLANNED"


def read_back_text(notes):
    return "; ".join("{}: {}".format(f, n) for f, n in notes)


def _form_value(driver, kind, elements):
    el0 = elements[0]
    if kind == "text":
        return driver.execute_script(READ_VALUE_JS, el0)
    if kind == "checkbox":
        return el0.is_selected()
    if kind == "radio":
        for el in elements:
            if el.is_selected():
                return el.get_attribute("value") or ""
        return ""
    from selenium.webdriver.support.ui import Select
    opt = Select(el0).first_selected_option
    return ((opt.text or "").strip(), opt.get_attribute("value") or "")


def _save_button(driver, scope):
    """The Save Changes button to press: the one in the sticky Save bar of
    the late-2026 pages, else the first one shown and enabled (older
    pages); never one the page's own layout hides, such as the top row
    those pages hide. Returns (button or None, how many the page has)."""
    from selenium.webdriver.common.by import By
    xp = "input[@type='submit' and @name='x_process_changes']"
    found = scope.find_elements(By.XPATH, ".//" + xp)
    if not found:
        found = driver.find_elements(By.XPATH, "//" + xp)
    for btn in found:
        try:
            if driver.execute_script(IN_BAR_JS, btn):
                return btn, len(found)
        except Exception:
            continue
    for btn in found:
        try:
            if btn.is_displayed() and btn.is_enabled():
                return btn, len(found)
        except Exception:
            continue
    return None, len(found)


def _after_press(driver, origin, timeout):
    """What the page did after Save Changes was pressed: "loaded" once a new
    document (another performance.timeOrigin) has finished loading;
    "unchanged" if the old one is still there, answering, when the time
    runs out; "pending" if a new one began, or the page stopped answering
    while it navigated, and had not finished. A script click returns at
    once, before the form's request has been answered, and a page read
    back before then is read before the save is written."""
    end = time.time() + timeout
    began = False
    while True:
        try:
            ready, now = driver.execute_script(DOC_JS)
            if now != origin:
                began = True
                if ready == "complete":
                    return "loaded"
        except Exception:
            began = True         # navigating: the old document has gone
        if time.time() >= end:
            return "pending" if began else "unchanged"
        time.sleep(0.25)


def _open_form(driver, url):
    """Load a config page for Apply and wait for its scripts to finish.
    Returns (scope, why): why is '' when the page settled. Raises
    LoginRedirect, or RuntimeError when the form is not on the page."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get(url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "user-config")))
        scope = driver.find_element(By.ID, "user-config")
    except Exception:
        if looks_like_login_page(driver.page_source, driver.current_url):
            raise LoginRedirect()
        raise RuntimeError("config form (id=user-config) not found")
    return scope, _wait_settled(driver)


def _answer_page(driver, planned):
    """The page that answered Save Changes, for the log: its title, its
    address, and each planned field as that page's own form shows it.
    Never raises."""
    try:
        title = " ".join((driver.title or "").split())[:80] or "(no title)"
    except Exception:
        title = "(title not read)"
    try:
        here = driver.current_url or "(no address)"
    except Exception:
        here = "(address not read)"
    try:
        live, scoped = extract_fields(driver.page_source)
    except Exception as e:
        return '"{}" at {}, whose contents could not be read ({})'.format(
            title, here, first_line(e))
    if not scoped:
        return '"{}" at {}, with no configuration form'.format(title, here)
    return '"{}" at {}, whose form shows {}'.format(
        title, here, _shown(field_outcomes(planned, live)))


def _shown(outcomes):
    return "; ".join("{} {}".format(f, o) for f, o in outcomes)


def _all_saved(outcomes):
    return all(o == "saved as planned" for _f, o in outcomes)


def _read_again(driver, url, ctx, planned, outcomes, notes):
    """After a press that brought a new page: while the page read back does
    not show every planned field saved as planned, wait each READ_AGAIN
    pause and read it again, logging every read. Returns the outcomes of
    the last read made; adds a Read Back note when the save showed only on
    a later read. Raises LoginRedirect."""
    waited = 0.0
    for n, pause in enumerate(READ_AGAIN, start=2):
        if _all_saved(outcomes):
            break
        log("'{}': read {} of the page shows {} — reading it again in "
            "{:.0f} s.".format(ctx, n - 1, _shown(outcomes), pause))
        time.sleep(pause)
        waited += pause
        try:
            html, why = _load_config_page(driver, url)
            here = driver.current_url
        except Exception as e:
            log("'{}': read {} could not be made ({}); the status is from "
                "read {}.".format(ctx, n, first_line(e), n - 1))
            return outcomes
        if looks_like_login_page(html, here):
            raise LoginRedirect()
        live, scoped = ({}, False) if why else extract_fields(html)
        if not scoped:
            log("'{}': read {} found no settled configuration form{}; the "
                "status is from read {}.".format(
                    ctx, n, " ({})".format(why) if why else "", n - 1))
            return outcomes
        outcomes = field_outcomes(planned, live)
        log("'{}': read {}, after waiting {:.0f} s, shows {}.".format(
            ctx, n, waited, _shown(outcomes)))
        if _all_saved(outcomes):
            notes.append(("Read back", "saved as planned only at read {}, "
                          "after waiting {:.0f} s".format(n, waited)))
    return outcomes


def _apply_attempt(driver, url, ctx, diffs):
    """One try at saving a structure's planned changes. Returns either
    {"final": (status, notes)} — nothing more to do — or {"outcomes",
    "notes", "skipped", "replaced"} once Save Changes was pressed and the
    page read back. Raises LoginRedirect."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    scope, why = _open_form(driver, url)
    if why:
        log("'{}': the page did not finish loading within {:.0f} s ({}) — "
            "nothing was saved.".format(ctx, SETTLE_WAIT, why))
        return {"final": ("NOT SAVED (the page did not finish loading)",
                          [(f, "not saved: the page did not finish "
                            "loading") for f, _o, _n in diffs])}

    notes = []          # (field, what happened) for the Read Back column
    skipped = 0
    to_check = []       # (field, old, new, kind, elements, one_line)
    for field, old, new in diffs:
        try:
            elements = scope.find_elements(By.NAME, field)
        except Exception:
            elements = []
        if not elements:
            skipped += 1
            notes.append((field, "skipped: not on the live page"))
            log("'{}': field '{}' vanished from the live page — skipped."
                .format(ctx, field))
            continue

        el0 = elements[0]
        tag = el0.tag_name.lower()
        itype = (el0.get_attribute("type") or "").lower()
        if tag == "select" and el0.get_attribute("multiple"):
            skipped += 1
            notes.append((field, "skipped: Apply does not change a "
                          "multiple-choice list"))
            log("'{}': '{}' is a multiple-choice list, which Apply does not "
                "change — skipped.".format(ctx, field))
            continue
        try:
            if tag == "textarea" or (tag == "input" and itype in TEXT_TYPES):
                driver.execute_script(SET_VALUE_JS, el0, new)
                kind = "text"
            elif tag == "input" and itype == "checkbox":
                if el0.is_selected() != bool(new):
                    el0.click()
                kind = "checkbox"
            elif tag == "input" and itype == "radio":
                if not new:
                    skipped += 1
                    notes.append((field, "skipped: a radio group cannot be "
                                  "blanked"))
                    log("'{}': can't blank radio group '{}' — skipped."
                        .format(ctx, field))
                    continue
                hit = None
                for el in elements:
                    if (el.get_attribute("value") or "") == new:
                        hit = el
                        break
                if hit is None:
                    skipped += 1
                    notes.append((field, "skipped: no such option"))
                    log("'{}': no radio option '{}' in group '{}' — skipped."
                        .format(ctx, new, field))
                    continue
                if not hit.is_selected():
                    hit.click()
                kind = "radio"
            elif tag == "select":
                sel = Select(el0)
                if new:
                    try:
                        sel.select_by_visible_text(new)
                    except Exception:
                        sel.select_by_value(new)
                else:
                    sel.select_by_index(0)
                kind = "select"
            else:
                skipped += 1
                notes.append((field, "skipped: unsupported element"))
                log("'{}': unsupported element for field '{}' — skipped."
                    .format(ctx, field))
                continue
        except Exception as e:
            skipped += 1
            notes.append((field, "skipped: could not be set ({})"
                          .format(first_line(e))))
            log("'{}': couldn't set field '{}' ({}).".format(
                ctx, field, first_line(e)))
            continue
        to_check.append((field, old, new, kind, elements,
                         tag == "input" and kind == "text"))

    if not to_check:
        return {"final": ("NOT SAVED (no field could be set)", notes)}

    # Read every field back from the form before anything is saved.
    for field, _old, new, kind, elements, one_line in to_check:
        try:
            observed = _form_value(driver, kind, elements)
            held = form_holds(kind, new, observed)
            why = "" if held else form_mismatch(kind, new, observed,
                                                one_line)
        except Exception as e:
            held, why = False, "could not be read ({})".format(first_line(e))
        if not held:
            notes.append((field, "not saved: " + why))
            log("'{}': field '{}' does not hold its planned value in the "
                "form ({}) — nothing was saved for this structure."
                .format(ctx, field, why))
            return {"final": ("NOT SAVED (a field would not hold its value)",
                              notes)}

    btn, count = _save_button(driver, scope)
    if btn is None:
        log("'{}': no Save Changes button is shown ({} on the page) — "
            "nothing was saved.".format(ctx, count))
        return {"final": ("NOT SAVED (no Save Changes button shown; {} on "
                          "the page)".format(count), notes)}

    try:
        origin = driver.execute_script(DOC_JS)[1]
    except Exception:
        origin = None
    # From here on the outcome is read from the page, never assumed.
    try:
        driver.execute_script(CLICK_JS, btn)
    except Exception as e:
        log("'{}': pressing Save Changes raised {} — the page is read "
            "back to see what it holds.".format(ctx, first_line(e)))
    after = _after_press(driver, origin, SAVE_WAIT)
    if after == "pending":
        log("'{}': Save Changes was pressed and a new page began, but it did "
            "not finish loading within {:.0f} s, so what was saved is not "
            "known. Run a dry run to see what the page holds."
            .format(ctx, SAVE_WAIT))
        return {"final": ("UNKNOWN (Save Changes pressed; the page after it "
                          "did not finish loading)", notes)}
    replaced = after == "loaded"
    planned = [(f, o, n) for f, o, n, _k, _e, _l in to_check]
    if replaced:
        log("'{}': Save Changes was answered with the page {}."
            .format(ctx, _answer_page(driver, planned)))
    else:
        log("'{}': the page did not change within {:.0f} s of Save Changes "
            "— the page is read back to see what it holds."
            .format(ctx, SAVE_WAIT))

    try:
        html, why = _load_config_page(driver, url)
        here = driver.current_url
    except Exception as e:
        log("'{}': the configuration page could not be read back after "
            "Save Changes ({}). Run a dry run to see what it holds."
            .format(ctx, first_line(e)))
        return {"final": ("UNKNOWN (Save Changes pressed; the page could not "
                          "be read back)", notes)}
    if looks_like_login_page(html, here):
        raise LoginRedirect()
    if why:
        log("'{}': the page read back after Save Changes did not finish "
            "loading within {:.0f} s ({}). Run a dry run to see what it "
            "holds.".format(ctx, SETTLE_WAIT, why))
        return {"final": ("UNKNOWN (Save Changes pressed; the page read back "
                          "did not finish loading)", notes)}
    live, scoped = extract_fields(html)
    if not scoped:
        log("'{}': the page read back after Save Changes has no "
            "configuration form. Run a dry run to see what it holds."
            .format(ctx))
        return {"final": ("UNKNOWN (Save Changes pressed; no configuration "
                          "form on the page read back)", notes)}
    outcomes = field_outcomes(planned, live)
    if replaced:
        outcomes = _read_again(driver, url, ctx, planned, outcomes, notes)
    return {"outcomes": outcomes, "notes": notes, "skipped": skipped,
            "replaced": replaced}


def _apply_one(driver, base_url, ctx, diffs, regen):
    """Apply one structure's planned changes. Returns (status, read_back)
    for the update log. Raises LoginRedirect on a login bounce.

    1. The page is loaded and its scripts are waited for (SETTLE_JS); a
       page that does not finish is not touched.
    2. Each field is SET in the form, never typed (SET_VALUE_JS); a
       multiple-choice list is never changed.
    3. Each field set is read back from the form. If one does not hold its
       planned value, nothing is saved for this structure.
    4. Save Changes is pressed by a script click on the Save bar's button
       (CLICK_JS), which does not depend on what Chrome has drawn.
    5. The page that follows the press is waited for until it has finished
       loading (DOC_JS); one that does not is UNKNOWN. Then the
       configuration page is loaded again and each planned field is
       compared with the plan as a dry run compares it, so the status says
       what the page holds, whatever happened in between. The page that
       answered the press is described in the log; if the page read back
       does not show every field as planned, it is read again after about
       2, 5 and 10 s (READ_AGAIN), each read logged.
    6. If the press did not change the page and every planned field is
       still unchanged, steps 1-5 run once more; never otherwise.
    7. Regeneration only when every field set was saved as planned."""
    url = config_url(base_url, ctx)
    got = _apply_attempt(driver, url, ctx, diffs)
    pressed_twice = False
    if ("final" not in got and not got["replaced"]
            and all(o == "unchanged" for _f, o in got["outcomes"])):
        log("'{}': Save Changes did not change the page and every planned "
            "field is unchanged — trying once more.".format(ctx))
        pressed_twice = True
        got = _apply_attempt(driver, url, ctx, diffs)
    if "final" in got:
        status, notes = got["final"]
        return status, read_back_text(notes)

    outcomes, notes = got["outcomes"], got["notes"]
    for field, outcome in outcomes:
        log("'{}': {} — {}.".format(ctx, field, outcome))
    status = saved_status(outcomes, got["skipped"])
    notes = outcomes + notes
    if pressed_twice:
        notes.append(("Save Changes", "pressed twice; the first press did "
                      "not change the page"))

    if regen:
        if status.startswith("Saved"):
            try:
                driver.get(regen_url(base_url, ctx))
                time.sleep(0.8)   # let the regeneration request settle
                if looks_like_login_page(driver.page_source,
                                         driver.current_url):
                    raise LoginRedirect()
                status += " + regenerated"
                log("'{}': regeneration requested.".format(ctx))
            except LoginRedirect:
                raise
            except Exception as e:
                status += " + regeneration failed ({})".format(first_line(e))
                log("'{}': regeneration failed ({}).".format(
                    ctx, first_line(e)))
        else:
            status += " (not regenerated)"
            log("'{}': not regenerated, because a field was not saved as "
                "planned.".format(ctx))
    return status, read_back_text(notes)


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
 input[type=text]{width:100%;padding:.45rem .6rem;border:1px solid #a9a396;border-radius:5px;font:inherit}
 select{width:100%;padding:.45rem .6rem;border:1px solid #a9a396;border-radius:5px;font:inherit;background:#fff}
 button{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
        padding:.6rem 1.3rem;font:inherit;font-weight:600;cursor:pointer}
 button.secondary{background:#fff;color:var(--primary);border:2px solid var(--primary)}
 button.small{padding:.45rem .8rem;font-weight:600}
 button.danger{background:#8c2f0d}
 button:disabled{opacity:.55;cursor:not-allowed}
 .row{display:flex;gap:.6rem;align-items:end;flex-wrap:wrap;margin:.4rem 0}
 .row>div{flex:1;min-width:12rem}
 .controls{display:flex;gap:.6rem;flex-wrap:wrap}
 label{display:block;margin-bottom:.3rem}
 .inline{display:flex;gap:.45rem;align-items:center;margin:.35rem 0}
 .inline label{margin:0}
 .hint{font-size:.85rem;color:#54524c;margin:.35rem 0 0}
 .sum{margin:.6rem 0 0;padding:.5rem .7rem;border-radius:6px;background:#eef1f5;
      border:1px solid #d7d4cc;font-size:.9rem}
 .sum.loaded{background:#e8f0e6;border-color:#4a6741}
 .sum.warn{background:#f9f1df;border-color:__ACCENT__}
 .typelist{border:1px solid #d7d4cc;border-radius:6px;margin-top:.5rem;
           background:#fff;max-height:12rem;overflow:auto}
 .typelist .item{display:flex;gap:.5rem;align-items:baseline;padding:.3rem .6rem;
                 border-bottom:1px solid #efece6}
 .typelist .item:last-child{border-bottom:0}
 .typelist .meta{font-size:.82rem;color:#54524c}
 :focus-visible{outline:3px solid #1a5dc8;outline-offset:2px}
 #status{margin:1rem 0;padding:.7rem .9rem;border-radius:6px;background:#eef1f5;border:1px solid #d7d4cc}
 #status.done{background:#e8f0e6;border-color:#4a6741}
 #status.error{background:#f7e8e2;border-color:#a33a12}
 #status a{font-weight:600}
 progress{width:100%;height:.8rem;margin:.5rem 0 0}
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
/* DC-LOG 2 styles */
.logbar{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:1rem}
.logbar h2{margin:0}
.logbar #logcopy{margin-left:auto}
.logbar #logcopy,.logbar #logclear{padding:.25rem .6rem;font-size:.85rem}
.lognote{font-size:.85rem;color:#54524c}
#log{user-select:text;-webkit-user-select:text}
.clearask{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.4rem 0;
  padding:.5rem .7rem;border-radius:6px;border:1px solid #9aa5ad;background:#eef1f5}
.logfull{width:100%;margin-top:.4rem;font:12px/1.4 ui-monospace,monospace}
/* /DC-LOG styles */
/* DC-BROWSE 1 styles */
.browse-row{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}
.browse-row>input{flex:1 1 16rem;min-width:0}
.browse-row>button{flex:0 0 auto;white-space:nowrap}
.browse-files{margin-top:.3rem}
.browse-msg{display:block;font-size:.9em;margin-top:.2rem}
.browse-msg:empty{display:none}
/* /DC-BROWSE styles */
/* DC-PALETTE 3 — one notification palette for the shell and every module.
   Gray: instructions and neutral state. Green: the state matches what the
   operator intended. Amber (version 3): a run that finished, with problems
   to look at. Red: errors, and a run that ended in error or in which
   nothing succeeded. The same block, byte for byte, in every page;
   modules/_verify_pages.py fails the build if any copy differs, and checks
   each ink against its background for WCAG 2.1 AA contrast. It sits last in
   each page's <style>, so it wins over the older per-page colors. */
:root{--dc-info-bg:#eef1f5;--dc-info-line:#9aa5ad;--dc-info-ink:#2c3440;
 --dc-ok-bg:#eaf5ec;--dc-ok-line:#1d6b34;--dc-ok-ink:#14522a;
 --dc-bad-bg:#fbecec;--dc-bad-line:#a3252c;--dc-bad-ink:#7c1c22;
 --dc-amber-bg:#fff4d6;--dc-amber-line:#b07d12;--dc-amber-ink:#6b4300}
#status,#status.waiting,#cerr.notice,.sum,.sum.warn,.status.info{
 background:var(--dc-info-bg);border-color:var(--dc-info-line);color:var(--dc-info-ink)}
#status.done,#cerr.ok,.sum.loaded,.status.good,.notice{
 background:var(--dc-ok-bg);border-color:var(--dc-ok-line);color:var(--dc-ok-ink)}
#status.error,#cerr,.sum.bad,.status.bad,.status.warn,.notice.err{
 background:var(--dc-bad-bg);border-color:var(--dc-bad-line);color:var(--dc-bad-ink)}
#status.amber,#cerr.amber,.status.amber{
 background:var(--dc-amber-bg);border-color:var(--dc-amber-line);color:var(--dc-amber-ink)}
.dc-amber{color:var(--dc-amber-ink)}
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


<fieldset><legend>1 · Operation</legend>
 <div class="inline">
  <input type="radio" id="opscrape" name="op" value="scrape" checked>
  <label for="opscrape">Scrape configurations (read-only — writes a config
   report workbook)</label>
 </div>
 <div class="inline">
  <input type="radio" id="opupdate" name="op" value="update">
  <label for="opupdate">Update configurations (dry-run preview first, then
   apply an edited config report)</label>
 </div>
</fieldset>

<div id="scrapeSec">
<fieldset><legend>2 · Structures to scrape</legend>
 <div class="row">
  <div>
   <label for="invdir">Workbooks folder</label>
   <span class="browse-row"><input type="text" id="invdir" data-path="folder" value="" autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="invdir" data-title="Workbooks folder" aria-label="Browse for Workbooks folder" hidden>Browse…</button></span><span class="browse-msg" id="invdir-browsemsg" role="status" aria-live="polite"></span>
  </div>
  <button type="button" id="invscan" class="small secondary">Scan</button>
 </div>
 <div class="row">
  <div>
   <label for="invsel">Structure URL Generator inventory</label>
   <select id="invsel" aria-describedby="invhint" disabled>
    <option value="">— scan the folder to list inventories —</option>
   </select>
  </div>
  <button type="button" id="invload" class="small" disabled>Load</button>
 </div>
 <p class="hint" id="invhint">Inventories are DC_URL_Inventory_*.xlsx files
 written by the Structure URL Generator, listed newest first — or skip them:</p>
 <div class="row">
  <button type="button" id="harvest" class="secondary">Harvest structure
   list now (reads the site-wide listing)</button>
 </div>
 <div id="tgtsum" class="sum" role="status" aria-live="polite">No structure list loaded yet.</div>
 <div id="tgtlist" class="typelist" role="group" aria-label="Structure types to scrape"></div>
</fieldset>
</div>

<div id="updSec" hidden>
<fieldset><legend>2 · Config report to apply</legend>
 <div class="row">
  <div>
   <label for="repdir">Workbooks folder</label>
   <span class="browse-row"><input type="text" id="repdir" data-path="folder" value="" autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="repdir" data-title="Workbooks folder" aria-label="Browse for Workbooks folder" hidden>Browse…</button></span><span class="browse-msg" id="repdir-browsemsg" role="status" aria-live="polite"></span>
  </div>
  <button type="button" id="repscan" class="small secondary">Scan</button>
 </div>
 <div class="row">
  <div>
   <label for="repsel">Config report</label>
   <select id="repsel" aria-describedby="rephint" disabled>
    <option value="">— scan the folder to list config reports —</option>
   </select>
  </div>
  <button type="button" id="repload" class="small" disabled>Load</button>
 </div>
 <p class="hint" id="rephint">Config reports are DC_Config_Report_*.xlsx files
 written by this module's scrape mode — usually one you've edited. Cells hold
 the values the forms SHOULD have; a blank cell means "clear the field".</p>
 <div id="repsum" class="sum" role="status" aria-live="polite">No config report loaded yet.</div>
 <div id="replist" class="typelist" role="group" aria-label="Type sheets to include"></div>
 <div class="inline" style="margin-top:.6rem">
  <input type="checkbox" id="regen" checked>
  <label for="regen">Regenerate each structure after its changes are
   submitted (recommended)</label>
 </div>
 <div id="plansum" class="sum" role="status" aria-live="polite">No dry run yet
  — Apply stays locked until a dry run completes.</div>
</fieldset>
</div>

<fieldset><legend>3 · Output</legend>
 <label for="outdir">Output folder (reports · dry-run previews · update logs)</label>
 <span class="browse-row"><input type="text" id="outdir" data-path="folder" value="" autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="outdir" data-title="Output folder (reports · dry-run previews · update logs)" aria-label="Browse for Output folder (reports · dry-run previews · update logs)" hidden>Browse…</button></span><span class="browse-msg" id="outdir-browsemsg" role="status" aria-live="polite"></span>
</fieldset>

<div class="controls">
 <button type="button" id="goscrape" disabled>4 · Start scrape</button>
 <button type="button" id="godry" disabled hidden>4 · Dry run (preview changes)</button>
 <button type="button" id="goapply" class="danger" disabled hidden>5 · Apply changes</button>
 <button type="button" id="pause" class="secondary" disabled>Pause</button>
 <button type="button" id="stop" class="secondary" disabled>Stop</button>
</div>

<div id="status" role="status" aria-live="polite">Idle — choose an operation and load a workbook.</div>
<progress id="prog" max="1" value="0" hidden aria-label="Job progress"></progress>
<!-- DC-LOG 2: the log bar, the inline Clear question, the box, and the
     whole-run fallback for a browser that will not write the clipboard.
     Shared verbatim; see the script's DC-LOG block. -->
<div class="logbar">
 <h2 style="font-size:1rem">Log</h2>
 <span id="logheld" class="lognote" hidden></span>
 <span id="logmsg" class="lognote" role="status" aria-live="polite"></span>
 <button type="button" id="logcopy" class="secondary">Copy status and log</button>
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
 aria-label="The status and the whole log of the current run, selected for copying"></textarea>
<!-- /DC-LOG -->
</main>
<script>
/* ---- Activity log rendering ---------------------------------------------
   Rewriting the whole log on every poll destroyed the reader's selection
   and snapped the scroll back to the tail. So: render only on change,
   append rather than replace, follow the tail only from the tail, and do
   not touch the DOM at all while a selection is live inside the box.
   Found by an operator trying to copy one line out of a running log.
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

/* DC-LOG 2: Copy status and log, Clear for a new run, and the one way a
   page writes its status line. Shared by every module with a log and
   duplicated verbatim; modules/_verify_pages.py fails if any copy
   differs. There is no backslash anywhere in it, because some pages are
   raw strings and some are not, and the same bytes must serve both.
   Copy status and log copies the status line, then the WHOLE current run
   from the module (/api/log), not whatever the box holds: the box keeps
   the last 200 lines and may still show the end of the previous run. A
   link in the status line that carries data-path is copied as its label
   and that path. Every page's poll() calls logStateSeen(s) with the state
   it just read, and writes #status only through logShow(). */
var LOG_NL = String.fromCharCode(10);
var logLastCleared = null;
var logPhase = '';
var logShown = null;
/* #status is a live region and every page polls every 1.5 s, so the line
   is written only when it changes: rewriting the same words can have them
   read out again each time. */
function logShow(cls, html){
  var st = document.getElementById('status');
  if(!st) return;
  if(st.className !== cls) st.className = cls;
  if(logShown !== html){ st.innerHTML = html; logShown = html; }
}
/* For a value going into an attribute, such as data-path. */
function logAttr(t){
  return String(t).split('&').join('&amp;').split('"').join('&quot;')
    .split('<').join('&lt;').split('>').join('&gt;');
}
/* The status line as text: a line break for each br, a list item on a
   line of its own, and a link as its label, then its path if it has one. */
function logStatusText(){
  var st = document.getElementById('status');
  if(!st) return '';
  var out = [];
  (function walk(n){
    var kids = n.childNodes || [];
    for(var i = 0; i < kids.length; i++){
      var c = kids[i];
      if(c.nodeType === 3){ out.push(c.nodeValue); continue; }
      if(c.nodeType !== 1) continue;
      var tag = String(c.nodeName).toUpperCase();
      if(tag === 'BR'){ out.push(LOG_NL); continue; }
      if(tag === 'A'){
        var p = c.getAttribute('data-path');
        out.push(c.textContent + (p ? ': ' + p : ''));
        continue;
      }
      if(tag === 'LI') out.push(LOG_NL + '- ');
      walk(c);
    }
  })(st);
  return out.join('').split(LOG_NL).map(function(l){ return l.trim(); })
    .join(LOG_NL).trim();
}
function logLines(txt){
  if(!txt) return 0;
  var t = txt.charAt(txt.length - 1) === LOG_NL ? txt.slice(0, -1) : txt;
  return t.split(LOG_NL).length;
}
function logOffer(txt, what){
  var ta = document.getElementById('logfull');
  if(!ta){ logSelectAll(); return; }
  ta.value = txt; ta.hidden = false; ta.focus(); ta.select();
  logSay(what + ' selected below. Press Ctrl+C (Cmd+C) to copy.');
}
/* The status line is copied with the log unless the page is idle, when
   the line is only an instruction, not a result. */
function copyLog(){
  fetch('/api/log', {cache:'no-store'})
    .then(function(r){ return r.ok ? r.text() : Promise.reject(r.status); })
    .then(function(txt){
      var st = logPhase === 'idle' ? '' : logStatusText();
      if(!txt && !st){ logSay('Nothing has been logged since the last clean start.'); return; }
      var n = logLines(txt);
      var lines = n + ' line' + (n === 1 ? '' : 's');
      var all, said, what;
      if(!txt){
        all = 'Status:' + LOG_NL + st + LOG_NL;
        said = 'The status line copied to the clipboard. Nothing has been logged since the last clean start.';
        what = 'The status line is';
      }else if(st){
        all = 'Status:' + LOG_NL + st + LOG_NL + LOG_NL + 'Log:' + LOG_NL + txt;
        said = 'The status and the whole current run, ' + lines + ', copied to the clipboard.';
        what = 'The status and the whole current run (' + lines + ') are';
      }else{
        all = txt;
        said = 'The whole current run, ' + lines + ', copied to the clipboard.';
        what = 'The whole current run (' + lines + ') is';
      }
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(all).then(
          function(){ logSay(said); }, function(){ logOffer(all, what); });
      }else{
        logOffer(all, what);
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
  logPhase = (s && s.phase) || '';
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
/* DC-TONE 1: the color of a finished run (1.0.1). Green (done): the state
   matches what the operator intended. Amber: it finished, with problems to
   look at. Red (error): it ended in error, or nothing it attempted
   succeeded. The module decides, in s.outcome; this only shows it. Shared
   by every module with a log and duplicated verbatim. */
function logTone(s){
  if(!s) return '';
  if(s.phase === 'error') return 'error';
  if(s.phase !== 'done') return '';
  if(s.outcome === 'amber') return 'amber';
  if(s.outcome === 'red') return 'error';
  return 'done';
}
/* /DC-TONE */
const FORM_IDS=__FORM_FIELDS__;
/* DC-FORM 1: the page's settings, remembered across launches (1.0.1).
   Every field named in FORM_IDS (the module's FORM_FIELDS, defined by the
   page before this block) is sent to the module as it changes, and put
   back when the page loads from a file the module keeps beside the suite's
   configuration. A choice whose option has not loaded yet is put back when
   it appears. Shared by every module that runs a job and duplicated
   verbatim. A page may define formParents() (its checked structures) and
   formRestoredHook(form) (what its own controls need afterwards). Every
   page's poll() calls formSeen(s) with the state it just read. */
var formRestored = false, formTimer = null, formPending = {};
function formState(){
  var fields = {};
  FORM_IDS.forEach(function(id){
    var el = document.getElementById(id); if(!el) return;
    fields[id] = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
  });
  return {fields: fields,
          parents: (typeof formParents === 'function') ? formParents() : []};
}
function saveForm(){
  if(!formRestored) return;      /* never overwrite what is about to load */
  clearTimeout(formTimer);
  formTimer = setTimeout(function(){
    fetch('/api/form', {method:'POST', cache:'no-store',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(formState())});
  }, 400);
}
function formHasOption(el, v){
  if(!el.options) return true;
  for(var i = 0; i < el.options.length; i++){
    if(el.options[i].value === v) return true;
  }
  return false;
}
function formFire(el){
  if(el.dispatchEvent && typeof Event === 'function') el.dispatchEvent(new Event('change'));
}
function restoreForm(form){
  var changed = [];
  if(form && form.fields){
    Object.keys(form.fields).forEach(function(id){
      if(FORM_IDS.indexOf(id) < 0) return;
      var el = document.getElementById(id); if(!el) return;
      var v = form.fields[id];
      if(el.type === 'checkbox' || el.type === 'radio'){ el.checked = !!v; }
      else if(formHasOption(el, String(v))){ el.value = String(v); }
      else { formPending[id] = String(v); return; }
      changed.push(el);
    });
  }
  formRestored = true;
  FORM_IDS.forEach(function(id){
    var el = document.getElementById(id); if(!el) return;
    el.addEventListener('change', saveForm);
    el.addEventListener('input', saveForm);
  });
  changed.forEach(function(el){ if(el.type !== 'radio' || el.checked) formFire(el); });
  if(typeof formRestoredHook === 'function') formRestoredHook(form);
}
function formRetryPending(){
  Object.keys(formPending).forEach(function(id){
    var el = document.getElementById(id);
    if(!el || !formHasOption(el, formPending[id])) return;
    el.value = formPending[id];
    delete formPending[id];
    formFire(el);
  });
}
function formSeen(s){
  if(!formRestored){ restoreForm(s && s.form); return; }
  formRetryPending();
}
/* /DC-FORM */
/* DC-BROWSE 1: Browse... beside every path field (1.0.4). A button marked
   data-browse="folder|file|files" and data-for="<field id>" asks the module
   to open this computer's own dialog; the choice fills the field (a path
   list gains the chosen files, skipping any already listed) and fires the
   field's input and change events, so the page saves and reacts as if the
   path had been typed. Buttons stay hidden when the computer has no dialog,
   and the first path field says why. Shared by every module with a path
   field and duplicated verbatim. */
var browseState = {mechanism: null, none: '', busy: false};
var BROWSE_NL = String.fromCharCode(10);
function browseJoin(existing, picked){
  var lines = String(existing || '').split(BROWSE_NL)
    .map(function(s){ return s.trim(); })
    .filter(function(s){ return s.length > 0; });
  (picked || []).forEach(function(p){ if(lines.indexOf(p) < 0) lines.push(p); });
  return lines.join(BROWSE_NL);
}
function browseStartFrom(kind, value){
  if(kind !== 'files') return String(value || '').trim();
  var lines = browseJoin(value, []).split(BROWSE_NL);
  return lines[lines.length - 1] || '';
}
function browseButtons(){
  if(typeof document === 'undefined' || !document.querySelectorAll) return [];
  return Array.prototype.slice.call(document.querySelectorAll('button[data-browse]'));
}
function browseField(btn){ return document.getElementById(btn.getAttribute('data-for')); }
function browseSay(btn, text, tone){
  var el = document.getElementById(btn.getAttribute('data-for') + '-browsemsg');
  if(!el) return;
  el.textContent = text || '';
  el.className = 'browse-msg' + (tone ? ' dc-' + tone : '');
}
function browseSync(){
  browseButtons().forEach(function(btn){
    var field = browseField(btn);
    btn.hidden = !browseState.mechanism;
    btn.disabled = browseState.busy || !field || !!field.disabled || !!field.readOnly;
  });
}
function browseFire(el, type){
  if(el.dispatchEvent && typeof Event === 'function') el.dispatchEvent(new Event(type));
}
function browseApply(btn, paths){
  var field = browseField(btn);
  if(!field || !paths || !paths.length) return 0;
  var before = field.value;
  if(btn.getAttribute('data-browse') === 'files'){
    field.value = browseJoin(field.value, paths);
  } else {
    field.value = paths[0];
  }
  browseFire(field, 'input');
  browseFire(field, 'change');
  if(field.focus) field.focus();
  return field.value === before ? 0 : 1;
}
function browseClick(btn){
  var field = browseField(btn);
  if(!field || field.disabled || field.readOnly || browseState.busy
     || !browseState.mechanism) return Promise.resolve(null);
  var kind = btn.getAttribute('data-browse');
  var label = btn.textContent;
  browseState.busy = true;
  browseSync();
  btn.textContent = 'Choosing…';
  browseSay(btn, 'A dialog is open. If you do not see it, it may be behind this browser window.', 'info');
  return fetch('/api/browse', {method:'POST', cache:'no-store',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind: kind, start: browseStartFrom(kind, field.value),
                            title: btn.getAttribute('data-title') || ''})})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(j && j.error){ browseSay(btn, j.error, 'bad'); }
      else if(!j || j.cancelled || !j.paths || !j.paths.length){
        browseSay(btn, 'Nothing was chosen; the field is unchanged.', 'info');
      } else if(kind === 'files'){
        var had = browseJoin(field.value, []).split(BROWSE_NL).filter(function(s){ return s; }).length;
        browseApply(btn, j.paths);
        var now = browseJoin(field.value, []).split(BROWSE_NL).filter(function(s){ return s; }).length;
        var added = now - had, skipped = j.paths.length - added;
        browseSay(btn, added + (added === 1 ? ' path added' : ' paths added')
          + (skipped ? '; ' + skipped + ' already listed' : '') + '.', 'ok');
      } else {
        browseApply(btn, j.paths);
        browseSay(btn, '', '');
      }
      return j;
    })
    .catch(function(){
      browseSay(btn, 'The module did not answer the Browse request. Is it still running?', 'bad');
      return null;
    })
    .then(function(j){
      browseState.busy = false;
      btn.textContent = label;
      browseSync();
      return j;
    });
}
function browseInit(){
  var buttons = browseButtons();
  if(!buttons.length) return;
  buttons.forEach(function(btn){
    btn.addEventListener('click', function(){ browseClick(btn); });
  });
  if(typeof setInterval === 'function') setInterval(browseSync, 500);
  browseSync();
  fetch('/api/browse', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      browseState.mechanism = (j && j.mechanism) || null;
      browseState.none = (j && j.none) || '';
      browseSync();
      if(!browseState.mechanism && browseState.none) browseSay(buttons[0], browseState.none, 'info');
    })
    .catch(function(){});
}
browseInit();
/* /DC-BROWSE */

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

const $=id=>document.getElementById(id);
const opscrape=$('opscrape'),opupdate=$('opupdate'),
      scrapeSec=$('scrapeSec'),updSec=$('updSec'),
      invdir=$('invdir'),invscan=$('invscan'),invsel=$('invsel'),invload=$('invload'),
      harvest=$('harvest'),tgtsum=$('tgtsum'),tgtlist=$('tgtlist'),
      repdir=$('repdir'),repscan=$('repscan'),repsel=$('repsel'),repload=$('repload'),
      repsum=$('repsum'),replist=$('replist'),regen=$('regen'),plansum=$('plansum'),
      outdir=$('outdir'),goscrape=$('goscrape'),godry=$('godry'),goapply=$('goapply'),
      pauseBtn=$('pause'),stopBtn=$('stop'),status=$('status'),prog=$('prog'),logBox=$('log');
let paused=false, tgtVersion=0, repVersion=0, planInfo=null,
    tgtTypes=[], repTypes=[], tgtChecked=new Set(), repChecked=new Set();

async function api(path,body){
  const r=await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  let j={}; try{ j=await r.json(); }catch(e){}
  return {ok:r.ok, j};
}
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
/* flash() = a contextual notice; flashErr() = a contextual error. Both go
   to the dismissible #cerr callout and stay until the user clears them.
   They must never be written to #status — the poller owns that element. */
function flash(msg){ showNote(msg); }
function flashErr(msg){ showErr(msg); }
const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
const cap=t=>t.charAt(0).toUpperCase()+t.slice(1)+'…';

opscrape.addEventListener('change',syncOp);
opupdate.addEventListener('change',syncOp);
function syncOp(){
  const upd=opupdate.checked;
  scrapeSec.hidden=upd; updSec.hidden=!upd;
  goscrape.hidden=upd; godry.hidden=!upd; goapply.hidden=!upd;
}

// The workbook each side has loaded, by path, from the polled state, so a
// Scan keeps it selected: the list is newest first, and a list showing one
// workbook while another is loaded invites loading the wrong one.
const loadedPath={inventory:'',report:''};
function noteLoaded(s){
  loadedPath.inventory=(s.targets&&s.targets.path)||'';
  loadedPath.report=(s.report&&s.report.path)||'';
}
async function doScan(kind,folder,sel,loadBtn,noun){
  const {ok,j}=await api('/api/scan',{kind:kind,folder:folder});
  if(!ok){flashErr(j.error||'Scan failed.');return;}
  sel.innerHTML='';
  if(!j.files.length){
    sel.append(new Option('— no '+noun+' found —',''));
    sel.disabled=true; loadBtn.disabled=true;
    flashErr('No '+(j.pattern||'matching')+' workbooks in that folder.'+
      (kind==='inventory'?' To load a config report, choose Update configurations first.':''));
    return;
  }
  j.files.forEach(f=>sel.append(new Option(f.name+'  ('+f.modified+')',f.path)));
  const keep=loadedPath[kind];
  if(keep&&j.files.some(f=>f.path===keep)){sel.value=keep;}
  sel.disabled=false; loadBtn.disabled=false;
  flash(j.files.length+' workbook(s) found — pick one and click Load.');
}
invscan.addEventListener('click',()=>doScan('inventory',invdir.value.trim(),invsel,invload,'inventories'));
repscan.addEventListener('click',()=>doScan('report',repdir.value.trim(),repsel,repload,'config reports'));

async function doLoad(kind,sel){
  if(!sel.value){flashErr('Pick a workbook first.');return;}
  const {ok,j}=await api('/api/load',{kind:kind,path:sel.value});
  if(!ok){flashErr(j.error||'Could not load that workbook.');return;}
  poll();
}
invload.addEventListener('click',()=>doLoad('inventory',invsel));
repload.addEventListener('click',()=>doLoad('report',repsel));

harvest.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  const {ok,j}=await api('/api/harvest',{});
  if(!ok){flashErr(j.error||'Could not start the harvest.');return;}
  poll();
});

function renderTypes(list,types,checked){
  list.innerHTML='';
  types.forEach(t=>{
    const item=document.createElement('div'); item.className='item';
    const cb=document.createElement('input');
    cb.type='checkbox'; cb.id=list.id+'_'+t.label; cb.checked=checked.has(t.label);
    cb.addEventListener('change',()=>{
      cb.checked?checked.add(t.label):checked.delete(t.label);});
    const lab=document.createElement('label');
    lab.htmlFor=cb.id; lab.style.margin='0'; lab.textContent=t.label;
    const meta=document.createElement('span'); meta.className='meta';
    meta.textContent=t.count+' structure(s)';
    item.append(cb,lab,meta); list.append(item);
  });
  if(!types.length){
    const p=document.createElement('div'); p.className='item';
    p.textContent='Load a workbook (or harvest) first.';
    list.append(p);
  }
}

async function refreshTargets(){
  const j=await (await fetch('/api/targets')).json();
  tgtTypes=j.scrape.types||[]; repTypes=j.update.types||[];
  tgtChecked=new Set(tgtTypes.map(t=>t.label));   // default: everything
  repChecked=new Set(repTypes.map(t=>t.label));
  renderTypes(tgtlist,tgtTypes,tgtChecked);
  renderTypes(replist,repTypes,repChecked);
}

goscrape.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  if(!tgtChecked.size){flashErr('Check at least one structure type.');return;}
  goscrape.disabled=true;
  const {ok,j}=await api('/api/start',{action:'scrape',
    out_dir:outdir.value.trim(), types:[...tgtChecked]});
  if(!ok){flashErr(j.error||'Could not start.');goscrape.disabled=false;return;}
  poll();
});

godry.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  if(!repChecked.size){flashErr('Check at least one type sheet.');return;}
  godry.disabled=true;
  const {ok,j}=await api('/api/start',{action:'dryrun',
    out_dir:outdir.value.trim(), types:[...repChecked]});
  if(!ok){flashErr(j.error||'Could not start.');godry.disabled=false;return;}
  poll();
});

goapply.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  if(!planInfo||!planInfo.ready)return;
  if(!confirm('Apply '+planInfo.changes+' change(s) to '+planInfo.structures+
      ' structure(s) on the live site? This writes to Digital Commons.'))return;
  goapply.disabled=true;
  const {ok,j}=await api('/api/start',{action:'apply',
    out_dir:outdir.value.trim(), regen:regen.checked});
  if(!ok){flashErr(j.error||'Could not start.');goapply.disabled=false;return;}
  poll();
});

pauseBtn.addEventListener('click', async ()=>{
  const r=await api('/api/pause',{paused:!paused});
  if(r.ok) poll();
});
stopBtn.addEventListener('click', async ()=>{
  if(!confirm('Stop the job? A partial workbook will still be written.'))return;
  const r=await api('/api/stop');
  if(r.ok) poll();
});

async function poll(){
  const s=await (await fetch('/api/state')).json();
  renderLog(s.log); formSeen(s); logStateSeen(s); noteLoaded(s);
  paused=!!s.paused;
  pauseBtn.textContent=paused?'Resume':'Pause';
  const running=['starting','harvesting','scraping','checking','updating','writing'].includes(s.phase);

  const v=(s.targets?s.targets.version:0)+'|'+(s.report?s.report.version:0);
  if(v!==tgtVersion+'|'+repVersion){
    tgtVersion=s.targets.version; repVersion=s.report.version;
    await refreshTargets();
  }
  if(s.targets.loaded){
    tgtsum.className='sum loaded';
    tgtsum.textContent=s.targets.count+(s.targets.count===1?' structure · ':' structures · ')+s.targets.source;
  }
  if(s.report.loaded){
    repsum.className='sum loaded';
    repsum.textContent=s.report.count+(s.report.count===1?' structure · ':' structures · ')+s.report.source;
  }
  planInfo=s.plan;
  if(s.plan.ready){
    plansum.className=s.plan.changes?'sum warn':'sum loaded';
    plansum.textContent='Dry run: '+s.plan.changes+' change(s) across '+
      s.plan.structures+' structure(s)'+
      (s.plan.skipped?' · '+s.plan.skipped+' sheet field(s) skipped':'')+
      (s.plan.changes?' — review the preview, then Apply.':' — nothing to apply.');
  }else{
    plansum.className='sum';
    plansum.textContent='No dry run yet — Apply stays locked until a dry run completes.';
  }

  goscrape.disabled=running||!s.targets.loaded;
  godry.disabled=running||!s.report.loaded;
  goapply.disabled=running||!s.plan.ready||!s.plan.changes;
  harvest.disabled=running;
  invscan.disabled=running; repscan.disabled=running;
  invload.disabled=running||!invsel.value;
  repload.disabled=running||!repsel.value;
  pauseBtn.disabled=!running; stopBtn.disabled=!running;

  const p=s.progress||{};
  if(running&&p.total>0){prog.hidden=false;prog.max=p.total;prog.value=p.current;}
  else prog.hidden=true;
  const counts=p.total>0?' ('+p.current+'/'+p.total+')':'';
  const msg=p.msg?' — '+p.msg:'';
  let links='';
  if(s.last_file) links+='<br><a href="/download"'+(s.last_file?' data-path="'+logAttr(s.last_file)+'"':'')+'>Download workbook</a>';
  if(s.plan_file) links+='<br><a href="/download-plan"'+(s.plan_file?' data-path="'+logAttr(s.plan_file)+'"':'')+'>Download dry-run preview</a>';
  if(s.phase==='done'){
    logShow(logTone(s),'Done: '+esc(s.summary)+links);
  }else if(s.phase==='error'){
    logShow('error','Error: '+esc(s.summary)+links);
  }else if(s.phase==='idle'){
    logShow('',esc(s.summary?'Idle — '+s.summary
                   :'Idle — choose an operation and load a workbook.'));
  }else{
    logShow('',esc((paused?'Paused':cap(s.phase))+counts+msg));
  }
}
syncOp(); refreshTargets(); poll(); setInterval(poll,1500);
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
    page = PAGE
    for token, value in {
        "__FORM_FIELDS__": json.dumps(list(FORM_FIELDS)),
        "__NAME__": MANIFEST["name"],
        "__SUITE__": brand.get("suite_name", "DC Admin Suite"),
        "__INSTITUTION__": brand.get("institution", ""),
        "__PRIMARY__": primary,
        "__ACCENT__": colors.get("accent", FALLBACK_BRAND["accent"]),
        "__ONPRIMARY__": contrast_text(primary),
        "__HUB__": session.get("hub_url", "#"),
        "__BASE_URL__": session.get("base_url", ""),
    }.items():
        page = page.replace(token, value)
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

SCAN_GLOBS = {"inventory": INVENTORY_GLOB, "report": REPORT_GLOB}


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "")


# ---- DC-FOLDER 1 -----------------------------------------------------------
# Folder fields carry no default (1.0.1). The pages used to fill every folder
# field with ~/Desktop, a guess that goes wrong on a Windows computer whose
# Desktop is redirected to OneDrive: a report lands in a folder the operator
# does not see as the Desktop. Every folder a request names is judged here,
# and the message names the field the way the page labels it. Duplicated
# verbatim into every module with a folder field (modules are standalone);
# _verify_pages.py checks that the copies agree and drives each one.
def folder_error(label, path, optional=False):
    """"" when `path` names an existing folder, or is blank and the field is
    optional. Otherwise a message that names the field."""
    if not path:
        if optional:
            return ""
        return ("The \u201c{}\u201d field is empty — choose a folder "
                "(it must exist already).".format(label))
    if not os.path.isdir(path):
        return ("The folder in \u201c{}\u201d does not exist: {} — "
                "create it first, or choose another.".format(label, path))
    return ""
# ---- /DC-FOLDER ------------------------------------------------------------


# ---- DC-BROWSE 1 ----------------------------------------------------------
# A Browse... button beside every path field (1.0.4). The page asks this
# module, and the module opens the operating system's own dialog on this
# computer: osascript on macOS, PowerShell on Windows, tkinter otherwise.
# On Windows the dialog is the shell's own file dialog (IFileOpenDialog,
# which also picks folders), owned by an invisible always-on-top window
# that is shown first: Windows will not bring a background process's
# window to the front, but a dialog owned by a shown topmost window stays
# above ordinary windows such as the browser.
# The text field stays; the button only fills it. The start folder and the
# title travel as arguments or environment variables, never inside the
# dialog's script text. One dialog at a time per module. Duplicated
# verbatim into every module with a path field (modules are standalone);
# _verify_pages.py checks that the copies agree and drives each one with
# the dialog faked.
import base64 as _browse_b64
import shutil as _browse_shutil
import subprocess as _browse_subprocess

BROWSE_KINDS = ("folder", "file", "files")
BROWSE_TIMEOUT = 600          # seconds a dialog may stay open
BROWSE_CANCELLED = 3          # exit status the PowerShell and tkinter scripts use
BROWSE_NONE = ("No file dialog is available on this computer (osascript, "
               "PowerShell and tkinter were each looked for). Type the path "
               "into the field instead.")
BROWSE_BUSY = ("A dialog is already open for this module. Choose or cancel "
               "there first; it may be behind the browser window.")
_BROWSE = {"mechanism": None}
_BROWSE_LOCK = threading.Lock()
_BROWSE_OPEN = threading.Lock()

_BROWSE_OSA = """on run argv
set startPath to item 1 of argv
set promptText to item 2 of argv
set mode to item 3 of argv
activate
try
set loc to (POSIX file startPath) as alias
on error
set loc to path to home folder
end try
if mode is "folder" then
return POSIX path of (choose folder with prompt promptText default location loc)
else if mode is "file" then
return POSIX path of (choose file with prompt promptText default location loc)
else
set picked to choose file with prompt promptText default location loc with multiple selections allowed
set out to ""
repeat with f in picked
set out to out & (POSIX path of f) & linefeed
end repeat
return out
end if
end run
"""

_BROWSE_PS = """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
# COMPILE-START
Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DcPicker {
  [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
  class FileOpenDialogRCW {}
  [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IFileOpenDialog {
    [PreserveSig] int Show(IntPtr owner);
    void SetFileTypes(uint count, IntPtr types);
    void SetFileTypeIndex(uint index);
    void GetFileTypeIndex(out uint index);
    void Advise(IntPtr events, out uint cookie);
    void Unadvise(uint cookie);
    void SetOptions(uint fos);
    void GetOptions(out uint fos);
    void SetDefaultFolder(IShellItem item);
    void SetFolder(IShellItem item);
    void GetFolder(out IShellItem item);
    void GetCurrentSelection(out IShellItem item);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetResult(out IShellItem item);
    void AddPlace(IShellItem item, int place);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string ext);
    void Close(int hr);
    void SetClientGuid(ref Guid guid);
    void ClearClientData();
    void SetFilter(IntPtr filter);
    void GetResults(out IShellItemArray items);
    void GetSelectedItems(out IShellItemArray items);
  }
  [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem item);
    void GetDisplayName(uint sigdn, out IntPtr name);
    void GetAttributes(uint mask, out uint attrs);
    void Compare(IShellItem item, uint hint, out int order);
  }
  [ComImport, Guid("B63EA76D-1F85-456F-A19C-48159EFA858B"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItemArray {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetPropertyStore(int flags, ref Guid riid, out IntPtr ppv);
    void GetPropertyDescriptionList(IntPtr key, ref Guid riid, out IntPtr ppv);
    void GetAttributes(int flags, uint mask, out uint attrs);
    void GetCount(out uint count);
    void GetItemAt(uint index, out IShellItem item);
    void EnumItems(out IntPtr items);
  }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName(string path, IntPtr pbc,
      ref Guid riid, out IShellItem item);
  const uint SIGDN_FILESYSPATH = 0x80058000;
  const int CANCELLED = unchecked((int)0x800704C7);
  static string PathOf(IShellItem item) {
    IntPtr p;
    item.GetDisplayName(SIGDN_FILESYSPATH, out p);
    try { return Marshal.PtrToStringUni(p); } finally { Marshal.FreeCoTaskMem(p); }
  }
  public static string[] Pick(IntPtr owner, string kind, string start, string title) {
    IFileOpenDialog d = (IFileOpenDialog)new FileOpenDialogRCW();
    uint fos;
    d.GetOptions(out fos);
    fos |= 0x40 | 0x800;              /* file system only; the path must exist */
    if (kind == "folder") fos |= 0x20; /* pick folders */
    else fos |= 0x1000;                /* the file must exist */
    if (kind == "files") fos |= 0x200; /* several at once */
    d.SetOptions(fos);
    if (!String.IsNullOrEmpty(title)) d.SetTitle(title);
    if (!String.IsNullOrEmpty(start)) {
      try {
        Guid iid = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");
        IShellItem folder;
        SHCreateItemFromParsingName(start, IntPtr.Zero, ref iid, out folder);
        d.SetFolder(folder);
      } catch (Exception) { }
    }
    int hr = d.Show(owner);
    if (hr == CANCELLED) return null;
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
    if (kind == "files") {
      IShellItemArray items;
      d.GetResults(out items);
      uint n;
      items.GetCount(out n);
      string[] paths = new string[n];
      for (uint i = 0; i < n; i++) {
        IShellItem item;
        items.GetItemAt(i, out item);
        paths[i] = PathOf(item);
      }
      return paths;
    }
    IShellItem one;
    d.GetResult(out one);
    return new string[] { PathOf(one) };
  }
}
'@
# COMPILE-END
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show()
$owner.Activate()
try {
  $got = [DcPicker]::Pick($owner.Handle, $env:DC_BROWSE_KIND,
                          $env:DC_BROWSE_START, $env:DC_BROWSE_TITLE)
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
} finally {
  $owner.Close()
  $owner.Dispose()
}
if ($null -eq $got -or $got.Count -eq 0) { exit 3 }
[Console]::Out.Write(($got -join [Environment]::NewLine))
"""

_BROWSE_TK = """import os, sys
import tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes('-topmost', True)
kind = os.environ.get('DC_BROWSE_KIND', 'folder')
opts = dict(parent=root, title=os.environ.get('DC_BROWSE_TITLE', ''),
            initialdir=os.environ.get('DC_BROWSE_START', ''))
if kind == 'folder':
    got = filedialog.askdirectory(mustexist=True, **opts)
    got = [got] if got else []
elif kind == 'file':
    got = filedialog.askopenfilename(**opts)
    got = [got] if got else []
else:
    got = list(filedialog.askopenfilenames(**opts))
root.destroy()
if not got:
    sys.exit(3)
sys.stdout.buffer.write(chr(10).join(got).encode('utf-8'))
"""


def browse_kind_error(kind):
    """"" for a kind the dialogs know; otherwise a message (contract 11:
    the one validator both endpoints use)."""
    if kind in BROWSE_KINDS:
        return ""
    return "Unknown Browse request {!r}: expected one of {}.".format(
        kind, ", ".join(BROWSE_KINDS))


def start_folder(value):
    """Where a dialog opens: the field's folder if it exists; for a file or
    a path that does not exist yet, its nearest existing parent; otherwise
    the home folder. A relative path is not guessed at."""
    home = os.path.expanduser("~")
    path = os.path.expanduser((value or "").strip())
    if not path or not os.path.isabs(path):
        return home
    if os.path.isdir(path):
        return path
    cur = os.path.dirname(path)
    while cur and not os.path.isdir(cur):
        up = os.path.dirname(cur)
        if up == cur:
            break
        cur = up
    return cur if cur and os.path.isdir(cur) else home


def _browse_tk_works():
    try:
        done = _browse_subprocess.run(
            [sys.executable, "-c",
             "import tkinter; r = tkinter.Tk(); r.withdraw(); r.destroy()"],
            capture_output=True, timeout=20)
    except (OSError, _browse_subprocess.SubprocessError):
        return False
    return done.returncode == 0


def browse_mechanism():
    """The dialog this computer can show: "osascript", "powershell",
    "tkinter", or "" for none. Looked for once, on first use."""
    with _BROWSE_LOCK:
        if _BROWSE["mechanism"] is None:
            if sys.platform == "darwin" and _browse_shutil.which("osascript"):
                found = "osascript"
            elif os.name == "nt" and (_browse_shutil.which("powershell")
                                      or _browse_shutil.which("pwsh")):
                found = "powershell"
            elif _browse_tk_works():
                found = "tkinter"
            else:
                found = ""
            _BROWSE["mechanism"] = found
        return _BROWSE["mechanism"]


def browse_command(mechanism, kind, start, title):
    """(argv, extra environment, stdin bytes) for one dialog. The start
    folder and title are arguments or environment, never script text."""
    env = {"DC_BROWSE_KIND": kind, "DC_BROWSE_START": start,
           "DC_BROWSE_TITLE": title}
    if mechanism == "osascript":
        return (["osascript", "-", start, title, kind], {},
                _BROWSE_OSA.encode("utf-8"))
    if mechanism == "powershell":
        exe = _browse_shutil.which("powershell") or _browse_shutil.which(
            "pwsh") or "powershell"
        code = _browse_b64.b64encode(_BROWSE_PS.encode("utf-16-le")).decode(
            "ascii")
        return ([exe, "-NoProfile", "-NonInteractive", "-STA",
                 "-EncodedCommand", code], env, None)
    return ([sys.executable, "-c", _BROWSE_TK], env, None)


def _browse_clean(path, mechanism):
    path = path.strip()
    if mechanism == "osascript" and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


def pick_paths(mechanism, kind, start, title):
    """Open one dialog. {"paths": [...]}, {"cancelled": True}, or
    {"error": message}. Cancel is not an error."""
    argv, extra, stdin = browse_command(mechanism, kind, start, title)
    env = dict(os.environ)
    env.update(extra)
    try:
        done = _browse_subprocess.run(argv, input=stdin, capture_output=True,
                                      timeout=BROWSE_TIMEOUT, env=env)
    except _browse_subprocess.TimeoutExpired:
        return {"error": "Nothing was chosen within {} minutes, so the "
                         "dialog was closed. The field is unchanged."
                         .format(BROWSE_TIMEOUT // 60)}
    except OSError as e:
        return {"error": "The {} dialog could not be started: {}"
                         .format(mechanism, e)}
    said = done.stderr.decode("utf-8", "replace").strip()
    if done.returncode == BROWSE_CANCELLED or (
            mechanism == "osascript" and "-128" in said):
        return {"cancelled": True}
    if done.returncode != 0:
        last = said.splitlines()[-1] if said else "no message"
        return {"error": "The {} dialog ended with status {}: {}".format(
            mechanism, done.returncode, last[:300])}
    out = done.stdout.decode("utf-8", "replace")
    paths = [_browse_clean(p, mechanism) for p in out.splitlines()]
    paths = [p for p in paths if p]
    if not paths:
        return {"cancelled": True}
    if kind != "files":
        paths = paths[:1]
    return {"paths": paths}


def browse_status():
    """GET /api/browse: which dialog this computer offers ("" = none)."""
    mech = browse_mechanism()
    return {"mechanism": mech, "none": "" if mech else BROWSE_NONE}


def browse_request(handler):
    """POST /api/browse {kind, start, title}. Returns (payload, code)."""
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        if length > 65536:
            raise ValueError("request too large")
        body = json.loads(handler.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
    except ValueError as e:
        return {"error": "Unreadable Browse request: {}".format(e)}, 400
    kind = body.get("kind")
    why = browse_kind_error(kind)
    if why:
        return {"error": why}, 400
    mech = browse_mechanism()
    if not mech:
        return {"error": BROWSE_NONE}, 409
    if not _BROWSE_OPEN.acquire(blocking=False):
        return {"error": BROWSE_BUSY}, 409
    try:
        title = str(body.get("title") or "Choose")[:200]
        start = start_folder(str(body.get("start") or ""))
        return pick_paths(mech, kind, start, title), 200
    finally:
        _BROWSE_OPEN.release()
# ---- /DC-BROWSE ------------------------------------------------------------


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

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def _running(self):
            with LOCK:
                return STATE["phase"] in RUNNING_PHASES

        def _send_file(self, path):
            if not path or not os.path.isfile(path):
                return self._json({"error": "No file available."}, 404)
            data = Path(path).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", XLSX_MIME)
            self.send_header("Content-Disposition",
                             'attachment; filename="{}"'
                             .format(os.path.basename(path)))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not request_allowed(self):
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/browse":
                return self._json(browse_status())
            if self.path == "/api/log":
                return send_run_log(self)
            if self.path == "/api/state":
                form_for_page()
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/api/targets":
                with LOCK:
                    smodel, umodel = SCRAPE_MODEL, UPD_MODEL
                out = {"scrape": {"types": []}, "update": {"types": []}}
                if smodel:
                    out["scrape"]["types"] = [
                        {"label": label, "count": len(ctxs)}
                        for label, ctxs in sorted(smodel["types"].items())]
                if umodel:
                    out["update"]["types"] = [
                        {"label": label, "count": len(ctxs)}
                        for label, ctxs in sorted(umodel["types"].items())]
                return self._json(out)
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
                return self._send_file(path)
            if self.path == "/download-plan":
                with LOCK:
                    path = STATE["plan_file"]
                return self._send_file(path)
            # default: the app page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if not request_allowed(self):
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/browse":
                payload, code = browse_request(self)
                return self._json(payload, code)
            if self.path == "/api/form":
                payload, code = remember_form_request(self)
                return self._json(payload, code)
            if self.path == "/api/clear":
                with LOCK:
                    busy = STATE["phase"] in RUNNING_PHASES
                if busy:
                    return self._json({"error": CLEAR_REFUSED}, 409)
                clear_for_new_run()
                return self._json({"ok": True})
            if self.path == "/api/scan":
                try:
                    req = self._body()
                    kind = str(req["kind"])
                    folder = os.path.expanduser(str(req["folder"]).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if kind not in SCAN_GLOBS:
                    return self._json({"error": "Bad request."}, 400)
                err_ = folder_error("Workbooks folder", folder)
                if err_:
                    return self._json({"error": err_}, 400)
                return self._json(
                    {"files": scan_workbooks(folder, SCAN_GLOBS[kind]),
                     "pattern": SCAN_GLOBS[kind]})

            if self.path == "/api/load":
                if self._running():
                    return self._json({"error": "A job is running."}, 409)
                try:
                    req = self._body()
                    kind = str(req["kind"])
                    path = os.path.expanduser(str(req["path"]).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if kind not in SCAN_GLOBS:
                    return self._json({"error": "Bad request."}, 400)
                if not path or not os.path.isfile(path):
                    return self._json(
                        {"error": "File does not exist: " + path}, 400)
                try:
                    if kind == "inventory":
                        types = parse_inventory(path)
                        set_scrape_model(
                            {"types": types, "path": path,
                             "source": "loaded from " + os.path.basename(path)})
                    else:
                        types, rows, fields = parse_config_report(path)
                        set_upd_model(
                            {"types": types, "rows": rows, "fields": fields,
                             "path": path,
                             "source": "loaded from " + os.path.basename(path)})
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                log("Loaded {} workbook: {}".format(kind, path))
                return self._json({"ok": True})

            if self.path == "/api/harvest":
                if self._running():
                    return self._json({"error": "A job is already running."},
                                      409)
                if not start_run(harvest_worker, (session,),
                                 events=(STOP_EVENT, PAUSE_EVENT)):
                    return self._json({"error": "A job is already running."}, 409)
                return self._json({"ok": True})

            if self.path == "/api/start":
                if self._running():
                    return self._json({"error": "A job is already running."},
                                      409)
                try:
                    req = self._body()
                    action = str(req["action"])
                    out_dir = os.path.expanduser(str(req["out_dir"]).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if action not in ("scrape", "dryrun", "apply"):
                    return self._json({"error": "Bad request."}, 400)
                err_ = folder_error("Output folder", out_dir)
                if err_:
                    return self._json({"error": err_}, 400)
                if action == "scrape":
                    labels = [str(t) for t in req.get("types", [])]
                    with LOCK:
                        loaded = SCRAPE_MODEL is not None
                    if not loaded:
                        return self._json(
                            {"error": "Load an inventory or harvest the "
                                      "structure list first."}, 400)
                    if not labels:
                        return self._json(
                            {"error": "Check at least one structure type."},
                            400)
                    worker, args = scrape_worker, (session, out_dir, labels)
                elif action == "dryrun":
                    labels = [str(t) for t in req.get("types", [])]
                    with LOCK:
                        loaded = UPD_MODEL is not None
                    if not loaded:
                        return self._json(
                            {"error": "Load a config report first."}, 400)
                    if not labels:
                        return self._json(
                            {"error": "Check at least one type sheet."}, 400)
                    worker, args = dryrun_worker, (session, out_dir, labels)
                else:  # apply
                    regen = bool(req.get("regen", True))
                    with LOCK:
                        plan_ok = (PLAN is not None
                                   and PLAN["report_version"]
                                   == STATE["report"]["version"])
                    if not plan_ok:
                        return self._json(
                            {"error": "Run a dry run first — Apply only "
                                      "executes a current dry-run plan."},
                            400)
                    worker, args = apply_worker, (session, out_dir, regen)

                if not start_run(worker, args,
                                 events=(STOP_EVENT, PAUSE_EVENT)):
                    return self._json({"error": "A job is already running."}, 409)
                return self._json({"ok": True})

            if self.path == "/api/pause":
                if not self._running():
                    return self._json({"error": "No job is running."}, 409)
                try:
                    req = self._body()
                    want_pause = bool(req.get("paused",
                                              not PAUSE_EVENT.is_set()))
                except (ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if want_pause:
                    PAUSE_EVENT.set()
                    log("Job paused.")
                else:
                    PAUSE_EVENT.clear()
                    log("Job resumed.")
                set_state(paused=want_pause)
                return self._json({"ok": True, "paused": want_pause})

            if self.path == "/api/stop":
                if not self._running():
                    return self._json({"error": "No job is running."}, 409)
                STOP_EVENT.set()
                PAUSE_EVENT.clear()
                set_state(paused=False)
                log("Stop requested by user.")
                return self._json({"ok": True})

            return self._json({"error": "Not found."}, 404)

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


