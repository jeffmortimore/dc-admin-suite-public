#!/usr/bin/env python3
"""Page Number Generator v1.0 — DC Admin Suite module.

Works out the first and last page of each article in a journal issue, the
way staff have done it by hand: put the issue's PDFs in order, leave out the
cover page Digital Commons adds, number the first page of the first article
1 (or carry on from the previous issue) and count on to the last page of the
last article.

  1. Choose journal, volume(s), issue(s) — the DOI & XML Generator's choice
     (the shared DC-ISSUE block) and its journal registry,
     config/journals.json, which says for each journal whether numbering
     restarts each issue or continues through the volume, and how many
     cover pages its published copies carry. Both are defaults the operator
     can change for each issue.
  2. List contents — the instance's public OAI-PMH feed (document-export),
     read over urllib, one list per issue. The feed sends records in no
     particular order; each carries its label (its number within the
     issue), which is the order offered to start with.
  3. Select and order — the operator ticks the items that are paginated and
     puts them in order with Up, Down and Move-to-position (all keyboard
     operable). Nothing is assumed from an item's document type.
  4. Get the PDFs — the module writes a workbook the Batch File Downloader
     re-runs as a download job: for each ticked item, the stamped copy the
     public sees and the unstamped file as uploaded, and nothing else. This
     module never attaches to Chrome; every file request goes through the
     downloader's monitored browser route.
  5. Count and number — pages are counted with pikepdf. The published copy
     should be the cover page(s) plus the uploaded file, so for each item
     the stamped pages minus the unstamped pages must equal the issue's
     cover-page count. An item that disagrees, or that cannot be checked,
     is flagged and not numbered, and neither is anything after it in the
     issue: a page number after a gap would be a guess. An item with no PDF
     can be given its page count by hand.
  6. Report — DC_PageNumbers_<stamp>.xlsx (written as _PARTIAL while the
     run is under way): Pages, one row per item; Summary, one row per
     issue; Checks, what was verified and what was not.

Items are matched to downloaded files through the downloader's reports, by
structure and article ID — never by title or file name. Writing the page
numbers back into the records is not this module's work.

Run standalone:  python3 modules/dc_page_numbers.py --port 8800 \\
                     --session config/session.json
Or launch it from the Main Menu.
"""

# ---------------------------------------------------------------------------
# MANIFEST — read by the shell with `ast`; keep it a dict of literals.
# ---------------------------------------------------------------------------
MANIFEST = {
    "id": "page-number-generator",
    "name": "Page Number Generator",
    "description": "Work out first and last page numbers for the articles of journal issues from their published PDFs, in the order staff choose.",
    "version": "1.0.1",
    "requires": ["openpyxl", "pikepdf"],
}

import argparse
import collections
import glob
import html as html_lib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------
DEFAULT_SESSION = Path(__file__).resolve().parent.parent / "config" / "session.json"

# Neutral fallbacks, used ONLY when session.json is missing or unreadable.
# Duplicated per module; the reference copy is in _template_module.py.
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
                         "colors": {"primary": FALLBACK_BRAND["primary"],
                                    "accent": FALLBACK_BRAND["accent"],
                                    "neutral": FALLBACK_BRAND["neutral"]}},
        }


# ---- DC-ISSUE 1 ------------------------------------------------------------
# The journal / volume / issue choice and the journal registry's
# page-numbering fields, shared by the DOI & XML Generator and the Page
# Number Generator: both read config/journals.json, which the DOI & XML
# Generator edits. Duplicated verbatim; modules/_verify_pages.py checks that
# every module reading the registry carries this block, and the page's half
# (the DC-ISSUE script block), and that the copies agree.
#
#   page_numbering  "restart" (each issue starts at its first page number)
#                   or "continue" (an issue goes on from the previous issue
#                   of its volume). A default: the operator can change it
#                   for each issue when setting up a run.
#   cover_pages     how many cover pages Digital Commons adds to the
#                   published copy of an article (normally 1). The Page
#                   Number Generator checks each article against it.
ISSUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ISSUE_YEAR_RE = re.compile(r"^\d{4}$")
PAGINATION_MODES = ("restart", "continue")
COVER_PAGES_MAX = 9


def backfill_pagination(j):
    """Give a registry entry the page-numbering fields it may predate.
    Mutates and returns j."""
    j.setdefault("page_numbering", "restart")
    j.setdefault("cover_pages", 1)
    return j


def pagination_error(j, jid):
    """"" when an entry's page-numbering fields are usable, else a message."""
    if str(j.get("page_numbering", "restart")) not in PAGINATION_MODES:
        return ("Journal '{}': page numbering must be restart or continue."
                .format(jid))
    cover = j.get("cover_pages", 1)
    if (isinstance(cover, bool) or not isinstance(cover, int)
            or not 0 <= cover <= COVER_PAGES_MAX):
        return ("Journal '{}': cover pages must be a whole number from 0 to "
                "{}.".format(jid, COVER_PAGES_MAX))
    return ""


def issue_ref(s, entries, i):
    """One volume / issue row as the page sent it, checked.

    Returns (ref, "") or (None, a message naming the row). ref holds the
    journal ID, volume, issue and series year, and the registry entry (None
    for a structure ID the registry does not carry)."""
    if not isinstance(s, dict):
        return None, "Bad volume / issue #{}.".format(i)
    jid = str(s.get("journal", "")).strip().lower()
    if not ISSUE_ID_RE.match(jid):
        return None, ("Volume / issue #{}: enter a journal structure ID."
                      .format(i))
    entry = entries.get(jid)
    vol = str(s.get("volume", "")).strip()
    iss = str(s.get("issue", "")).strip()
    if (vol and not vol.isdigit()) or (iss and not iss.isdigit()):
        return None, ("Volume / issue #{}: volume and issue must be "
                      "numbers.".format(i))
    syear = str(s.get("syear", "")).strip()
    if bool(entry) and entry.get("structure") == "yearly":
        if not ISSUE_YEAR_RE.match(syear):
            return None, ("Volume / issue #{}: '{}' is a yearly series — "
                          "enter its 4-digit year (the harvest targets "
                          "{}_YYYY).".format(i, jid, jid))
        iss = ""  # per-year series have no issues
    else:
        syear = ""
    return {"journal": jid, "volume": vol, "issue": iss, "syear": syear,
            "entry": entry}, ""


def oai_url(base_url, jid, vol, iss, year=""):
    """OAI ListRecords URL for one set. Volume/issue publications use
    publication:jid/volN/issN; yearly-series publications (an ir_community
    with one ir_series per year, e.g. amtp-proceedings_2026 or pmhr_2025)
    are separate per-year publications, addressed as publication:jid_YYYY."""
    if year:
        setspec = "publication:{}_{}".format(jid, year)
    else:
        setspec = "publication:" + jid
        if vol:
            setspec += "/vol" + str(int(vol))
        if iss:
            setspec += "/iss" + str(int(iss))
    return ("{}/do/oai/?verb=ListRecords&metadataPrefix=document-export"
            "&set={}".format(base_url, setspec))
# ---- /DC-ISSUE -------------------------------------------------------------


# ---------------------------------------------------------------------------
# The journal registry, read only. The DOI & XML Generator owns
# config/journals.json and edits it; this module only reads the journals and
# their page-numbering defaults.
# ---------------------------------------------------------------------------
REGISTRY_PATH = None  # set in main() from the session path


def load_journals() -> dict:
    """The registry's journals as this module needs them, and a note.

    Never writes the file. A registry that is missing or unreadable gives
    no journals and says so; a custom structure ID still works, with the
    defaults (restart each issue, one cover page)."""
    note = ""
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        raw = reg.get("journals", []) if isinstance(reg, dict) else []
        if not isinstance(raw, list):
            raw = []
    except FileNotFoundError:
        raw = []
        note = ("There is no journal registry yet (config/journals.json). "
                "Journals are added in the DOI & XML Generator; a custom "
                "structure ID works here meanwhile.")
    except (OSError, ValueError, AttributeError) as e:
        raw = []
        note = ("The journal registry could not be read ({}); a custom "
                "structure ID still works.".format(e.__class__.__name__))
    journals, bad = [], []
    for j in raw:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id", "")).strip().lower()
        if not ISSUE_ID_RE.match(jid):
            continue
        entry = backfill_pagination(dict(j))
        if pagination_error(entry, jid):
            bad.append(jid)
            entry["page_numbering"], entry["cover_pages"] = "restart", 1
        journals.append({
            "id": jid, "name": str(entry.get("name", "")).strip() or jid,
            "structure": str(entry.get("structure", "volume_issue")),
            "page_numbering": str(entry["page_numbering"]),
            "cover_pages": int(entry["cover_pages"])})
    if bad:
        note = ("The registry's page-numbering settings for {} are not "
                "usable, so the defaults (restart each issue, one cover "
                "page) are offered; correct them in the DOI & XML "
                "Generator.".format(", ".join(bad)))
    return {"journals": journals, "note": note}


# ---------------------------------------------------------------------------
# Step 2: list contents from the OAI-PMH feed (public; urllib, no Chrome).
# ---------------------------------------------------------------------------
class ListError(Exception):
    """One volume / issue could not be listed; other rows still are."""


def fetch_url(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "DCAdminSuite-PageNumbers/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ListError("the feed answered HTTP {} {}".format(e.code, e.reason))
    except urllib.error.URLError as e:
        raise ListError("the feed could not be reached: {}".format(e.reason))


def local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def first(elem, name):
    for e in elem.iter():
        if local(e.tag) == name:
            return e
    return None


def text_of(elem, name) -> str:
    e = first(elem, name)
    return (e.text or "").strip() if e is not None else ""


def parse_item(rec) -> dict:
    """The fields of one OAI <record> this module needs."""
    # The feed double-escapes ampersands in URLs ("&amp;amp;"), so after XML
    # parsing the text still holds "&amp;": unescape once more.
    unesc = html_lib.unescape
    names = []
    for e in rec.iter():
        if local(e.tag) == "author":
            name = " ".join(p for p in (text_of(e, "fname"), text_of(e, "mname"),
                                        text_of(e, "lname")) if p)
            name = name or text_of(e, "organization")
            if name:
                names.append(name)
    full = unesc(text_of(rec, "fulltext-url"))
    path = text_of(rec, "submission-path")
    ctx = parse_qs(urlparse(full).query).get("context", [""])[0] if full else ""
    if not ctx and path:
        ctx = path.split("/", 1)[0]
    return {
        "articleid": text_of(rec, "articleid"),
        "label": text_of(rec, "label"),
        "title": text_of(rec, "title"),
        "authors": "; ".join(names),
        "doctype": text_of(rec, "document-type"),
        "fulltext": full,
        "landing": unesc(text_of(rec, "coverpage-url")),
        "native": bool(text_of(rec, "native-url")),
        "fpage": text_of(rec, "fpage"),
        "lpage": text_of(rec, "lpage"),
        "path": path,
        "context": ctx,
    }


def harvest_items(base_url, ref, logf):
    """Every record of one volume / issue row, in the order the feed sent
    them. Raises ListError."""
    items = []
    url = oai_url(base_url, ref["journal"], ref["volume"], ref["issue"],
                  ref["syear"])
    for page_no in range(1, 51):  # sanity cap; an issue is a page or two
        logf("Reading the feed, page {}: {}".format(page_no, url))
        raw = fetch_url(url)
        try:
            tree = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as e:
            raise ListError("the feed's answer is not well-formed XML ({})"
                            .format(e))
        err = first(tree, "error")
        if err is not None:
            code = err.attrib.get("code", "?")
            if code == "noRecordsMatch":
                raise ListError("the feed holds no records for it (OAI "
                                "noRecordsMatch). Only posted records are "
                                "in the feed.")
            raise ListError("the feed answered with OAI error [{}]: {}".format(
                code, (err.text or "").strip() or "no message"))
        for rec in tree.iter():
            if local(rec.tag) != "record":
                continue
            hdr = first(rec, "header")
            if hdr is not None and hdr.attrib.get("status") == "deleted":
                continue
            items.append(parse_item(rec))
        tok = first(tree, "resumptionToken")
        token = (tok.text or "").strip() if tok is not None else ""
        if not token:
            break
        url = "{}/do/oai/?verb=ListRecords&resumptionToken={}".format(
            base_url, urllib.parse.quote(token, safe=""))
    else:
        raise ListError("the feed's paging did not finish within 50 pages, so "
                        "the list would be incomplete. Choose a single issue.")
    if not items:
        raise ListError("the feed returned no records for it.")
    return items


def label_key(item):
    """Label order: numeric labels by number, then any others by text, then
    by article ID, so the starting order is the same on every listing."""
    lab = item.get("label", "")
    if lab.isdigit():
        return (0, int(lab), "", item.get("articleid", ""))
    return (1, 0, lab, item.get("articleid", ""))


def issue_parts(path):
    """('jid/vol19/iss1', '19', '1') from a submission path's issue part."""
    key = path.rsplit("/", 1)[0] if "/" in path else ""
    vol = iss = ""
    for part in key.split("/")[1:]:
        if part.startswith("vol") and part[3:].isdigit():
            vol = str(int(part[3:]))
        elif part.startswith("iss") and part[3:].isdigit():
            iss = str(int(part[3:]))
    return key, vol, iss


def group_issues(items, ref, entry, warn):
    """One issue per submission-path prefix, in volume and issue order, its
    items in label order. Items the feed gives no article ID are left out
    and said: nothing could match them to a downloaded file."""
    groups = collections.OrderedDict()
    for it in items:
        if not it["articleid"].isdigit():
            warn("{}: a record with no article ID ({!r}) was left out — "
                 "nothing could match it to a downloaded file.".format(
                     describe_ref(ref), it["title"][:60]))
            continue
        key, vol, iss = issue_parts(it["path"])
        if not key:
            key = "/".join(p for p in (
                ref["journal"] + ("_" + ref["syear"] if ref["syear"] else ""),
                "vol" + ref["volume"] if ref["volume"] else "",
                "iss" + ref["issue"] if ref["issue"] else "") if p)
            vol, iss = ref["volume"], ref["issue"]
        g = groups.setdefault(key, {"key": key, "journal": ref["journal"],
                                    "volume": vol, "issue": iss,
                                    "syear": ref["syear"], "items": []})
        g["items"].append(it)
    out = []
    for g in groups.values():
        g["items"].sort(key=label_key)
        g["name"] = (entry or {}).get("name") or ref["journal"]
        g["mode"] = (entry or {}).get("page_numbering", "restart")
        g["cover"] = int((entry or {}).get("cover_pages", 1))
        for it in g["items"]:
            it["on"] = True
            it["hand"] = ""
        out.append(g)
    out.sort(key=lambda g: (int(g["volume"] or 0), int(g["issue"] or 0),
                            g["key"]))
    return out


def default_firsts(issues):
    """First page number offered for each issue: 1, except an issue that
    continues from an earlier issue of its volume in this list (blank,
    meaning the previous issue's last page + 1)."""
    seen = set()
    for g in issues:
        vkey = (g["journal"], g["syear"], g["volume"])
        g["first"] = "" if (g["mode"] == "continue" and vkey in seen) else "1"
        seen.add(vkey)


def describe_ref(ref) -> str:
    d = ref["journal"]
    if ref.get("syear"):
        d += " " + ref["syear"]
    if ref.get("volume"):
        d += " vol. " + ref["volume"]
    if ref.get("issue"):
        d += " iss. " + ref["issue"]
    return d


def describe_issue(g) -> str:
    d = g.get("name") or g.get("journal", "")
    if g.get("syear"):
        d += " " + g["syear"]
    if g.get("volume"):
        d += ", vol. " + g["volume"]
    if g.get("issue"):
        d += ", iss. " + g["issue"]
    return d


# ---------------------------------------------------------------------------
# State machine — the page polls GET /api/state every 1.5 s.
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",  # idle | starting | listing | planning | counting | done | error
    "kind": "",        # the run that last ran: list | plan | count
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "summary": "",
    "warnings": 0,
    "listing": None,   # {"listed": stamp, "issues": [...]} from the last listing
    "link_file": None,  # the file the last run wrote (full path)
    "link_label": "",
}
LOCK = threading.Lock()
RUNNING_PHASES = ("starting", "listing", "planning", "counting")


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary. The listing
# is kept: Clear is a clean start, not a change of what is loaded.
RUN_LOG_NAME = "DC_PageNumbers_Log_{}.txt"
CLEAR_RESETS = {"link_file": None, "link_label": "", "warnings": 0,
                "kind": ""}


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


# The page's settings, by element id, remembered across launches (DC-FORM).
# setsjson holds the volume / issue rows and arrangement the listing with
# the operator's ticks, order and page settings, both as JSON the page
# writes, so a restart comes back to the arrangement as it was left.
FORM_FIELDS = ("setsjson", "arrangement", "plandir", "rundir", "rptdir",
               "outdir",
)
# The page's other controls, which are NOT kept, and why: the log copy box.
# Every control on the page is in one list or the other (_verify_pages.py).
FORM_NOT_KEPT = (
    "logfull",
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
        del STATE["log"][:-200]
        run_log_add(line)
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


def describe_error(e) -> str:
    return "{}: {}".format(e.__class__.__name__, e)


# ---------------------------------------------------------------------------
# Request validation — ONE function per input, called by every endpoint
# that takes it (module contract 11).
# ---------------------------------------------------------------------------
SETS_MAX = 20
ISSUES_MAX = 200
ITEMS_MAX = 2000
PAGE_MAX = 99999
HAND_MAX = 9999


def validate_sets(raw_sets, journals):
    """The volume / issue rows for a listing: (refs, "") or (None, msg)."""
    if not isinstance(raw_sets, list) or not raw_sets:
        return None, "Add at least one volume / issue."
    if len(raw_sets) > SETS_MAX:
        return None, "At most {} volume / issue rows in one listing.".format(
            SETS_MAX)
    entries = {j["id"]: j for j in journals}
    refs, seen = [], set()
    for i, s in enumerate(raw_sets, start=1):
        ref, msg = issue_ref(s, entries, i)
        if msg:
            return None, msg
        key = (ref["journal"], ref["syear"], ref["volume"], ref["issue"])
        if key in seen:
            return None, ("Volume / issue #{} repeats an earlier row ({})."
                          .format(i, describe_ref(ref)))
        seen.add(key)
        refs.append(ref)
    return refs, ""


def _digits(value, lo, hi):
    """int(value) when it is a whole number lo..hi (as int or digits)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    else:
        text = str(value if value is not None else "").strip()
        if not text.isdigit():
            return None
        n = int(text)
    return n if lo <= n <= hi else None


def _same_site(url, base):
    """True when url is an http(s) address on the instance's own host."""
    try:
        u, b = urlparse(url), urlparse(base)
    except ValueError:
        return False
    return (u.scheme in ("http", "https") and bool(u.netloc)
            and u.netloc.lower() == b.netloc.lower())


def file_link_error(url, base_url, article, ctx):
    """"" when url is the instance's own file link for this article."""
    if not _same_site(url, base_url):
        return "its PDF link is not on {}".format(urlparse(base_url).netloc
                                                  or "this instance")
    p = urlparse(url)
    q = parse_qs(p.query)
    if not p.path.endswith("/cgi/viewcontent.cgi"):
        return "its PDF link is not a file link"
    if q.get("article", [""])[0] != article:
        return "its PDF link names another article"
    if ctx and q.get("context", [""])[0] != ctx:
        return "its PDF link names another structure"
    # Module contract 9: never a link that changes state on the server.
    if "force" in q:
        return "its PDF link asks the server to change something"
    return ""


def validate_arrangement(raw, base_url):
    """The arrangement the page sends to write a plan or count pages.

    Returns (arrangement, "") or (None, a message). Called by both
    /api/plan and /api/start, so the two cannot accept different things.
    Every link is checked against the instance's own address, because the
    arrangement comes back from the page and the plan it makes is fetched
    in the operator's browser."""
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        return None, "List the contents of an issue first."
    issues = raw["issues"]
    if not issues:
        return None, "List the contents of an issue first."
    if len(issues) > ISSUES_MAX:
        return None, "At most {} issues in one run.".format(ISSUES_MAX)
    out, ticked, n_items = [], 0, 0
    earlier = set()
    for gi, g in enumerate(issues, start=1):
        if not isinstance(g, dict) or not isinstance(g.get("items"), list):
            return None, "Issue #{} is not usable; list it again.".format(gi)
        jid = str(g.get("journal", "")).strip().lower()
        if not ISSUE_ID_RE.match(jid):
            return None, "Issue #{} names no journal; list it again.".format(gi)
        vol = str(g.get("volume", "")).strip()
        iss = str(g.get("issue", "")).strip()
        syear = str(g.get("syear", "")).strip()
        if (vol and not vol.isdigit()) or (iss and not iss.isdigit()) or (
                syear and not ISSUE_YEAR_RE.match(syear)):
            return None, "Issue #{} is not usable; list it again.".format(gi)
        name = str(g.get("name", "")).strip()[:300] or jid
        label = describe_issue({"name": name, "journal": jid, "volume": vol,
                                "issue": iss, "syear": syear})
        mode = str(g.get("mode", ""))
        if mode not in PAGINATION_MODES:
            return None, "{}: choose how its pages are numbered.".format(label)
        cover = _digits(g.get("cover"), 0, COVER_PAGES_MAX)
        if cover is None:
            return None, ("{}: cover pages must be a whole number from 0 to "
                          "{}.".format(label, COVER_PAGES_MAX))
        first_raw = str(g.get("first", "") if g.get("first") is not None
                        else "").strip()
        first_page = None
        if first_raw:
            first_page = _digits(first_raw, 1, PAGE_MAX)
            if first_page is None:
                return None, ("{}: the first page number must be a whole "
                              "number from 1 to {}.".format(label, PAGE_MAX))
        vkey = (jid, syear, vol)
        if first_page is None and mode == "continue" and vkey not in earlier:
            return None, ("{} continues from the previous issue, but no "
                          "earlier issue of its volume is in this run: enter "
                          "its first page number.".format(label))
        earlier.add(vkey)
        items = []
        for ii, it in enumerate(g["items"], start=1):
            n_items += 1
            if n_items > ITEMS_MAX:
                return None, "At most {} items in one run.".format(ITEMS_MAX)
            if not isinstance(it, dict):
                return None, "{}: item {} is not usable; list it again.".format(
                    label, ii)
            art = str(it.get("articleid", "")).strip()
            if not art.isdigit():
                return None, "{}: item {} has no article ID.".format(label, ii)
            ctx = str(it.get("context", "")).strip().lower()
            if ctx and not ISSUE_ID_RE.match(ctx):
                return None, "{}: article {} names an unusable structure.".format(
                    label, art)
            full = str(it.get("fulltext", "")).strip()
            if full:
                why = file_link_error(full, base_url, art, ctx)
                if why:
                    return None, "{}: article {} — {}.".format(label, art, why)
            landing = str(it.get("landing", "")).strip()
            if landing and not _same_site(landing, base_url):
                return None, ("{}: article {} — its record link is not on "
                              "this instance.".format(label, art))
            on = it.get("on")
            if not isinstance(on, bool):
                return None, "{}: article {} — Paginate is not yes or no.".format(
                    label, art)
            hand_raw = str(it.get("hand", "") if it.get("hand") is not None
                           else "").strip()
            hand = None
            if hand_raw:
                hand = _digits(hand_raw, 1, HAND_MAX)
                if hand is None:
                    return None, ("{}: article {} — pages by hand must be a "
                                  "whole number from 1 to {}.".format(
                                      label, art, HAND_MAX))
            ticked += 1 if on else 0
            items.append({
                "articleid": art, "context": ctx or jid,
                "label": str(it.get("label", ""))[:32],
                "title": str(it.get("title", ""))[:2000],
                "authors": str(it.get("authors", ""))[:2000],
                "doctype": str(it.get("doctype", ""))[:64],
                "fulltext": full, "landing": landing,
                "native": bool(it.get("native")),
                "fpage": str(it.get("fpage", ""))[:16],
                "lpage": str(it.get("lpage", ""))[:16],
                "on": on, "hand": hand})
        out.append({"key": str(g.get("key", ""))[:300], "journal": jid,
                    "volume": vol, "issue": iss, "syear": syear, "name": name,
                    "label": label, "mode": mode, "cover": cover,
                    "first": first_page, "items": items})
    if not ticked:
        return None, "Tick at least one item to paginate."
    return {"issues": out, "listed": str(raw.get("listed", ""))[:40]}, ""


# ---------------------------------------------------------------------------
# Step 4: the download plan, a workbook the Batch File Downloader re-runs.
#
# Its first sheet has the downloader's own report columns, each file row
# "Not attempted", so the downloader's re-run fetches exactly these files in
# the operator's monitored browser and reports them like any other. The name
# deliberately does not begin DC_FileDownload_Plan_: the downloader treats a
# workbook of that name as a whole job's plan and would count every other
# record of the journal as added since the plan was taken.
# ---------------------------------------------------------------------------
# Must equal the downloader's REPORT_HEADERS (_verify_page_numbers.py reads
# both with ast and compares them).
DOWNLOADER_HEADERS = [
    "Order", "Article ID", "Title", "Record State", "File Kind",
    "Visibility", "Version", "Version Date", "Original Filename",
    "Saved Filename", "Size", "Status", "File URL", "Public Record URL",
    "Admin Record URL", "Time", "Content Type", "Access",
    "Type / Release Option", "Embargo", "Plan", "Fetched via"]
DOWNLOADER_NOT_ATTEMPTED = "Not attempted"
# The downloader's version names for the two copies of the current PDF.
VERSION_STAMPED = "current"
VERSION_UNSTAMPED = "current-unstamped"
PLAN_NAME = "DC_PageNumbers_Download_{}.xlsx"
REPORT_NAME = "DC_PageNumbers_{}.xlsx"
REPORT_PARTIAL = "DC_PageNumbers_{}_PARTIAL.xlsx"


def stamped_file_url(base_url, ctx, article):
    """The record's current file as the public sees it, cover page included:
    the same default content stream the downloader asks for."""
    return ("{0}/cgi/viewcontent.cgi?article={1}&context={2}&preview_mode=1"
            .format(base_url, article, ctx))


def admin_url(base_url, ctx, article):
    """The record's admin page, as the downloader writes it for a journal."""
    return ("{0}/cgi/editor.cgi?window=abstract&article={1}&context={2}"
            .format(base_url, article, ctx))


def plan_rows(arr, base_url):
    """(rows, notes): two downloader rows per ticked item that has a PDF —
    the stamped copy, then the unstamped file — in the arrangement's order."""
    rows, notes = [], []
    order = 0
    for g in arr["issues"]:
        for it in g["items"]:
            if not it["on"]:
                continue
            if not it["fulltext"]:
                notes.append("{}: article {} has no PDF in the feed, so it is "
                             "not in the plan; enter its page count by hand."
                             .format(g["label"], it["articleid"]))
                continue
            ctx, art = it["context"], it["articleid"]
            for version, url in ((VERSION_STAMPED,
                                  stamped_file_url(base_url, ctx, art)),
                                 (VERSION_UNSTAMPED, it["fulltext"])):
                order += 1
                rows.append((order, art, it["title"], "", "primary", "public",
                             version, "", "", "", "", DOWNLOADER_NOT_ATTEMPTED,
                             url, it["landing"], admin_url(base_url, ctx, art),
                             "", "", "", "", "", "", ""))
    return rows, notes


def _header_style(primary):
    from openpyxl.styles import Font, PatternFill
    hexc = (primary or FALLBACK_BRAND["primary"]).lstrip("#")[:6].upper()
    ink = contrast_text("#" + hexc).lstrip("#").upper()
    return (Font(bold=True, color="FF" + ink),
            PatternFill("solid", fgColor="FF" + hexc))


def _sheet(ws, headers, rows, widths, primary, link_cols=()):
    from openpyxl.utils import get_column_letter
    font, fill = _header_style(primary)
    ws.append(list(headers))
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font, cell.fill = font, fill
    for r in rows:
        ws.append(list(r))
    for col in link_cols:
        for row in range(2, len(rows) + 2):
            cell = ws.cell(row=row, column=col)
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def write_download_plan(path, rows, notes, arr, primary):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    _sheet(ws, DOWNLOADER_HEADERS, rows,
           [8, 12, 44, 14, 10, 10, 18, 12, 18, 30, 10, 16, 72, 60, 60,
            10, 14, 10, 18, 12, 10, 14], primary, (13, 14, 15))
    about = wb.create_sheet("About")
    ticked = sum(1 for g in arr["issues"] for it in g["items"] if it["on"])
    info = [
        ("What this is", "A download plan written by the Page Number "
         "Generator: for each item ticked for pagination, the stamped copy "
         "the public sees and the unstamped file as uploaded."),
        ("How to run it", "In the Batch File Downloader, re-run a report or "
         "plan and choose this file. Then give the Page Number Generator the "
         "run folder the downloader writes (and its reports folder, if the "
         "reports were saved elsewhere)."),
        ("Written", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("Issues", len(arr["issues"])),
        ("Items ticked", ticked),
        ("Files in the plan", len(rows)),
    ]
    for n in notes:
        info.append(("Left out", n))
    _sheet(about, ["Item", "Value"], info, [22, 100], primary)
    wb.save(path)


# ---------------------------------------------------------------------------
# Step 5: count and number.
# ---------------------------------------------------------------------------
DOWNLOADER_REPORT_GLOBS = ("DC_FileDownload_Plan_*.xlsx",
                           "DC_FileDownload_Report_*.xlsx",
                           "DC_FileDownload_Retry_*.xlsx")
DOWNLOADER_REQUIRED = ("Article ID", "File Kind", "Version", "Status",
                       "Saved Filename", "File URL")


def report_stamp_key(path):
    """Chronological order for the downloader's report names (each carries a
    YYYYMMDD_HHMMSS stamp), as the downloader itself orders them."""
    m = re.search(r"(\d{8}_\d{6})", os.path.basename(path))
    return (m.group(1) if m else "", os.path.basename(path))


def report_folders(folders):
    """Each folder given, and the folders directly inside it.

    The downloader writes a structure's report into that structure's own
    folder in the run folder (DC_FileDownloads_<stamp>/<structure>/), not
    at the top of the run folder, so a reader that looks only at the top
    finds nothing. Folders whose names begin with "." or "_" (the
    downloader's private _incoming/) are passed over."""
    out = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        out.append(folder)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            inner = os.path.join(folder, name)
            if not name.startswith((".", "_")) and os.path.isdir(inner):
                out.append(inner)
    return out


def read_downloader_rows(folders):
    """What the downloader's reports say about each current PDF.

    Returns ({(context, article, "stamped"|"unstamped"): {status, saved,
    report}}, [report names read], [problems]). Every plan, report and
    retry workbook in each folder and the folders directly inside it
    (report_folders()) is read by column NAME;
    the latest report's row for a file wins, as in the downloader's own
    merge."""
    from openpyxl import load_workbook
    found = []
    for folder in report_folders(folders):
        for pattern in DOWNLOADER_REPORT_GLOBS:
            found += glob.glob(os.path.join(folder, pattern))
    found = sorted(set(found), key=report_stamp_key)
    rows, read, problems = {}, [], []
    for path in found:
        name = os.path.basename(path)
        if name.endswith("_PARTIAL.xlsx"):
            continue
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as e:  # a damaged workbook is one problem, not a stop
            problems.append("{} could not be read ({})".format(
                name, describe_error(e)))
            continue
        try:
            it = wb.active.iter_rows(values_only=True)
            head = [str(h or "") for h in (next(it, None) or ())]
            missing = [h for h in DOWNLOADER_REQUIRED if h not in head]
            if missing:
                problems.append("{} is not a download report (missing {})"
                                .format(name, ", ".join(missing)))
                continue
            idx = {h: head.index(h) for h in head if h}

            def cell(r, h):
                i = idx.get(h)
                return "" if i is None or i >= len(r) else str(r[i] or "").strip()

            for r in it:
                if not r:
                    continue
                kind, ver = cell(r, "File Kind"), cell(r, "Version")
                if kind != "primary" or ver not in (VERSION_STAMPED,
                                                    VERSION_UNSTAMPED):
                    continue
                art = cell(r, "Article ID")
                ctx = parse_qs(urlparse(cell(r, "File URL")).query).get(
                    "context", [""])[0].lower()
                if not art or not ctx:
                    continue
                form = "unstamped" if ver == VERSION_UNSTAMPED else "stamped"
                rows[(ctx, art, form)] = {"status": cell(r, "Status"),
                                          "saved": cell(r, "Saved Filename"),
                                          "report": name}
            read.append(name)
        finally:
            wb.close()
    return rows, read, problems


def find_saved(run_dir, ctx, name):
    """Where the downloader put a saved file: its structure's folder in the
    run folder, the run folder itself, or one folder down. "" if nowhere."""
    if not name or os.path.basename(name) != name:
        return ""
    for cand in (os.path.join(run_dir, ctx, name), os.path.join(run_dir, name)):
        if os.path.isfile(cand):
            return cand
    for cand in sorted(glob.glob(os.path.join(glob.escape(run_dir), "*",
                                              glob.escape(name)))):
        if os.path.isfile(cand):
            return cand
    return ""


def count_pages(path):
    """(pages, "") or (None, why). pikepdf reads the page tree; nothing else
    in the file is opened."""
    try:
        import pikepdf
    except ImportError:
        return None, "pikepdf is not installed, so no PDF could be counted"
    try:
        with pikepdf.open(path) as pdf:
            n = len(pdf.pages)
    except Exception as e:
        return None, "{} could not be read as a PDF ({})".format(
            os.path.basename(path), e.__class__.__name__)
    if n < 1:
        return None, "{} has no pages".format(os.path.basename(path))
    return n, ""


def file_count(rows, run_dir, ctx, article, form):
    """(pages, note, file name) for one copy of one item, from the
    downloader's row for it and the file it names."""
    row = rows.get((ctx, article, form))
    if row is None:
        return None, "no downloader report has a row for it", ""
    if not row["status"].startswith("Downloaded"):
        return None, "the downloader's row says: {}".format(
            row["status"] or "(no status)"), ""
    path = find_saved(run_dir, ctx, row["saved"])
    if not path:
        return None, ("the downloader saved it as {}, which is not in the run "
                      "folder".format(row["saved"] or "(no name)")), ""
    n, why = count_pages(path)
    return n, why, os.path.basename(path)


def number_issue(items, first_page, cover, stop=""):
    """Number one issue's items, in order. Pure.

    items: dicts with on, articleid, unstamped / stamped (page counts or
    None), u_note / s_note (why a count is missing) and hand (pages entered
    by hand, or None). first_page: the issue's first page, or None when it
    cannot be known, with `stop` saying why.

    Returns (results, end, stop): one result per item (pages used, first,
    last, check, notes); end, the last page used (first_page - 1 when the
    issue numbered nothing), or None when numbering stopped; and the reason
    it stopped, or "". An item whose count cannot be used — a cover check
    that disagrees, a copy that is missing, no PDF and no count by hand —
    stops the numbering: every item after it would otherwise carry a page
    number that is only a guess."""
    page = first_page
    results = []
    for pos, it in enumerate(items, start=1):
        r = {"position": pos, "pages": None, "first": None, "last": None,
             "check": "", "notes": []}
        results.append(r)
        if not it["on"]:
            r["check"] = "not paginated"
            continue
        u, s, hand = it.get("unstamped"), it.get("stamped"), it.get("hand")
        if u is not None and s is not None:
            diff = s - u
            if diff == cover:
                r["check"] = "agrees"
                r["pages"] = u
            else:
                r["check"] = "disagrees"
                r["notes"].append(
                    "The stamped copy has {} page(s) and the unstamped file "
                    "{}: a difference of {}, where the issue's cover-page "
                    "count is {}. Not numbered.".format(s, u, diff, cover))
        elif u is not None:
            r["check"] = "not checked"
            r["notes"].append("The unstamped file has {} page(s), but there "
                              "is no stamped copy to check it against ({}). "
                              "Not numbered.".format(u, it.get("s_note")
                                                     or "not downloaded"))
        elif hand:
            r["check"] = "not checked — pages entered by hand"
            r["pages"] = hand
            r["notes"].append("No PDF was counted ({}); the page count was "
                              "entered by hand.".format(it.get("u_note")
                                                        or "no PDF"))
        else:
            r["check"] = "no PDF"
            r["notes"].append("No PDF was counted ({}), and no page count was "
                              "entered by hand. Not numbered.".format(
                                  it.get("u_note") or "no PDF"))
        if hand and u is not None:
            r["notes"].append("The page count entered by hand ({}) was not "
                              "used: the PDF was counted.".format(hand))
        if r["pages"] is None:
            if not stop:
                stop = ("numbering stopped at position {} (article {}), whose "
                        "page count could not be used (see its row)".format(
                            pos, it.get("articleid", "?")))
            continue
        if stop:
            r["notes"].append("Not numbered: " + stop + ".")
            continue
        r["first"], r["last"] = page, page + r["pages"] - 1
        page = r["last"] + 1
    if stop:
        return results, None, stop
    return results, page - 1, ""


PAGES_HEADERS = ["Issue", "Position", "Label", "Article ID", "Title",
                 "Record URL", "Document Type", "Paginated",
                 "Unstamped Pages", "Stamped Pages", "Cover Check",
                 "Pages Used", "First Page", "Last Page",
                 "Existing First Page", "Existing Last Page", "Differs",
                 "Unstamped File", "Stamped File", "Notes"]
PAGES_WIDTHS = [34, 9, 7, 11, 44, 50, 14, 10, 10, 10, 16, 10, 10, 10, 10,
                10, 9, 34, 34, 80]
SUMMARY_HEADERS = ["Issue", "Numbering", "Cover Pages", "Most Common "
                   "Difference", "First Page", "Last Page", "Items",
                   "Paginated", "Numbered", "Flagged", "Without PDF", "Notes"]
SUMMARY_WIDTHS = [34, 24, 10, 14, 10, 10, 8, 10, 10, 9, 11, 80]


def differs(existing_first, existing_last, first_page, last_page):
    """"yes" / "no" against the record's existing pages, "" when either side
    is missing (nothing to compare)."""
    if not (existing_first or existing_last) or first_page is None:
        return ""
    same = (existing_first == str(first_page)
            and existing_last == str(last_page))
    return "no" if same else "yes"


def common_difference(pairs):
    """The most common stamped − unstamped difference, as text."""
    diffs = [s - u for (u, s) in pairs if u is not None and s is not None]
    if not diffs:
        return "none measured"
    counts = collections.Counter(diffs).most_common()
    top = [d for d, n in counts if n == counts[0][1]]
    if len(top) > 1:
        return "tie: " + ", ".join(str(d) for d in sorted(top))
    return "{} ({} of {})".format(top[0], counts[0][1], len(diffs))


def write_report(path, pages, summary, checks, primary):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Pages"
    _sheet(ws, PAGES_HEADERS, pages, PAGES_WIDTHS, primary, (6,))
    _sheet(wb.create_sheet("Summary"), SUMMARY_HEADERS, summary,
           SUMMARY_WIDTHS, primary)
    _sheet(wb.create_sheet("Checks"), ["Check", "Result", "Detail"], checks,
           [34, 40, 90], primary)
    wb.save(path)


def _primary(session):
    return ((session.get("branding") or {}).get("colors") or {}).get(
        "primary", FALLBACK_BRAND["primary"])


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
def list_worker(session, refs, journals):
    base_url = session.get("base_url", "").rstrip("/")
    entries = {j["id"]: j for j in journals}
    warnings, issues, seen_keys = [], [], set()

    def warn(msg):
        warnings.append(msg)
        log("WARNING: " + msg)

    try:
        set_state(phase="listing", kind="list", summary="", link_file=None,
                  link_label="", warnings=0)
        failed = 0
        for i, ref in enumerate(refs):
            set_progress(i, len(refs), "Listing " + describe_ref(ref))
            try:
                items = harvest_items(base_url, ref, log)
            except ListError as e:
                failed += 1
                warn("{}: {}".format(describe_ref(ref), e))
                continue
            for g in group_issues(items, ref, entries.get(ref["journal"]),
                                  warn):
                if g["key"] in seen_keys:
                    warn("{} is in more than one row; it is listed once."
                         .format(describe_issue(g)))
                    continue
                seen_keys.add(g["key"])
                issues.append(g)
                log("{}: {} item(s), in label order to start with.".format(
                    describe_issue(g), len(g["items"])))
        set_progress(len(refs), len(refs), "")
        if not issues:
            return fail("Nothing was listed. " + " | ".join(warnings))
        default_firsts(issues)
        n_items = sum(len(g["items"]) for g in issues)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        set_state(listing={"listed": stamp, "issues": issues})
        summary = ("Listed {} item(s) in {} issue(s). Tick the items that are "
                   "paginated, put them in order, then write the download "
                   "plan.".format(n_items, len(issues)))
        if failed:
            summary += " {} row(s) could not be listed; see the log.".format(
                failed)
        set_state(phase="done", summary=summary, warnings=len(warnings),
                  outcome=outcome_tone(len(warnings)))
    except Exception as e:  # never leave the page spinning
        fail("Unexpected error: " + describe_error(e))


def plan_worker(session, arr, out_dir):
    base_url = session.get("base_url", "").rstrip("/")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        set_state(phase="planning", kind="plan", summary="", link_file=None,
                  link_label="", warnings=0)
        rows, notes = plan_rows(arr, base_url)
        for n in notes:
            log("NOTE: " + n)
        if not rows:
            save_run_log(out_dir, stamp)
            return fail("No ticked item has a PDF in the feed, so there is "
                        "nothing for the downloader to fetch. Enter page "
                        "counts by hand and go straight to Count and number.")
        path = os.path.join(out_dir, PLAN_NAME.format(stamp))
        write_download_plan(path, rows, notes, arr, _primary(session))
        items = len(rows) // 2
        log("Wrote the download plan: {} file(s) for {} item(s) — {}".format(
            len(rows), items, path))
        log("Next: in the Batch File Downloader, re-run a report or plan and "
            "choose this file. When it has finished, give this module the run "
            "folder it wrote.")
        save_run_log(out_dir, stamp)
        set_state(phase="done", link_file=path, link_label="Download plan",
                  summary=("Download plan written: {} file(s) for {} item(s) "
                           "(the stamped copy and the unstamped file of each). "
                           "Run it in the Batch File Downloader.".format(
                               len(rows), items)),
                  outcome=outcome_tone(run_warnings()))
    except Exception as e:
        fail("Unexpected error: " + describe_error(e))


def count_worker(session, arr, run_dir, rpt_dir, out_dir):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    primary = _primary(session)
    partial = os.path.join(out_dir, REPORT_PARTIAL.format(stamp))
    final = os.path.join(out_dir, REPORT_NAME.format(stamp))
    try:
        set_state(phase="counting", kind="count", summary="", link_file=None,
                  link_label="", warnings=0)
        folders = [run_dir] + ([rpt_dir] if rpt_dir and
                               os.path.abspath(rpt_dir) !=
                               os.path.abspath(run_dir) else [])
        rows, read, problems = read_downloader_rows(folders)
        for p in problems:
            log("WARNING: " + p)
        if not read:
            save_run_log(out_dir, stamp)
            return fail("No download report was found in {}, or in the "
                        "folders directly inside it. Give the run folder the "
                        "Batch File Downloader wrote, and its reports folder "
                        "too if the reports were saved elsewhere.".format(
                            " or ".join(folders)))
        log("Read {} downloader report(s): {}".format(len(read),
                                                       ", ".join(read)))
        total = sum(len(g["items"]) for g in arr["issues"])
        done = 0
        pages, summary = [], []
        n_on = n_numbered = n_flagged = n_nopdf = n_hand = n_agree = 0
        n_checked = n_existing = 0
        ends = {}
        for g in arr["issues"]:
            work = []
            for it in g["items"]:
                done += 1
                set_progress(done, total, "Counting " + g["label"])
                w = dict(it)
                w["unstamped"] = w["stamped"] = None
                w["u_note"] = w["s_note"] = w["u_file"] = w["s_file"] = ""
                if it["on"]:
                    if it["fulltext"]:
                        for form in ("unstamped", "stamped"):
                            n, note, fname = file_count(
                                rows, run_dir, it["context"], it["articleid"],
                                form)
                            w[form] = n
                            w[form[0] + "_note"] = note
                            w[form[0] + "_file"] = fname
                    else:
                        w["u_note"] = "the feed has no PDF for it"
                work.append(w)
            vkey = (g["journal"], g["syear"], g["volume"])
            stop = ""
            if g["first"] is not None:
                first_page = g["first"]
            elif g["mode"] == "continue":
                prev = ends.get(vkey)
                first_page = None if prev is None else prev + 1
                if prev is None:
                    stop = ("the previous issue of this volume did not finish "
                            "numbering, so this issue's first page is not "
                            "known")
            else:
                first_page = 1
            results, end, stop = number_issue(work, first_page, g["cover"],
                                              stop)
            ends[vkey] = end
            counts = {"items": len(work), "on": 0, "numbered": 0,
                      "flagged": 0, "nopdf": 0}
            for w, r in zip(work, results):
                if w["on"]:
                    counts["on"] += 1
                    if r["first"] is not None:
                        counts["numbered"] += 1
                    if r["check"] in ("disagrees", "not checked"):
                        counts["flagged"] += 1
                    if w["unstamped"] is None:
                        counts["nopdf"] += 1
                    if w["unstamped"] is not None and w["stamped"] is not None:
                        n_checked += 1
                        n_agree += 1 if r["check"] == "agrees" else 0
                    if r["check"].startswith("not checked — pages entered"):
                        n_hand += 1
                if w["fpage"] or w["lpage"]:
                    n_existing += 1
                notes = list(r["notes"])
                if w["on"] and w["u_note"] and w["unstamped"] is None and \
                        not any(w["u_note"] in n for n in notes):
                    notes.append("Unstamped file: " + w["u_note"] + ".")
                pages.append((
                    g["label"], r["position"], w["label"], w["articleid"],
                    w["title"], w["landing"], w["doctype"],
                    "yes" if w["on"] else "no",
                    w["unstamped"], w["stamped"], r["check"], r["pages"],
                    r["first"], r["last"], w["fpage"], w["lpage"],
                    differs(w["fpage"], w["lpage"], r["first"], r["last"]),
                    w["u_file"], w["s_file"], " ".join(notes)))
            n_on += counts["on"]
            n_numbered += counts["numbered"]
            n_flagged += counts["flagged"]
            n_nopdf += counts["nopdf"]
            if g["mode"] == "restart":
                mode_word = "restart at page {}".format(first_page)
            elif g["first"] is not None:
                mode_word = "continue, but from page {} as set".format(
                    g["first"])
            else:
                mode_word = "continue from the previous issue"
            summary.append((
                g["label"], mode_word, g["cover"],
                common_difference([(w["unstamped"], w["stamped"])
                                   for w in work if w["on"]]),
                first_page, end if counts["numbered"] else None,
                counts["items"], counts["on"], counts["numbered"],
                counts["flagged"], counts["nopdf"],
                (stop[0].upper() + stop[1:] + ".") if stop else ""))
            log("{}: {} of {} ticked item(s) numbered{}.".format(
                g["label"], counts["numbered"], counts["on"],
                " — " + stop if stop else (
                    ", pages {}–{}".format(first_page, end)
                    if counts["numbered"] else "")))
            write_report(partial, pages, summary, [], primary)
        checks = [
            ("Downloader reports read", str(len(read)), ", ".join(read)),
            ("Items matched to downloaded files",
             "by structure and article ID",
             "through the downloader's reports; never by title or file name"),
            ("Cover-page check",
             "{} of {} item(s) agree".format(n_agree, n_checked),
             "for each item with both copies: stamped pages − unstamped "
             "pages must equal its issue's cover-page count; an item that "
             "disagrees is not numbered"),
            ("Pages entered by hand", str(n_hand),
             "used only for an item with no PDF counted; not checked"),
            ("Order", "as arranged on the page",
             "the feed's label order was only the starting point; whether the "
             "order matches each issue's table of contents was not checked"),
            ("Existing first / last page in the feed",
             "{} of {} item(s)".format(n_existing, total),
             "compared in the Differs column where present"),
            ("Not done", "nothing was written to any record",
             "page numbers are entered in Digital Commons separately"),
        ]
        for p in problems:
            checks.append(("Report not read", p, ""))
        write_report(final, pages, summary, checks, primary)
        try:
            os.remove(partial)
        except OSError:
            pass
        log("Wrote the report — " + final)
        save_run_log(out_dir, stamp)
        problems_n = (n_on - n_numbered) + len(problems)
        text = ("Numbered {} of {} ticked item(s) in {} issue(s)".format(
            n_numbered, n_on, len(arr["issues"])))
        if n_flagged:
            text += "; {} flagged by the cover-page check".format(n_flagged)
        if n_on - n_numbered:
            text += "; {} not numbered — see the report's Notes".format(
                n_on - n_numbered)
        set_state(phase="done", link_file=final, link_label="Report",
                  summary=text + ".",
                  outcome=("red" if n_on and not n_numbered
                           else outcome_tone(problems_n + run_warnings())))
    except Exception as e:
        if os.path.exists(partial):
            log("The report so far is kept as " + partial)
        fail("Unexpected error: " + describe_error(e))


# ---------------------------------------------------------------------------
# UI — branded page per suite conventions (WCAG 2.1 AA). Tokens are
# substituted at startup (no str.format, so CSS/JS braces stay untouched).
# A plain string, so nothing in it uses a backslash.
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ — __SUITE__</title>
<style>
 .issue{border:1px solid #d7d4cc;border-radius:6px;padding:.6rem .8rem;margin:.75rem 0;background:#fbfaf7}
 .issue h3{margin:.1rem 0 .5rem;font-size:1rem}
 .issue .row{display:flex;flex-wrap:wrap;gap:.4rem .9rem;align-items:center;margin:0 0 .5rem}
 table.items{border-collapse:collapse;width:100%;font-size:.9rem;background:#fff}
 table.items th,table.items td{border:1px solid #d7d4cc;padding:.25rem .4rem;text-align:left;vertical-align:top}
 table.items tr.off td{color:#5a5a60;background:#f2f0ea}
 table.items td.mv{white-space:nowrap}
 table.items td.mv label{padding:0 .2rem}
 button.small{padding:.25rem .6rem;font-size:.85rem}
 input.wide{width:12rem}
 .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
 .stephint{font-size:.85rem;color:#5a5a60;margin:.35rem 0 .5rem}
 :root{--primary:__PRIMARY__;--accent:__ACCENT__;--onprimary:__ONPRIMARY__}
 *{box-sizing:border-box}
 body{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f7f6f3;color:#1b1b1f}
 header{background:var(--primary);color:var(--onprimary);padding:1rem 1.25rem}
 header h1{margin:0;font-size:1.2rem}
 header p{margin:.25rem 0 0;font-size:.9rem}
 header a{color:var(--onprimary)}
 .bar{height:5px;background:var(--accent)}
 main{max-width:62rem;margin:0 auto;padding:1.25rem}
 fieldset{border:1px solid #d7d4cc;border-radius:6px;margin:0 0 1rem;padding:.75rem 1rem;background:#fff}
 legend{font-weight:600;padding:0 .35rem}
 label{display:inline-flex;gap:.35rem;align-items:center;padding:.15rem 0}
 input[type=text],select{padding:.4rem .5rem;border:1px solid #a9a396;border-radius:5px;font:inherit;background:#fff}
 input.small{width:5.5rem} input.tiny{width:4.2rem}
 button{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
        padding:.55rem 1.2rem;font:inherit;font-weight:600;cursor:pointer}
 button.secondary{background:#e7e3da;color:#1b1b1f}
 button.linkish{background:none;color:#8c2f10;padding:.2rem .4rem;font-weight:600}
 button:disabled{opacity:.55;cursor:not-allowed}
 :focus-visible{outline:3px solid #1a5dc8;outline-offset:2px}
 .set{border:1px solid #d7d4cc;border-radius:6px;padding:.6rem .8rem;margin:.5rem 0;background:#fbfaf7}
 .set .row{display:flex;flex-wrap:wrap;gap:.4rem .9rem;align-items:center}
 .set .row+.row{margin-top:.35rem}
 .mintextra{display:none}
 .set.minting .mintextra{display:inline-flex}
 /* yearly-series rows: Year replaces Vol/Iss-style set addressing */
 .syearlab{display:none}
 .set.yearly .syearlab{display:inline-flex}
 .set.yearly .isslab{display:none}
 .set.yearly.minting .mintyear{display:none}
 #status{margin:1rem 0;padding:.7rem .9rem;border-radius:6px;background:#eef1f5;border:1px solid #d7d4cc}
 #status.done{background:#e8f0e6;border-color:#4a6741}
 #status.error{background:#f7e8e2;border-color:#a33a12}
 #status.waiting{background:#fdf3dc;border-color:#b58a2e}
 #waitbtns{margin:.5rem 0 0;display:none;gap:.6rem}
 #files{margin:.4rem 0 0;padding-left:1.2rem}
 #log{background:#1b1b1f;color:#d8d8de;border-radius:6px;padding:.7rem .9rem;
      font:13px/1.5 ui-monospace,monospace;max-height:16rem;overflow:auto;white-space:pre-wrap}
 table.reg{border-collapse:collapse;width:100%;font-size:.9rem}
 table.reg th,table.reg td{border:1px solid #d7d4cc;padding:.25rem .4rem;text-align:left}
 table.reg input,table.reg select{width:100%;border:1px solid #cfcabf;padding:.25rem .3rem}
 table.reg .idc{width:11rem}.reg .issnc{width:7.5rem}.reg .typec{width:8rem}
 .hint{font-size:.85rem;color:#5a5a60;margin:.25rem 0 0}
 /* disabled per-title actions (DOI/DOAJ turned off in the registry) */
 label.off{opacity:.5}
 label.off input{cursor:not-allowed}
 .offnote{font-size:.8rem;color:#8c2f10;margin:.25rem 0 0}
 /* registry summary table + row actions */
 table.reg tbody tr{cursor:pointer}
 table.reg tbody tr:hover,table.reg tbody tr:focus-within{background:#f2eee4}
 table.reg td.flag,table.reg th.flag{text-align:center;width:3.4rem}
 .pill{display:inline-block;font-size:.75rem;font-weight:600;border-radius:10px;
       padding:.05rem .5rem;background:#e7e3da;color:#3a3a40}
 .pill.on{background:#e0ecda;color:#2f5426}
 .pill.no{background:#efe2dc;color:#8c2f10}
 .rowbtn{background:none;border:0;color:var(--primary);font:inherit;font-weight:600;
         cursor:pointer;padding:.2rem .3rem;text-align:left}
 .rowbtn:hover{text-decoration:underline}
 /* modal dialog */
 .backdrop{position:fixed;inset:0;background:rgba(20,20,28,.55);display:none;
           align-items:flex-start;justify-content:center;padding:2rem 1rem;overflow:auto;z-index:50}
 .backdrop.open{display:flex}
 .modal{background:#fff;border-radius:8px;max-width:40rem;width:100%;padding:1.1rem 1.25rem;
        box-shadow:0 10px 40px rgba(0,0,0,.3)}
 .modal h3{margin:.1rem 0 .8rem;font-size:1.1rem}
 .modal .field{margin:0 0 .7rem}
 .modal .field label{display:block;font-weight:600;padding:0 0 .2rem}
 .modal .field input[type=text],.modal .field select{width:100%}
 .modal .checks label{display:inline-flex;gap:.4rem;margin-right:1.2rem;font-weight:400}
 .modal .actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem;align-items:center}
 .modal .actions .spacer{flex:1}
 .modal .err{color:#8c2f10;font-weight:600;min-height:1.2rem}
 .varrow{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.35rem 0}
 .varrow input.small{width:6rem}.varrow input.tiny{width:4rem}
 code.tok{background:#efece4;border-radius:4px;padding:.05rem .3rem;font-size:.85em}
 .preview{background:#f2eee4;border:1px solid #d7d4cc;border-radius:6px;
          padding:.4rem .6rem;font:13px/1.5 ui-monospace,monospace;word-break:break-all}
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

<form id="form" onsubmit="return false">
 <fieldset><legend>1. Choose volumes / issues</legend>
  <div id="sets"></div>
  <textarea id="setsjson" hidden aria-label="The volume / issue rows, kept for the next launch"></textarea>
  <button type="button" class="secondary" id="addset">+ Add another volume / issue</button>
  <p class="hint" id="regnote"></p>
  <p class="hint">Leave Iss. blank to list every issue of a volume. Titles
  registered as a <em>yearly series</em> take a Year instead. The contents
  come from the instance's public OAI-PMH feed, which holds posted records
  only; this module never attaches to Chrome.</p>
  <button type="button" id="listgo">List contents</button>
 </fieldset>

 <fieldset><legend>2. Select and order</legend>
  <p class="stephint">Tick the items that are paginated and put them in
  order: <strong>Up</strong>, <strong>Down</strong>, or a position and
  <strong>Move</strong>. The list starts in the order of each item's label
  in the feed; nothing is assumed from its document type. Numbering:
  <em>restart</em> starts the issue at its first page number;
  <em>continue</em> goes on from the previous issue of its volume listed
  here, unless a first page number is entered. Cover pages: how many pages
  Digital Commons adds to the published copy (normally 1); each item is
  checked against it. Pages by hand: only for an item with no PDF.</p>
  <div id="arr"></div>
  <textarea id="arrangement" hidden aria-label="The arrangement, kept for the next launch"></textarea>
  <p id="arrmsg" class="sr" role="status" aria-live="polite"></p>
 </fieldset>

 <fieldset><legend>3. Get the PDFs</legend>
  <p class="stephint">Writes a workbook for the Batch File Downloader: the
  stamped copy and the unstamped file of each ticked item. Run it there
  (re-run a report or plan, and choose this file); the downloads stay on
  the downloader's monitored browser route.</p>
  <label for="plandir">Save the download plan to</label>
  <span class="browse-row"><input type="text" id="plandir" data-path="folder" value="" size="40"
         autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="plandir" data-title="Save the download plan to" aria-label="Browse for Save the download plan to" hidden>Browse…</button></span><span class="browse-msg" id="plandir-browsemsg" role="status" aria-live="polite"></span>
  <p><button type="button" id="plango">Write download plan</button></p>
 </fieldset>

 <fieldset><legend>4. Count and number</legend>
  <p><label for="rundir">Downloader run folder</label>
  <span class="browse-row"><input type="text" id="rundir" data-path="folder" value="" size="40"
         autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="rundir" data-title="Downloader run folder" aria-label="Browse for Downloader run folder" hidden>Browse…</button></span><span class="browse-msg" id="rundir-browsemsg" role="status" aria-live="polite"></span></p>
  <p class="hint">The DC_FileDownloads_… folder the downloader wrote for
  the plan. Its reports are read from it and from the structure folders
  inside it; the reports folder below is needed only if the downloader was
  told to save its reports somewhere else.</p>
  <p><label for="rptdir">Reports folder, if saved elsewhere</label>
  <span class="browse-row"><input type="text" id="rptdir" data-path="folder" value="" size="40"
         autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="rptdir" data-title="Reports folder, if saved elsewhere" aria-label="Browse for Reports folder, if saved elsewhere" hidden>Browse…</button></span><span class="browse-msg" id="rptdir-browsemsg" role="status" aria-live="polite"></span></p>
  <p><label for="outdir">Save report to</label>
  <span class="browse-row"><input type="text" id="outdir" data-path="folder" value="" size="40"
         autocomplete="off" spellcheck="false"><button type="button" class="small secondary" data-browse="folder" data-for="outdir" data-title="Save report to" aria-label="Browse for Save report to" hidden>Browse…</button></span><span class="browse-msg" id="outdir-browsemsg" role="status" aria-live="polite"></span></p>
  <button type="button" id="go">Count and number</button>
 </fieldset>
</form>

<div id="status" role="status" aria-live="polite">Idle — choose a journal, volume and issue, then click List contents.</div>

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
/* DC-ISSUE 1: the journal / volume / issue controls of one row, shared by
   the DOI & XML Generator and the Page Number Generator and duplicated
   verbatim. The page defines REG (the journal registry as /api/journals
   sends it) and esc() before using these. issueRowHtml() is the row's
   controls, issueRowWire(div, changed) their behavior, issueRowRead(div)
   their values, and issueEntry(div) the registry entry they name. */
function journalOptions(sel){
  let h=REG.journals.map(j=>'<option value="'+esc(j.id)+'"'+(j.id===sel?' selected':'')+'>'
      +esc(j.name)+' ('+esc(j.id)+')</option>').join('');
  h+='<option value="__custom__"'+(sel==='__custom__'?' selected':'')+'>Custom structure ID…</option>';
  return h;
}
function issueRowHtml(){
  return '<label>Journal <select name="journal" aria-label="Journal">'+journalOptions()+'</select></label>'+
   '<input type="text" name="customid" class="small" placeholder="custom id" aria-label="Custom structure ID" style="display:none">'+
   '<label class="syearlab">Year <input type="text" name="syear" class="tiny" inputmode="numeric" aria-label="Series year"></label>'+
   '<label>Vol. <input type="text" name="volume" class="tiny" inputmode="numeric" aria-label="Volume number"></label>'+
   '<label class="isslab">Iss. <input type="text" name="issue" class="tiny" inputmode="numeric" aria-label="Issue number"></label>';
}
function issueEntry(div){
  let jid=div.querySelector('[name=journal]').value;
  if(jid==='__custom__') jid=div.querySelector('[name=customid]').value.trim().toLowerCase();
  return (REG.journals||[]).find(j=>j.id===jid)||null;
}
function issueRowWire(div, changed){
  const sel=div.querySelector('[name=journal]'), cid=div.querySelector('[name=customid]');
  sel.addEventListener('change',()=>{
    cid.style.display=sel.value==='__custom__'?'inline-block':'none';
    changed();
  });
  cid.addEventListener('input',()=>changed());
}
function issueRowRead(div){
  const v=n=>div.querySelector('[name='+n+']').value.trim();
  let jid=v('journal'); if(jid==='__custom__') jid=v('customid').toLowerCase();
  return {journal:jid,volume:v('volume'),issue:v('issue'),syear:v('syear')};
}
/* /DC-ISSUE */

let REG={journals:[]}, SETID=0, ARR=null;
const esc=t=>String(t).split('&').join('&amp;').split('<').join('&lt;')
  .split('>').join('&gt;').split('"').join('&quot;');
const setsBox=document.getElementById("sets"), arrBox=document.getElementById("arr"),
      arrMsg=document.getElementById("arrmsg"), setsField=document.getElementById("setsjson"),
      arrField=document.getElementById("arrangement"), listGo=document.getElementById("listgo"),
      planGo=document.getElementById("plango"), countGo=document.getElementById("go");

/* ---- 1. the volume / issue rows (DC-ISSUE controls) ---- */
function syncYearly(div){
  const e=issueEntry(div);
  div.classList.toggle('yearly',!!(e&&e.structure==='yearly'));
}
function readSets(){
  const out=[];
  for(const div of setsBox.children) out.push(issueRowRead(div));
  return out;
}
function saveSets(){ setsField.value=JSON.stringify(readSets()); saveForm(); }
function setRow(div,r){
  const sel=div.querySelector('[name=journal]'), cid=div.querySelector('[name=customid]');
  if((REG.journals||[]).some(j=>j.id===r.journal)) sel.value=r.journal;
  else if(r.journal){ sel.value='__custom__'; cid.value=r.journal; cid.style.display='inline-block'; }
  ['volume','issue','syear'].forEach(n=>{ div.querySelector('[name='+n+']').value=r[n]||''; });
}
function addSet(init){
  const div=document.createElement('div');
  div.className='set'; div.dataset.sid=String(++SETID);
  div.innerHTML='<div class="row">'+issueRowHtml()+
    '<button type="button" class="linkish" data-remove>Remove</button></div>';
  setsBox.appendChild(div);
  if(init) setRow(div,init);
  issueRowWire(div,()=>{ syncYearly(div); saveSets(); });
  ['volume','issue','syear','customid'].forEach(n=>{
    div.querySelector('[name='+n+']').addEventListener('input',saveSets);
  });
  div.querySelector('[data-remove]').addEventListener('click',()=>{
    if(setsBox.children.length>1){ div.remove(); saveSets(); }
  });
  syncYearly(div);
}
document.getElementById("addset").addEventListener('click',()=>{ addSet(); saveSets(); });

/* ---- 2. the arrangement ---- */
/* Move the item at index `from` to index `to` (both 0-based, clamped) and
   return the index it landed on. Pure, so the page's own copy can be driven
   in node. */
function arrMove(items, from, to){
  if(from<0||from>=items.length) return from;
  to=Math.max(0,Math.min(items.length-1,to));
  const it=items.splice(from,1)[0];
  items.splice(to,0,it);
  return to;
}
/* Whether an issue continues from an earlier issue of its volume in the
   arrangement — what a blank First page number means for it. Pure. */
function hasEarlier(issues, gi){
  const g=issues[gi];
  for(let k=0;k<gi;k++){
    const h=issues[k];
    if(h.journal===g.journal&&(h.syear||'')===(g.syear||'')&&(h.volume||'')===(g.volume||'')) return true;
  }
  return false;
}
function issueName(g){
  let d=g.name||g.journal;
  if(g.syear) d+=' '+g.syear;
  if(g.volume) d+=', vol. '+g.volume;
  if(g.issue) d+=', iss. '+g.issue;
  return d;
}
function pagesNow(it){
  if(!it.fpage&&!it.lpage) return 'none';
  return (it.fpage||'?')+'–'+(it.lpage||'?');
}
function arrSave(){ arrField.value=ARR?JSON.stringify(ARR):''; saveForm(); }
function firstHint(sec,g,gi){
  const f=sec.querySelector('[data-f=first]');
  f.placeholder=(g.mode==='continue'&&hasEarlier(ARR.issues,gi))?'after the previous issue':'1';
}
function itemName(it){ return 'article '+it.articleid+(it.label?' (label '+it.label+')':''); }
function renderArr(){
  if(!ARR||!ARR.issues||!ARR.issues.length){
    arrBox.innerHTML='<p class="hint">Nothing listed yet: choose a volume / issue above and click List contents.</p>';
    return;
  }
  let h='';
  ARR.issues.forEach((g,gi)=>{
    const nm=esc(issueName(g)), n=g.items.length;
    h+='<section class="issue" data-gi="'+gi+'"><h3>'+nm+' ('+n+' item'+(n===1?'':'s')+')</h3>'+
     '<div class="row"><label>Numbering <select data-f="mode" aria-label="Numbering for '+nm+'">'+
     '<option value="restart">restart</option><option value="continue">continue from the previous issue</option></select></label>'+
     '<label>First page number <input type="text" class="wide" inputmode="numeric" data-f="first" aria-label="First page number for '+nm+'"></label>'+
     '<label>Cover pages <input type="text" class="tiny" inputmode="numeric" data-f="cover" aria-label="Cover pages for '+nm+'"></label></div>'+
     '<table class="items"><caption class="sr">Items of '+nm+'</caption><thead><tr>'+
     '<th scope="col">Paginate</th><th scope="col">Position</th><th scope="col">Label</th>'+
     '<th scope="col">Type</th><th scope="col">Title</th><th scope="col">PDF</th>'+
     '<th scope="col">Pages now</th><th scope="col">Pages by hand</th><th scope="col">Move</th></tr></thead><tbody>';
    g.items.forEach((it,ii)=>{
      const inm=esc(itemName(it)), title=esc(it.title||'(no title)');
      h+='<tr data-ii="'+ii+'"'+(it.on?'':' class="off"')+'>'+
       '<td><input type="checkbox" data-f="on"'+(it.on?' checked':'')+' aria-label="Paginate '+inm+'"></td>'+
       '<td>'+(ii+1)+'</td><td>'+esc(it.label)+'</td><td>'+esc(it.doctype)+'</td>'+
       '<td>'+(it.landing?'<a href="'+esc(it.landing)+'" target="_blank" rel="noopener">'+title+'</a>':title)+
       (it.authors?'<br><span class="hint">'+esc(it.authors)+'</span>':'')+'</td>'+
       '<td>'+(it.fulltext?'yes':'no')+'</td><td>'+esc(pagesNow(it))+'</td>'+
       '<td><input type="text" class="tiny" inputmode="numeric" data-f="hand" value="'+esc(it.hand||'')+'" aria-label="Pages by hand for '+inm+'"></td>'+
       '<td class="mv"><button type="button" class="small secondary" data-mv="up" aria-label="Move '+inm+' up"'+(ii===0?' disabled':'')+'>Up</button> '+
       '<button type="button" class="small secondary" data-mv="down" aria-label="Move '+inm+' down"'+(ii===n-1?' disabled':'')+'>Down</button> '+
       '<label>to <input type="text" class="tiny" inputmode="numeric" data-f="to" aria-label="New position for '+inm+'"></label>'+
       '<button type="button" class="small secondary" data-mv="to" aria-label="Move '+inm+' to the position entered">Move</button></td></tr>';
    });
    h+='</tbody></table></section>';
  });
  arrBox.innerHTML=h;
  arrBox.querySelectorAll('section.issue').forEach(sec=>{
    const gi=+sec.dataset.gi, g=ARR.issues[gi];
    sec.querySelector('[data-f=mode]').value=g.mode;
    sec.querySelector('[data-f=first]').value=g.first||'';
    sec.querySelector('[data-f=cover]').value=String(g.cover);
    firstHint(sec,g,gi);
  });
}
function arrWhere(el){
  const sec=el.closest('section.issue'), tr=el.closest('tr[data-ii]');
  return {sec:sec, gi:sec?+sec.dataset.gi:-1, ii:tr?+tr.dataset.ii:-1};
}
function arrField_(e){
  const el=e.target, f=el.getAttribute&&el.getAttribute('data-f');
  if(!f||!ARR) return;
  const w=arrWhere(el); if(w.gi<0) return;
  const g=ARR.issues[w.gi];
  if(f==='on'){ g.items[w.ii].on=el.checked; el.closest('tr').classList.toggle('off',!el.checked); }
  else if(f==='hand'){ g.items[w.ii].hand=el.value.trim(); }
  else if(f==='mode'){ g.mode=el.value; arrBox.querySelectorAll('section.issue').forEach(s=>firstHint(s,ARR.issues[+s.dataset.gi],+s.dataset.gi)); }
  else if(f==='first'){ g.first=el.value.trim(); }
  else if(f==='cover'){ g.cover=el.value.trim(); }
  else return;
  arrSave();
}
arrBox.addEventListener('change',arrField_);
arrBox.addEventListener('input',e=>{ const f=e.target.getAttribute&&e.target.getAttribute('data-f'); if(f==='hand'||f==='first'||f==='cover') arrField_(e); });
function arrMoveFrom(btn){
  const w=arrWhere(btn), g=ARR.issues[w.gi], how=btn.getAttribute('data-mv');
  let to=w.ii;
  if(how==='up') to=w.ii-1;
  else if(how==='down') to=w.ii+1;
  else{
    const v=btn.closest('td').querySelector('[data-f=to]').value.trim();
    if(!/^[0-9]+$/.test(v)){ showErr('Enter the new position as a number from 1 to '+g.items.length+'.'); return; }
    to=parseInt(v,10)-1;
  }
  const it=g.items[w.ii], at=arrMove(g.items,w.ii,to);
  renderArr(); arrSave();
  const row=arrBox.querySelector('section[data-gi="'+w.gi+'"] tr[data-ii="'+at+'"]');
  if(row){
    let b=row.querySelector('[data-mv="'+how+'"]');
    if(!b||b.disabled) b=row.querySelector('[data-mv="'+(how==='up'?'down':'up')+'"]');
    if(b&&!b.disabled) b.focus();
  }
  arrMsg.textContent=itemName(it)+' is now at position '+(at+1)+' of '+g.items.length+' in '+issueName(g)+'.';
}
arrBox.addEventListener('click',e=>{
  const b=e.target.closest&&e.target.closest('[data-mv]');
  if(b&&ARR){ clearErr(); arrMoveFrom(b); }
});
arrBox.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.getAttribute&&e.target.getAttribute('data-f')==='to'){
    e.preventDefault();
    const b=e.target.closest('td').querySelector('[data-mv="to"]');
    if(b){ clearErr(); arrMoveFrom(b); }
  }
});

/* ---- the three runs ---- */
async function post(url,body){
  let r, j={};
  try{
    r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    try{ j=await r.json(); }catch(e){}
  }catch(e){ return {ok:false,error:'The module did not answer. Is it still running?'}; }
  return {ok:r.ok,error:j.error||'The module refused the request.'};
}
async function begin(url,body){
  clearErr();
  [listGo,planGo,countGo].forEach(b=>{ b.disabled=true; });
  const r=await post(url,body);
  if(!r.ok){ showErr(r.error); [listGo,planGo,countGo].forEach(b=>{ b.disabled=false; }); return; }
  poll();
}
listGo.addEventListener('click',()=>begin('/api/list',{sets:readSets()}));
planGo.addEventListener('click',()=>begin('/api/plan',{arrangement:ARR,
  out_dir:document.getElementById("plandir").value.trim()}));
countGo.addEventListener('click',()=>begin('/api/start',{arrangement:ARR,
  run_dir:document.getElementById("rundir").value.trim(),
  report_dir:document.getElementById("rptdir").value.trim(),
  out_dir:document.getElementById("outdir").value.trim()}));

/* What the status line calls each running phase. */
function phaseWord(phase){
  return ({starting:'Starting', listing:'Listing contents',
    planning:'Writing the download plan', counting:'Counting pages'})[phase]||phase;
}
function baseName(p){ const s=String(p||'').split('/').join(String.fromCharCode(92)).split(String.fromCharCode(92)); return s[s.length-1]; }
const RUNNING=['starting','listing','planning','counting'];
async function poll(){
  let s;
  try{ s=await (await fetch('/api/state',{cache:'no-store'})).json(); }catch(e){ return; }
  renderLog(s.log); formSeen(s); logStateSeen(s);
  if(s.listing&&s.listing.listed&&(!ARR||ARR.listed!==s.listing.listed)){
    ARR=JSON.parse(JSON.stringify(s.listing)); renderArr(); arrSave();
  }
  const busy=RUNNING.indexOf(s.phase)>=0;
  [listGo,planGo,countGo].forEach(b=>{ b.disabled=busy; });
  const pr=s.progress||{};
  const p=pr.total?(' ('+pr.current+'/'+pr.total+')'):'';
  const m=pr.msg?' — '+pr.msg:'';
  if(s.phase==='done'){
    let h='Done: '+esc(s.summary||'');
    if(s.link_file) h+='<br>'+esc(s.link_label||'File')+': <a href="/download" data-path="'+
      logAttr(s.link_file)+'">'+esc(baseName(s.link_file))+'</a>';
    logShow(logTone(s),h);
  }else if(s.phase==='error'){
    logShow('error',esc('Error: '+(s.summary||'')));
  }else if(s.phase==='idle'){
    logShow('',esc('Idle — choose a journal, volume and issue, then click List contents.'));
  }else{
    logShow('',esc(phaseWord(s.phase)+'…'+p+m));
  }
}
/* DC-FORM's hook: the rows and the arrangement come back from their
   hidden fields, as the page left them. */
function formRestoredHook(form){
  let rows=[];
  try{ rows=JSON.parse(setsField.value||'[]'); }catch(e){ rows=[]; }
  setsBox.innerHTML='';
  if(Array.isArray(rows)&&rows.length) rows.forEach(r=>addSet(r)); else addSet();
  try{ ARR=arrField.value?JSON.parse(arrField.value):null; }catch(e){ ARR=null; }
  renderArr();
}
(async function init(){
  try{ REG=await (await fetch('/api/journals',{cache:'no-store'})).json(); }catch(e){}
  if(!REG||!Array.isArray(REG.journals)) REG={journals:[],note:''};
  document.getElementById("regnote").textContent=REG.note||'';
  renderArr();
  poll(); setInterval(poll,1500);
})();
</script>
</body></html>"""


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


def contrast_text(hex_color: str) -> str:
    """White or black header text, whichever meets contrast on hex_color."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if lum > 140 else "#ffffff"


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
    base_url = session.get("base_url", "").rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

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
            if self.path == "/api/journals":
                return self._json(load_journals())
            if self.path == "/download":
                with LOCK:
                    path = STATE["link_file"]
                if not path or not os.path.isfile(path):
                    return self._json({"error": "No file available."}, 404)
                data = Path(path).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxml"
                                 "formats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition",
                                 'attachment; filename="{}"'
                                 .format(os.path.basename(path)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
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
            if self.path in ("/api/list", "/api/plan", "/api/start"):
                return self.start(self.path)
            return self._json({"error": "Not found."}, 404)

        def start(self, path):
            with LOCK:
                if STATE["phase"] in RUNNING_PHASES:
                    return self._json({"error": "A run is already active."}, 409)
            try:
                req = self._read_json()
                if not isinstance(req, dict):
                    raise ValueError("not an object")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "Bad request."}, 400)
            if path == "/api/list":
                journals = load_journals()["journals"]
                refs, msg = validate_sets(req.get("sets"), journals)
                if msg:
                    return self._json({"error": msg}, 400)
                if not base_url:
                    return self._json({"error": "No instance address is set; "
                                       "launch this module from the suite."},
                                      400)
                target, args = list_worker, (session, refs, journals)
            elif path == "/api/plan":
                out_dir = os.path.expanduser(str(req.get("out_dir", "")).strip())
                err_ = folder_error("Save the download plan to", out_dir)
                if err_:
                    return self._json({"error": err_}, 400)
                arr, msg = validate_arrangement(req.get("arrangement"),
                                                base_url)
                if msg:
                    return self._json({"error": msg}, 400)
                target, args = plan_worker, (session, arr, out_dir)
            else:
                run_dir = os.path.expanduser(str(req.get("run_dir", "")).strip())
                rpt_dir = os.path.expanduser(str(req.get("report_dir", "")).strip())
                out_dir = os.path.expanduser(str(req.get("out_dir", "")).strip())
                for label, value, optional in (
                        ("Downloader run folder", run_dir, False),
                        ("Reports folder, if saved elsewhere", rpt_dir, True),
                        ("Save report to", out_dir, False)):
                    err_ = folder_error(label, value, optional)
                    if err_:
                        return self._json({"error": err_}, 400)
                arr, msg = validate_arrangement(req.get("arrangement"),
                                                base_url)
                if msg:
                    return self._json({"error": msg}, 400)
                target, args = count_worker, (session, arr, run_dir,
                                              rpt_dir, out_dir)
            if not start_run(target, args):
                return self._json({"error": "A run is already active."}, 409)
            return self._json({"ok": True})

        def log_message(self, *a):
            pass

    return Handler


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    global REGISTRY_PATH
    parser = argparse.ArgumentParser(description=MANIFEST["name"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--session", default=str(DEFAULT_SESSION))
    args = parser.parse_args()

    session = load_session(Path(args.session))
    REGISTRY_PATH = Path(args.session).resolve().parent / "journals.json"
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
