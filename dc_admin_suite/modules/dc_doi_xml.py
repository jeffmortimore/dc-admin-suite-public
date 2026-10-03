#!/usr/bin/env python3
"""DOI & XML Generator v1.5 — DC Admin Suite module.

For one or more journal volumes / issues, this module can:

  1. MINT DOIs for manual addition to articles in Digital Commons.
     Format:  PREFIX/ID.YYYY.VVIINN   (e.g. 10.12345/cimle.2019.240106)
     VV/II are zero-padded to two digits and omitted when the volume or
     issue number is not provided; NN starts at 01. DOIs are written to a
     text file, one per line.

  2. OUTPUT XML by harvesting the instance's public OAI-PMH endpoint
     (metadataPrefix=document-export) and crosswalking to:
        * bepress XML          — the raw OAI response, saved as-is
        * Crossref deposit XML — schema 5.4.0 (journal or conference
                                 markup per the journal registry)
        * DOAJ article XML     — DOAJ native schema (doajArticles.xsd)
     If the harvested records lack DOIs, the Crossref/DOAJ crosswalks for
     that set fail with a warning (the bepress XML is still saved).

If a run both mints DOIs and outputs XML, it pauses after minting so the
DOIs can be added and saved in Digital Commons, then continues (or ends)
on the user's signal.

Journal registry: journal names, ISSNs, and Crossref record types live in
config/journals.json (created on first run; the shipped default carries
no institution's prefix or depositor identity) and are editable in the
module UI — no code changes needed to add or correct a journal.

Adapted from the standalone tkinter "journal_xml_generator" tool. Changes
for the suite architecture:
  * MANIFEST + --port/--session module contract; branded browser UI.
  * base_url comes from config/session.json — nothing is hard-coded.
  * Crossref schema 4.4.x → 5.4.0 (note the 5.3.1+ breaking change:
    <affiliation> became <affiliations><institution><institution_name>).
  * OAI resumptionToken paging supported; OAI <error> responses surfaced.
  * Multiple volumes / issues per run; per-item diagnostics in the log.
  * Pure standard library — no selenium (the OAI endpoint is public), no
    Chrome attachment, no third-party packages.

v1.1 (2026-07) — feedback revisions:
  * UI drops the word "set(s)" in favour of "Volume / issue" / "Volumes /
    issues" (internal identifiers keep the older names).
  * Journal registry gains per-title DOI and DOAJ feature toggles. With
    DOI off a title cannot mint DOIs or produce Crossref XML; with DOAJ
    off it cannot produce DOAJ XML; with both off only bepress XML is
    available. Enforced in the UI (greyed controls + tooltip) and again
    server-side at /api/start.
  * DOI suffixes are now built from a customizable, token-based pattern
    (built-ins {id} {year} {volume} {issue} {seq} plus user-defined fixed
    and sequential variables). Named patterns live in a shared library in
    the registry; each journal may point at one as its default. The
    factory "Default" pattern reproduces the old PREFIX/ID.YYYY.VVIINN
    output exactly and pre-populates the pattern editor.
  * The registry is edited through a summary table + modal dialog (click a
    row to edit; "Add journal" opens a blank modal).

v1.2 (2026-07) — the Crossref DOI prefix is now a shell-level setting.
  It arrives in session.json as "crossref_prefix" (set/verified on the
  suite's splash and Settings pages) and is the authoritative prefix for
  minting: the session value wins, the registry "prefix" field is only a
  standalone fallback for older/hand-run sessions. The pattern preview
  shows the session prefix, and /api/start rejects a minting run when no
  valid prefix is available.

v1.3 (2026-07) — conference communities with per-year series (AMTP, PMHR).
  Some proceedings are not an ir_journal or ir_event_community but an
  ir_community holding one ir_series per year (amtp-proceedings_2026,
  pmhr_2025, …). Changes:
  * The registry gains a per-title "structure" field: "volume_issue"
    (default — the classic journal shape) or "yearly" (community of
    per-year series). Yearly titles take a Year in the run form instead
    of volume / issue, and the OAI harvest targets the per-year series
    set publication:{id}_{year}. Volume stays available (it maps to
    Crossref <volume> in series markup); issue does not apply.
  * Crossref conference markup: <proceedings_title> now prefers the
    harvested <publication-title> (e.g. "AMTP Proceedings 2026", "17th
    IMHRC Proceedings (Trondheim, Norway-2025)"), falling back to the
    registry name (+ year for yearly titles).
  * Conference entries WITHOUT an eISSN now deposit with
    <proceedings_metadata> + <noisbn> instead of the ISSN-keyed
    <proceedings_series_metadata>, so unregistered proceedings (PMHR)
    can still produce valid Crossref XML. Journal-type Crossref and all
    DOAJ output still require an eISSN.
  * AMTP Proceedings (eISSN 2694-1821) and Progress in Material
    Handling Research added to the default registry as yearly
    conference titles.

Run standalone:  python3 modules/dc_doi_xml.py --port 8799 \
                     --session config/session.json
Or launch it from the Main Menu.

v1.4 (2026-07) — hardening pass for public distribution:
  * Local HTTP endpoints now reject non-local requests (Host/Origin guard
    against DNS rebinding and cross-site POSTs).
  * The "writing" phase now starts after a set's OAI harvest completes, so
    the UI shows "Harvesting…" during the actual harvest.
  * If OAI paging is still incomplete after the 50-page safety cap, the set
    fails with a clear error instead of writing a truncated deposit file.

v1.6 (2026-09-04) — identity refactor Step 2: the Crossref DEPOSIT IDENTITY
is configuration, not source.
  * depositor_name, depositor_email, registrant and publisher no longer have
    built-in values naming one institution. They are a shell-level setting
    (Settings > Crossref deposit identity), delivered in session.json and
    resolved by effective_crossref(session, reg) — the session block wins,
    the registry's own fields remain a standalone fallback, exactly the
    precedent effective_prefix set for the DOI prefix. An existing install
    keeps depositing unchanged from its saved config/journals.json.
  * With neither source filled in, a deposit is REFUSED rather than built:
    /api/start rejects the run up front naming the missing fields, and
    crossref_head / the publisher sites raise as a backstop. Only what each
    output needs is required — a journal Crossref deposit carries no
    publisher, so a blank one does not block it; conference proceedings and
    DOAJ do need it. An empty required field is a much better failure than
    a silent wrong one: a deposit credited to the wrong organization is
    very hard to notice and awkward to undo.

v1.7 (2026-09-04) — identity refactor Step 3: this module no longer
carries an institution. The standalone session fallback (used only when
config/session.json is missing) is now blank rather than one institution's
URL and name, and the branding color fallbacks read the neutral
FALLBACK_BRAND constant. Real values come from the shell, which seeds them
from config/profile.json — see profiles/README.md.

v1.8 (2026-09-04) — identity refactor Step 4: the default journal registry
ships empty. It used to carry 16 real journals with their eISSNs, so a fresh
install anywhere came up pre-loaded with one institution's titles — the
identity grep never caught it because journal names do not match the
pattern. Staff add their own in the registry editor; a prepared set is one
file away (copy profiles/<institution>-journals.json to
config/journals.json). An existing install is untouched: its own
config/journals.json wins.
"""

# ---------------------------------------------------------------------------
# MANIFEST — read by the shell with `ast`; keep it a dict of literals.
# ---------------------------------------------------------------------------
MANIFEST = {
    "id": "doi-xml-generator",
    "name": "DOI & XML Generator",
    "description": "Mint article DOIs and output bepress, Crossref 5.4.0, and DOAJ XML for journal issues and yearly proceedings.",
    "version": "1.8.2",
    "requires": [],
}

import argparse
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
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.dom import minidom

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
            "crossref_prefix": "",
            "chrome": {"host": "127.0.0.1", "port": 9222,
                       "debugger_address": "127.0.0.1:9222"},
            "branding": {"suite_name": "DC Admin Suite",
                         "institution": "",
                         "colors": {"primary": FALLBACK_BRAND["primary"],
                                    "accent": FALLBACK_BRAND["accent"],
                                    "neutral": FALLBACK_BRAND["neutral"]}},
        }


# ---------------------------------------------------------------------------
# Journal registry — config/journals.json (next to session.json, so it
# travels with the suite). Created with these defaults on first run;
# edited via the UI's "Journal registry" panel, never by hand-editing code.
#
# Fields per journal:
#   id              DC structure ID (the OAI publication slug)
#   name            full journal / proceedings title
#   abbrev          short title or acronym ("" = omit from Crossref)
#   eissn           electronic ISSN, hyphenated ("" = Crossref/DOAJ blocked)
#   type            "journal" | "conference"  (Crossref record markup)
#   structure       "volume_issue" (default) | "yearly" — yearly titles are
#                   an ir_community with one ir_series per year; harvests
#                   target publication:{id}_{year} instead of vol/iss sets
#   conference_name event name (conference type only)
#   doi_enabled     bool — off blocks minting AND Crossref for this title
#   doaj_enabled    bool — off blocks DOAJ XML for this title
#   doi_pattern     name of a saved suffix pattern ("" = use suite default)
# (doi_enabled / doaj_enabled / doi_pattern / structure are backfilled to
#  sensible defaults on load, so registries written by earlier versions
#  upgrade automatically.)
#
# Shared DOI suffix patterns live at the top level:
#   doi_patterns    list of {name, template, variables:[...]} definitions
#   default_pattern name of the suite-wide default pattern
# A pattern's template is built from tokens: built-ins {id} {year}
# {volume} {issue} {seq} (append :N to zero-pad a numeric token to width
# N; volume/issue render empty when blank) plus any custom variables. A
# custom variable is either {"kind":"fixed","value":"S"} (a constant) or
# {"kind":"seq","start":1,"pad":2} (a per-article counter).
# ---------------------------------------------------------------------------
DEFAULT_PATTERN_TEMPLATE = "{id}.{year}.{volume:2}{issue:2}{seq:2}"
DEFAULT_PATTERN_NAME = "Default (volume / issue / article)"

DEFAULT_REGISTRY = {
    # Empty by design (identity refactor Step 3): the shell-level
    # crossref_prefix is authoritative, and a built-in value here would be
    # the one thing effective_prefix() falls back to — silently minting
    # another institution's DOIs under the prefix compiled in here.
    # With no prefix anywhere, /api/start refuses to mint.
    "prefix": "",
    # Crossref DEPOSIT IDENTITY — empty by design (identity refactor Step 2).
    # These name a specific institution, so a built-in default would credit
    # another institution's deposits to whoever was compiled in here. The
    # authoritative source is the shell (Settings > Crossref deposit
    # identity, arriving via session.json); these registry fields remain as
    # a standalone fallback and keep working for an install whose saved
    # journals.json already carries them. With neither, a Crossref deposit
    # is REFUSED rather than built with a wrong or blank depositor.
    "depositor_name": "",
    "depositor_email": "",
    "registrant": "",
    "publisher": "",
    "doi_patterns": [
        {"name": DEFAULT_PATTERN_NAME, "template": DEFAULT_PATTERN_TEMPLATE,
         "variables": []},
    ],
    "default_pattern": DEFAULT_PATTERN_NAME,
    # No journals by design (identity refactor Step 4). This registry used to
    # ship 16 real titles with their eISSNs, so a fresh install anywhere came
    # up pre-loaded with one institution's journals. Staff add their own in
    # the module's registry editor; a prepared set is one file away — copy
    # the matching profiles/<institution>-journals.json to
    # config/journals.json. An existing install is unaffected: its own
    # config/journals.json wins.
    "journals": [],
}

REGISTRY_PATH = None  # set in main() from the session path
REGISTRY_LOCK = threading.Lock()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dXx]$")
PREFIX_RE = re.compile(r"^10\.\d{3,9}$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
YEAR_RE = re.compile(r"^\d{4}$")
# A suffix-pattern token: {name} or {name:N} (N = zero-pad width).
TOKEN_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)(?::(\d+))?\}")
VARNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
BUILTIN_TOKENS = ("id", "year", "volume", "issue", "seq")


def backfill_registry(reg: dict) -> dict:
    """Add fields introduced after v1.0 so older journals.json files (and
    any hand-edited registry) always expose the current shape. Mutates and
    returns reg. Defaults preserve v1.0 behaviour: every title keeps DOI
    and DOAJ enabled and uses the suite default suffix pattern."""
    if not reg.get("doi_patterns"):
        reg["doi_patterns"] = [
            {"name": DEFAULT_PATTERN_NAME,
             "template": DEFAULT_PATTERN_TEMPLATE, "variables": []}]
    if not reg.get("default_pattern"):
        reg["default_pattern"] = reg["doi_patterns"][0].get(
            "name", DEFAULT_PATTERN_NAME)
    for j in reg.get("journals", []):
        if not isinstance(j, dict):
            continue
        j.setdefault("doi_enabled", True)
        j.setdefault("doaj_enabled", True)
        j.setdefault("doi_pattern", "")
        j.setdefault("structure", "volume_issue")
    return reg


def load_registry() -> dict:
    """Load config/journals.json, creating it with defaults if missing."""
    with REGISTRY_LOCK:
        try:
            reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(reg, dict) and isinstance(reg.get("journals"), list):
                return backfill_registry(reg)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        reg = json.loads(json.dumps(DEFAULT_REGISTRY))  # deep copy
        if REGISTRY_PATH is not None:
            try:
                REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
                REGISTRY_PATH.write_text(
                    json.dumps(reg, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass  # still usable in-memory
        return reg


def save_registry(reg: dict):
    with REGISTRY_LOCK:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(
            json.dumps(reg, indent=2) + "\n", encoding="utf-8")


def validate_registry(reg) -> str:
    """Return "" if reg is a valid registry, else a user-facing message."""
    if not isinstance(reg, dict) or not isinstance(reg.get("journals"), list):
        return "Registry must be an object with a 'journals' list."
    patterns = reg.get("doi_patterns", [])
    if not isinstance(patterns, list):
        return "'doi_patterns' must be a list."
    pat_names = set()
    for p in patterns:
        msg = validate_pattern(p)
        if msg:
            return msg
        pname = str(p.get("name", "")).strip()
        if pname in pat_names:
            return "Duplicate DOI pattern name: " + pname
        pat_names.add(pname)
    default_pattern = str(reg.get("default_pattern", "")).strip()
    if pat_names and default_pattern and default_pattern not in pat_names:
        return ("The suite default pattern {!r} is not in the pattern library."
                .format(default_pattern))
    seen = set()
    for j in reg["journals"]:
        if not isinstance(j, dict):
            return "Each journal must be an object."
        jid = str(j.get("id", "")).strip().lower()
        if not ID_RE.match(jid):
            return "Bad journal ID: {!r} (lowercase letters, digits, - _)".format(jid)
        if jid in seen:
            return "Duplicate journal ID: " + jid
        seen.add(jid)
        if not str(j.get("name", "")).strip():
            return "Journal '{}' needs a name.".format(jid)
        eissn = str(j.get("eissn", "")).strip()
        if eissn and not ISSN_RE.match(eissn):
            return "Journal '{}': bad ISSN {!r} (use NNNN-NNNN).".format(jid, eissn)
        if str(j.get("type", "journal")) not in ("journal", "conference"):
            return "Journal '{}': type must be journal or conference.".format(jid)
        if str(j.get("structure", "volume_issue")) not in ("volume_issue", "yearly"):
            return ("Journal '{}': structure must be volume_issue or yearly."
                    .format(jid))
        jpat = str(j.get("doi_pattern", "")).strip()
        if jpat and pat_names and jpat not in pat_names:
            return ("Journal '{}': DOI pattern {!r} is not in the pattern "
                    "library.".format(jid, jpat))
    return ""


# ---------------------------------------------------------------------------
# DOI minting — customizable, token-based suffix patterns
#
# A pattern is {"name","template","variables":[...]}. The template is any
# text with {token} / {token:N} placeholders. Built-in tokens: {id} {year}
# {volume} {issue} {seq}; :N zero-pads a numeric token to width N (volume,
# issue and seq default to width 2). volume/issue render to "" when blank —
# reproducing v1.0's "omit the digits" behaviour. Custom variables extend
# the token set: kind "fixed" injects a constant; kind "seq" is a per-
# article counter with its own start/pad. The factory default template
# "{id}.{year}.{volume:2}{issue:2}{seq:2}" reproduces PREFIX/ID.YYYY.VVIINN.
# ---------------------------------------------------------------------------
def validate_pattern(pat) -> str:
    """Return "" if pat is a usable suffix pattern, else a user-facing msg."""
    if not isinstance(pat, dict):
        return "A DOI pattern must be an object."
    name = str(pat.get("name", "")).strip()
    if not name:
        return "Every DOI pattern needs a name."
    template = str(pat.get("template", ""))
    if not template.strip():
        return "Pattern '{}' needs a template.".format(name)
    variables = pat.get("variables", [])
    if not isinstance(variables, list):
        return "Pattern '{}': variables must be a list.".format(name)
    custom = {}
    for v in variables:
        if not isinstance(v, dict):
            return "Pattern '{}': each variable must be an object.".format(name)
        vname = str(v.get("name", "")).strip()
        if not VARNAME_RE.match(vname):
            return ("Pattern '{}': bad variable name {!r} (letters, digits, "
                    "_; must start with a letter).".format(name, vname))
        if vname in BUILTIN_TOKENS:
            return ("Pattern '{}': {!r} is a built-in token, choose another "
                    "variable name.".format(name, vname))
        if vname in custom:
            return "Pattern '{}': duplicate variable {!r}.".format(name, vname)
        kind = str(v.get("kind", "")).strip()
        if kind == "fixed":
            if str(v.get("value", "")) == "":
                return ("Pattern '{}': fixed variable {!r} needs a value."
                        .format(name, vname))
        elif kind == "seq":
            for k in ("start", "pad"):
                try:
                    int(v.get(k))
                except (TypeError, ValueError):
                    return ("Pattern '{}': counter {!r} needs a numeric {}."
                            .format(name, vname, k))
        else:
            return ("Pattern '{}': variable {!r} kind must be 'fixed' or "
                    "'seq'.".format(name, vname))
        custom[vname] = v
    known = set(BUILTIN_TOKENS) | set(custom)
    for m in TOKEN_RE.finditer(template):
        if m.group(1) not in known:
            return ("Pattern '{}': unknown token {{{}}} in the template."
                    .format(name, m.group(1)))
    # Fail fast on stray unmatched braces that aren't valid tokens.
    stripped = TOKEN_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        return ("Pattern '{}': unbalanced or malformed {{token}} in the "
                "template.".format(name))
    return ""


def render_suffix(pattern, ctx, seq_index) -> str:
    """Render one DOI suffix. ctx = {id, year, volume, issue}; seq_index is
    the 1-based article number within the volume / issue."""
    custom = {v["name"]: v for v in pattern.get("variables", [])}

    def repl(m):
        name, pad = m.group(1), m.group(2)
        width = int(pad) if pad else None
        if name == "id":
            return str(ctx.get("id", ""))
        if name == "year":
            return str(ctx.get("year", ""))
        if name in ("volume", "issue"):
            val = str(ctx.get(name, "")).strip()
            if not val:
                return ""
            return str(int(val)).zfill(width if width is not None else 2)
        if name == "seq":
            return str(seq_index).zfill(width if width is not None else 2)
        v = custom.get(name)
        if v is None:
            return m.group(0)  # validated earlier; leave untouched
        if v.get("kind") == "fixed":
            return str(v.get("value", ""))
        start = int(v.get("start", 1))
        w = width if width is not None else int(v.get("pad", 2))
        return str(start + seq_index - 1).zfill(w)

    return TOKEN_RE.sub(repl, pattern["template"])


def resolve_pattern(reg, entry) -> dict:
    """Return the suffix pattern a journal should use: its assigned pattern,
    else the suite default, else the factory default."""
    patterns = {p.get("name"): p for p in reg.get("doi_patterns", [])
                if isinstance(p, dict)}
    want = (entry or {}).get("doi_pattern", "") or reg.get("default_pattern", "")
    if want and want in patterns:
        return patterns[want]
    if reg.get("default_pattern") in patterns:
        return patterns[reg["default_pattern"]]
    if patterns:
        return next(iter(patterns.values()))
    return {"name": DEFAULT_PATTERN_NAME, "template": DEFAULT_PATTERN_TEMPLATE,
            "variables": []}


def mint_dois(prefix, pattern, ctx, count):
    """Return the list of DOIs for a volume / issue using its suffix pattern."""
    return ["{}/{}".format(prefix, render_suffix(pattern, ctx, n))
            for n in range(1, count + 1)]


def effective_prefix(session, reg) -> str:
    """The Crossref prefix to mint with. The shell-level session value
    ("crossref_prefix", set on the suite's splash / Settings pages) is
    authoritative; the registry's own "prefix" is only a standalone
    fallback. There is no built-in default: with neither set the result is
    empty and /api/start refuses the run. A present-but-
    malformed session value is returned as-is so callers can reject it."""
    p = (session or {}).get("crossref_prefix", "")
    if not str(p).strip():
        p = (reg or {}).get("prefix", "") or DEFAULT_REGISTRY["prefix"]
    p = str(p).strip()
    if p.lower().startswith("doi:"):
        p = p[4:].strip()
    return p.rstrip("/")


# The Crossref deposit identity: who a deposit is credited to.
CROSSREF_IDENTITY_FIELDS = ("depositor_name", "depositor_email", "registrant",
                            "publisher")

# What each output actually needs. The Crossref head carries the depositor and
# registrant on every deposit; publisher appears only in conference
# proceedings_metadata and in DOAJ's <publisher>, so a journal-type Crossref
# deposit is not blocked for want of one.
CROSSREF_HEAD_FIELDS = ("depositor_name", "depositor_email", "registrant")

FIELD_LABELS = {
    "depositor_name": "depositor name",
    "depositor_email": "depositor email",
    "registrant": "registrant",
    "publisher": "publisher",
}


def effective_crossref(session, reg) -> dict:
    """The Crossref deposit identity to deposit under.

    Mirrors effective_prefix: the shell-level session block ("crossref", set
    on the suite's Settings page) is authoritative; the registry's own
    fields are only a standalone fallback, which is also what keeps an
    existing install working from its saved journals.json before anyone
    visits Settings. Resolved per field, so a half-filled Settings block
    does not silently discard the registry's remaining values.
    """
    sess = (session or {}).get("crossref") or {}
    out = {}
    for f in CROSSREF_IDENTITY_FIELDS:
        val = str(sess.get(f, "") or "").strip()
        if not val:
            val = str((reg or {}).get(f, "") or "").strip()
        out[f] = val
    return out


def missing_crossref_fields(ident, need_publisher: bool) -> list:
    """Which required identity fields are still blank, in UI order."""
    needed = list(CROSSREF_HEAD_FIELDS)
    if need_publisher:
        needed.append("publisher")
    return [f for f in needed if not (ident or {}).get(f, "").strip()]


def crossref_identity_error(missing: list, what: str) -> str:
    """One user-facing sentence naming what to fill in and where."""
    return ("Cannot build {}: the Crossref deposit identity is incomplete "
            "({} not set). Fill it in on the suite's Settings page under "
            "\"Crossref deposit identity\", then relaunch this module. "
            "Depositing with a blank or borrowed identity would credit the "
            "deposit to the wrong organization."
            .format(what, ", ".join(FIELD_LABELS[f] for f in missing)))


# ---------------------------------------------------------------------------
# OAI-PMH harvest (public endpoint; no authentication involved)
# ---------------------------------------------------------------------------
class SetError(Exception):
    """A per-set failure: logged as a warning; other sets still run."""


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


def fetch_url(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "DCAdminSuite-DOI-XML/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SetError("OAI request failed: HTTP {} {}".format(e.code, e.reason))
    except urllib.error.URLError as e:
        raise SetError("OAI request failed: {}".format(e.reason))


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


def parse_record(rec) -> dict:
    """Extract the fields the crosswalks need from one OAI <record>."""
    doi = ""
    for f in rec.iter():
        if local(f.tag) == "field" and f.attrib.get("name") == "doi":
            v = first(f, "value")
            if v is not None and v.text:
                doi = v.text.strip()
    authors, keywords = [], []
    for e in rec.iter():
        if local(e.tag) == "author":
            authors.append({
                "fname": text_of(e, "fname"), "mname": text_of(e, "mname"),
                "lname": text_of(e, "lname"), "suffix": text_of(e, "suffix"),
                "institution": text_of(e, "institution"),
                "organization": text_of(e, "organization"),
            })
        elif local(e.tag) == "keyword" and (e.text or "").strip():
            keywords.append(e.text.strip())
    # bepress double-escapes ampersands in exported URLs ("&amp;amp;"), so
    # after XML parsing the text still holds "&amp;" — unescape once more.
    unesc = html_lib.unescape
    return {
        "title": text_of(rec, "title"),
        "pubtitle": text_of(rec, "publication-title"),
        "abstract": text_of(rec, "abstract"),
        "fpage": text_of(rec, "fpage"),
        "lpage": text_of(rec, "lpage"),
        "coverpage": unesc(text_of(rec, "coverpage-url")),
        "fulltext": unesc(text_of(rec, "fulltext-url")),
        "native": unesc(text_of(rec, "native-url")),
        "pubdate": text_of(rec, "publication-date"),
        "doi": doi,
        "authors": authors,
        "keywords": keywords,
    }


def harvest(base_url, jid, vol, iss, logf, year=""):
    """Fetch all OAI pages for a set. Returns (raw_pages, records)."""
    pages, records = [], []
    url = oai_url(base_url, jid, vol, iss, year)
    for page_no in range(1, 51):  # sanity cap; issues are 1–2 pages
        logf("Fetching OAI page {}: {}".format(page_no, url))
        raw = fetch_url(url)
        pages.append(raw)
        try:
            tree = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as e:
            raise SetError("OAI response is not well-formed XML ({}).".format(e))
        err = first(tree, "error")
        if err is not None:
            raise SetError("OAI error [{}]: {}".format(
                err.attrib.get("code", "?"), (err.text or "").strip()
                or "no records for this volume/issue?"))
        for rec in tree.iter():
            if local(rec.tag) != "record":
                continue
            hdr = first(rec, "header")
            if hdr is not None and hdr.attrib.get("status") == "deleted":
                continue
            records.append(parse_record(rec))
        tok = first(tree, "resumptionToken")
        token = (tok.text or "").strip() if tok is not None else ""
        if not token:
            break
        url = "{}/do/oai/?verb=ListRecords&resumptionToken={}".format(
            base_url, urllib.parse.quote(token, safe=""))
    else:
        # 50 pages consumed and a resumptionToken is still outstanding -
        # never write a silently incomplete deposit file.
        raise SetError("OAI paging did not finish within 50 pages, so the "
                       "output would be incomplete. Narrow the set "
                       "(volume / issue / year) and try again.")
    if not records:
        raise SetError("OAI returned no records for this volume/issue.")
    return pages, records


# ---------------------------------------------------------------------------
# Crosswalk helpers
# ---------------------------------------------------------------------------
def parse_date(text: str):
    """'2019-01-15T08:00:00Z' → ('2019','01','15') or None."""
    m = DATE_RE.match(text or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def resolve_dates(records, override):
    """Attach a (y,m,d) date to each record; the override wins when given.

    Raises SetError when any record needs a date it doesn't have.
    """
    if override:
        d = parse_date(override)
        for r in records:
            r["date"] = d
        return
    missing = []
    for i, r in enumerate(records, start=1):
        r["date"] = parse_date(r["pubdate"])
        if r["date"] is None:
            missing.append(str(i))
    if missing:
        raise SetError(
            "Records {} have no publication-date in the bepress XML; enter a "
            "publication date override for this set.".format(", ".join(missing)))


def strip_html_tags(text: str) -> str:
    """Flatten inline HTML in abstracts to readable plain text (DOAJ)."""
    if not text:
        return ""
    s = html_lib.unescape(text)
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</?\s*p[^>]*>", "\n", s)
    s = re.sub(r"(?is)<\s*sup[^>]*>(.*?)</\s*sup\s*>", r"^\1", s)
    s = re.sub(r"(?is)<\s*sub[^>]*>(.*?)</\s*sub\s*>", r"_\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip()
                     for ln in s.splitlines()).strip()


def prettify(root) -> bytes:
    return minidom.parseString(ET.tostring(root, encoding="utf-8")) \
                  .toprettyxml(indent="  ", encoding="utf-8")


def batch_timestamp() -> str:
    """Crossref 16-digit doi_batch_id / timestamp."""
    dt = datetime.now()
    return dt.strftime("%Y%m%d%H%M%S") + "{:02d}".format(dt.microsecond // 10000)


CROSSREF_VERSION = "5.4.0"


def crossref_head(root, ident):
    """The deposit head. Refuses to write a blank depositor: /api/start
    checks this before a run starts, and this is the backstop for any other
    caller (standalone use, tests) so a deposit can never be built with an
    empty or borrowed identity."""
    missing = missing_crossref_fields(ident, need_publisher=False)
    if missing:
        raise SetError(crossref_identity_error(missing, "a Crossref deposit"))
    head = ET.SubElement(root, "head")
    ts = batch_timestamp()
    ET.SubElement(head, "doi_batch_id").text = ts
    ET.SubElement(head, "timestamp").text = ts
    dep = ET.SubElement(head, "depositor")
    ET.SubElement(dep, "depositor_name").text = ident["depositor_name"]
    ET.SubElement(dep, "email_address").text = ident["depositor_email"]
    ET.SubElement(head, "registrant").text = ident["registrant"]


def crossref_root(ident):
    root = ET.Element("doi_batch", {
        "xmlns": "http://www.crossref.org/schema/" + CROSSREF_VERSION,
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "version": CROSSREF_VERSION,
        "xsi:schemaLocation":
            "http://www.crossref.org/schema/{0} "
            "https://www.crossref.org/schemas/crossref{0}.xsd"
            .format(CROSSREF_VERSION),
    })
    crossref_head(root, ident)
    ET.SubElement(root, "body")
    return root


def add_publication_date(parent, date):
    pd = ET.SubElement(parent, "publication_date", {"media_type": "online"})
    ET.SubElement(pd, "month").text = date[1]
    ET.SubElement(pd, "day").text = date[2]
    ET.SubElement(pd, "year").text = date[0]


def add_contributors(parent, record, logf, rec_no):
    """Crossref <contributors> — 5.3.1+ affiliations markup. Omitted when
    the record has no authors (allowed by the schema; e.g. editor's notes)."""
    authors = record["authors"]
    if not authors:
        logf("Record #{}: no authors; <contributors> omitted.".format(rec_no))
        return
    contribs = ET.SubElement(parent, "contributors")
    for i, a in enumerate(authors):
        seq = "first" if i == 0 else "additional"
        if not (a["lname"] or a["fname"]) and a["organization"]:
            org = ET.SubElement(contribs, "organization",
                                {"sequence": seq, "contributor_role": "author"})
            org.text = a["organization"]
            continue
        pn = ET.SubElement(contribs, "person_name",
                           {"sequence": seq, "contributor_role": "author"})
        given = " ".join(p for p in (a["fname"], a["mname"]) if p)
        if given:
            ET.SubElement(pn, "given_name").text = given
        surname = " ".join(p for p in (a["lname"], a["suffix"]) if p)
        if not surname:
            surname = given or "Unknown"
            logf("Record #{}: author with no surname; using {!r}."
                 .format(rec_no, surname))
        ET.SubElement(pn, "surname").text = surname
        if a["institution"]:
            affs = ET.SubElement(pn, "affiliations")
            inst = ET.SubElement(affs, "institution")
            ET.SubElement(inst, "institution_name").text = a["institution"]


def add_doi_data(parent, record, logf, rec_no):
    """Crossref <doi_data> with the crawler-based collection for
    Similarity Check, mirroring the old production tool."""
    dd = ET.SubElement(parent, "doi_data")
    ET.SubElement(dd, "doi").text = record["doi"]
    ET.SubElement(dd, "resource").text = record["coverpage"] or record["native"]
    crawled = record["fulltext"] or record["native"]
    if crawled:
        coll = ET.SubElement(dd, "collection", {"property": "crawler-based"})
        item = ET.SubElement(coll, "item", {"crawler": "iParadigms"})
        ET.SubElement(item, "resource").text = crawled
    else:
        logf("Record #{}: no fulltext-url/native-url; crawler resource "
             "omitted.".format(rec_no))


def build_crossref_journal(reg, ident, entry, vol, iss, records, logf):
    root = crossref_root(ident)
    body = root.find("body")
    journal = ET.SubElement(body, "journal")
    jm = ET.SubElement(journal, "journal_metadata")
    ET.SubElement(jm, "full_title").text = entry["name"]
    if entry.get("abbrev"):
        ET.SubElement(jm, "abbrev_title").text = entry["abbrev"]
    ET.SubElement(jm, "issn", {"media_type": "electronic"}).text = entry["eissn"]
    if vol or iss:
        ji = ET.SubElement(journal, "journal_issue")
        add_publication_date(ji, records[0]["date"])
        if vol:
            jv = ET.SubElement(ji, "journal_volume")
            ET.SubElement(jv, "volume").text = str(int(vol))
        if iss:
            ET.SubElement(ji, "issue").text = str(int(iss))
    for n, rec in enumerate(records, start=1):
        ja = ET.SubElement(journal, "journal_article",
                           {"publication_type": "full_text"})
        titles = ET.SubElement(ja, "titles")
        title = rec["title"] or "[Untitled]"
        if not rec["title"]:
            logf("Record #{}: missing title; using '[Untitled]'.".format(n))
        ET.SubElement(titles, "title").text = title
        add_contributors(ja, rec, logf, n)
        add_publication_date(ja, rec["date"])
        if rec["fpage"]:
            pages = ET.SubElement(ja, "pages")
            ET.SubElement(pages, "first_page").text = rec["fpage"]
            if rec["lpage"]:
                ET.SubElement(pages, "last_page").text = rec["lpage"]
        add_doi_data(ja, rec, logf, n)
    return root


def build_crossref_conference(reg, ident, entry, vol, iss, records, logf,
                              year=""):
    """Conference markup (schema 5.4.0). With an eISSN: proceedings series
    keyed to the ISSN (the old tool's AMTP pattern). Without one:
    <proceedings_metadata> + <noisbn>, the schema's no-identifier shape
    (needed for proceedings like PMHR that have no registered ISSN).
    The proceedings title prefers the harvested <publication-title>
    (e.g. "AMTP Proceedings 2026"), falling back to the registry name."""
    root = crossref_root(ident)
    body = root.find("body")
    conf = ET.SubElement(body, "conference")
    em = ET.SubElement(conf, "event_metadata")
    ET.SubElement(em, "conference_name").text = (
        entry.get("conference_name") or entry["name"])
    if entry.get("abbrev"):
        ET.SubElement(em, "conference_acronym").text = entry["abbrev"]
    ET.SubElement(em, "conference_date").text = year or records[0]["date"][0]
    ptitle = records[0].get("pubtitle", "").strip()
    if not ptitle:
        ptitle = entry["name"] + (" " + year if year else "")
        logf("No <publication-title> in the harvest; proceedings title "
             "falls back to {!r}.".format(ptitle))
    eissn = str(entry.get("eissn", "")).strip()
    if eissn:
        psm = ET.SubElement(conf, "proceedings_series_metadata")
        sm = ET.SubElement(psm, "series_metadata")
        titles = ET.SubElement(sm, "titles")
        ET.SubElement(titles, "title").text = entry["name"]
        ET.SubElement(sm, "issn", {"media_type": "electronic"}).text = eissn
        ET.SubElement(psm, "proceedings_title").text = ptitle
        if vol:
            ET.SubElement(psm, "volume").text = str(int(vol))
    else:
        logf("'{}' has no eISSN; using proceedings_metadata (no series) "
             "markup.".format(entry["id"]))
        if vol:
            logf("Note: volume is not part of proceedings_metadata markup "
                 "and was omitted (it needs an ISSN-keyed series).")
        psm = ET.SubElement(conf, "proceedings_metadata")
        ET.SubElement(psm, "proceedings_title").text = ptitle
    if not ident.get("publisher", "").strip():
        raise SetError(crossref_identity_error(
            ["publisher"], "a conference proceedings deposit"))
    pub = ET.SubElement(psm, "publisher")
    ET.SubElement(pub, "publisher_name").text = ident["publisher"]
    add_publication_date(psm, records[0]["date"])
    ET.SubElement(psm, "noisbn", {"reason": "simple_series"})
    for n, rec in enumerate(records, start=1):
        cp = ET.SubElement(conf, "conference_paper")
        add_contributors(cp, rec, logf, n)  # contributors precede titles here
        titles = ET.SubElement(cp, "titles")
        title = rec["title"] or "[Untitled]"
        if not rec["title"]:
            logf("Record #{}: missing title; using '[Untitled]'.".format(n))
        ET.SubElement(titles, "title").text = title
        add_publication_date(cp, rec["date"])
        if rec["fpage"]:
            pages = ET.SubElement(cp, "pages")
            ET.SubElement(pages, "first_page").text = rec["fpage"]
            if rec["lpage"]:
                ET.SubElement(pages, "last_page").text = rec["lpage"]
        add_doi_data(cp, rec, logf, n)
    return root


def build_doaj(reg, ident, entry, vol, iss, records, logf):
    """DOAJ native article XML (doajArticles.xsd), one <record> per article.
    Element order matches the schema; eISSN is deposited unhyphenated,
    matching the old tool's accepted uploads."""
    recs = ET.Element("records")
    publisher = ident.get("publisher", "").strip()
    if not publisher:
        raise SetError(crossref_identity_error(["publisher"], "DOAJ XML"))
    for n, r in enumerate(records, start=1):
        rec = ET.SubElement(recs, "record")
        ET.SubElement(rec, "language").text = "eng"
        ET.SubElement(rec, "publisher").text = publisher
        ET.SubElement(rec, "journalTitle").text = entry["name"]
        ET.SubElement(rec, "eissn").text = entry["eissn"].replace("-", "")
        ET.SubElement(rec, "publicationDate").text = "-".join(r["date"])
        if vol:
            ET.SubElement(rec, "volume").text = str(int(vol))
        if iss:
            ET.SubElement(rec, "issue").text = str(int(iss))
        if r["fpage"]:
            ET.SubElement(rec, "startPage").text = r["fpage"]
        if r["lpage"]:
            ET.SubElement(rec, "endPage").text = r["lpage"]
        ET.SubElement(rec, "doi").text = r["doi"]
        ET.SubElement(rec, "documentType").text = "Article"
        ET.SubElement(rec, "title", {"language": "eng"}).text = \
            r["title"] or "[Untitled]"
        authors = ET.SubElement(rec, "authors")
        for a in r["authors"]:
            full = " ".join(p for p in (a["fname"], a["mname"], a["lname"],
                                        a["suffix"]) if p) or a["organization"]
            if full:
                ET.SubElement(ET.SubElement(authors, "author"),
                              "name").text = full
        if not r["authors"]:
            logf("Record #{}: no authors in DOAJ output.".format(n))
        abstract = strip_html_tags(r["abstract"])
        if abstract:
            ET.SubElement(rec, "abstract", {"language": "eng"}).text = abstract
        ET.SubElement(rec, "fullTextUrl", {"format": "html"}).text = \
            r["coverpage"] or r["native"]
        if r["keywords"]:
            kws = ET.SubElement(rec, "keywords", {"language": "eng"})
            for kw in r["keywords"]:
                ET.SubElement(kws, "keyword").text = kw
    return recs


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s.
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",  # idle | starting | minting | waiting | harvesting | writing | done | error
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "files": [],       # basenames written this run (full paths in the log)
    "last_file": None,
    "summary": "",
    "warnings": 0,
}
LOCK = threading.Lock()
CONTINUE_EVENT = threading.Event()
STOP_EVENT = threading.Event()
RUNNING_PHASES = ("starting", "minting", "waiting", "harvesting", "writing")


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary.
RUN_LOG_NAME = None
CLEAR_RESETS = {"files": [], "last_file": None, "warnings": 0}


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


def add_file(path: str):
    with LOCK:
        STATE["files"].append(os.path.basename(path))
        STATE["last_file"] = path
    log("Wrote " + path)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def set_label(s) -> str:
    lab = s["journal"]
    if s.get("syear"):
        lab += "_" + s["syear"]
    if s["volume"]:
        lab += "_v" + str(int(s["volume"])).zfill(2)
    if s["issue"]:
        lab += "_i" + str(int(s["issue"])).zfill(2)
    return lab


def describe(s) -> str:
    d = s["journal"]
    if s.get("syear"):
        d += " " + s["syear"]
    if s["volume"]:
        d += " vol. " + str(int(s["volume"]))
    if s["issue"]:
        d += " iss. " + str(int(s["issue"]))
    return d


def process_xml_set(base_url, reg, ident, entries, s, out_dir, stamp, warn):
    """Harvest one set and write its requested XML files."""
    label = set_label(s)
    pages, records = harvest(base_url, s["journal"], s["volume"], s["issue"],
                             log, s.get("syear", ""))
    log("{}: {} record(s) harvested.".format(describe(s), len(records)))
    set_state(phase="writing")

    if s["bepress"]:
        for i, raw in enumerate(pages, start=1):
            suffix = "" if len(pages) == 1 else "_p{}".format(i)
            path = os.path.join(out_dir, "DC_Bepress_{}{}_{}.xml".format(
                label, suffix, stamp))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw)
            add_file(path)

    if not (s["crossref"] or s["doaj"]):
        return

    entry = entries.get(s["journal"])
    if entry is None:  # /api/start validates this; belt and braces
        raise SetError("'{}' is not in the journal registry; Crossref/DOAJ "
                       "output needs its title and ISSN.".format(s["journal"]))

    missing = [str(i) for i, r in enumerate(records, start=1) if not r["doi"]]
    if missing:
        raise SetError(
            "DOIs do not appear in the bepress XML for {} (record{} {} of {}). "
            "Add and save the DOIs in Digital Commons, wait for the OAI feed "
            "to update, then run the crosswalks again.".format(
                describe(s), "s" if len(missing) > 1 else "",
                ", ".join(missing), len(records)))

    resolve_dates(records, s["pubdate"])
    no_url = [str(i) for i, r in enumerate(records, start=1)
              if not (r["coverpage"] or r["native"])]
    if no_url:
        warn("{}: record(s) {} have no coverpage/native URL — fix before "
             "depositing.".format(describe(s), ", ".join(no_url)))

    if s["crossref"]:
        if entry.get("type") == "conference":
            root = build_crossref_conference(reg, ident, entry, s["volume"],
                                             s["issue"], records, log,
                                             s.get("syear", ""))
        else:
            root = build_crossref_journal(reg, ident, entry, s["volume"],
                                          s["issue"], records, log)
        path = os.path.join(out_dir, "DC_Crossref_{}_{}.xml".format(label, stamp))
        with open(path, "wb") as fh:
            fh.write(prettify(root))
        add_file(path)

    if s["doaj"]:
        root = build_doaj(reg, ident, entry, s["volume"], s["issue"],
                          records, log)
        path = os.path.join(out_dir, "DC_DOAJ_{}_{}.xml".format(label, stamp))
        with open(path, "wb") as fh:
            fh.write(prettify(root))
        add_file(path)


def run_worker(session, out_dir, sets, reg):
    base_url = session["base_url"].rstrip("/")
    entries = {j["id"]: j for j in reg.get("journals", [])}
    prefix = effective_prefix(session, reg)
    ident = effective_crossref(session, reg)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    warnings = []

    def warn(msg):
        warnings.append(msg)
        log("WARNING: " + msg)

    try:
        set_state(phase="starting", summary="", files=[], last_file=None,
                  warnings=0)
        mint_sets = [s for s in sets if s["mint"]]
        xml_sets = [s for s in sets if s["bepress"] or s["crossref"] or s["doaj"]]
        total = len(mint_sets) + len(xml_sets)
        done = 0

        # ---- Phase 1: mint DOIs --------------------------------------
        if mint_sets:
            set_state(phase="minting")
            for s in mint_sets:
                set_progress(done, total, "Minting DOIs for " + describe(s))
                ctx = {"id": s["journal"], "year": s["year"],
                       "volume": s["volume"], "issue": s["issue"]}
                dois = mint_dois(prefix, s["pattern"], ctx, s["count"])
                path = os.path.join(out_dir, "DC_DOIs_{}_{}.txt".format(
                    set_label(s), stamp))
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(dois) + "\n")
                add_file(path)
                log("{}: {} DOI(s) minted ({} … {}).".format(
                    describe(s), len(dois), dois[0], dois[-1]))
                done += 1
                set_progress(done, total, "")

        # ---- Pause between minting and harvesting --------------------
        if mint_sets and xml_sets:
            set_state(phase="waiting")
            set_progress(done, total, "Waiting — add the DOIs in Digital "
                                      "Commons, then continue.")
            log("PAUSED. Add the minted DOIs to the articles in Digital "
                "Commons and save each revision, then click 'Continue to "
                "XML'. Note: revisions can take a little while to appear in "
                "the OAI feed. Click 'End job' to stop after minting.")
            while not CONTINUE_EVENT.wait(timeout=0.5):
                if STOP_EVENT.is_set():
                    set_progress(done, total, "")
                    set_state(phase="done",
                              summary="Ended after minting — {} file(s) in {}"
                                      .format(len(STATE["files"]), out_dir))
                    log("Job ended after minting at the user's request.")
                    return
            log("Continuing to XML output…")

        # ---- Phase 2: harvest + crosswalk -----------------------------
        failed = 0
        for s in xml_sets:
            set_state(phase="harvesting")
            set_progress(done, total, "Harvesting " + describe(s))
            try:
                process_xml_set(base_url, reg, ident, entries, s, out_dir,
                                stamp, warn)
            except SetError as e:
                failed += 1
                warn(str(e))
            done += 1
            set_progress(done, total, "")

        with LOCK:
            n_files = len(STATE["files"])
        summary = "{} file(s) written to {}".format(n_files, out_dir)
        if warnings:
            summary += " — {} warning(s), see log".format(len(warnings))
        if xml_sets and failed == len(xml_sets) and not mint_sets:
            return fail("All sets failed: " + " | ".join(warnings))
        set_state(phase="done", summary=summary, warnings=len(warnings))
    except Exception as e:  # never leave the UI spinning
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# UI — branded page per suite conventions (WCAG 2.1 AA). Tokens are
# substituted at startup (no str.format, so CSS/JS braces stay untouched).
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
 <fieldset><legend>Volumes / issues</legend>
  <div id="sets"></div>
  <button type="button" class="secondary" id="addset">+ Add another volume / issue</button>
  <p class="hint">Volume and issue are optional — leave blank to omit those
  digits from DOIs and harvest the whole publication. Titles registered as a
  <em>yearly series</em> (a community with one series per year, e.g. AMTP
  Proceedings or PMHR) take a required Year instead — the harvest targets
  <code>ID_YEAR</code> — and volume, if entered, goes into the Crossref
  series metadata. Pub date override (YYYY-MM-DD) replaces the per-article
  dates from the bepress XML. Mint / Crossref / DOAJ actions are greyed out
  for a title when its DOI or DOAJ features are turned off in the journal
  registry.</p>
 </fieldset>
 <fieldset><legend>Output</legend>
  <label for="outdir">Output folder</label>
  <input type="text" id="outdir" name="outdir" value="~/Desktop" size="40"
         autocomplete="off" spellcheck="false">
 </fieldset>
 <button type="submit" id="go">Run</button>
</form>

<div id="status" role="status" aria-live="polite">Idle — configure sets and click Run.</div>
<div id="waitbtns">
 <button type="button" id="continue">Continue to XML</button>
 <button type="button" class="secondary" id="endjob">End job</button>
</div>

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

<details style="margin-top:1.25rem" open>
 <summary style="font-weight:600;cursor:pointer">Journal registry</summary>
 <p class="hint">Titles used for Crossref and DOAJ output, saved to
 config/journals.json. Click a row to edit a title; use DOI / DOAJ to turn
 those features on or off. Journal-type Crossref and all DOAJ output need
 an eISSN; conference-type Crossref can deposit without one (the module
 switches to the no-ISSN proceedings markup).</p>
 <table class="reg" id="regtable">
  <thead><tr><th>Title</th><th class="idc">Structure ID</th>
  <th class="issnc">eISSN</th><th class="typec">Type</th>
  <th class="flag">DOI</th><th class="flag">DOAJ</th>
  <th>Suffix pattern</th></tr></thead>
  <tbody></tbody>
 </table>
 <p>
  <button type="button" class="secondary" id="regadd">+ Add journal</button>
  <span id="regmsg" role="status" aria-live="polite"></span>
 </p>
</details>

<details style="margin-top:1rem">
 <summary style="font-weight:600;cursor:pointer">DOI suffix patterns</summary>
 <p class="hint">A DOI is <code class="tok">prefix</code>/<span id="patprefix"></span>
 followed by the suffix your pattern produces. Build a suffix from tokens:
 <code class="tok">{id}</code> <code class="tok">{year}</code>
 <code class="tok">{volume}</code> <code class="tok">{issue}</code>
 <code class="tok">{seq}</code> (add <code class="tok">:N</code> to zero-pad,
 e.g. <code class="tok">{volume:2}</code>; volume/issue vanish when blank),
 plus any custom variables you define below. Save named patterns here, then
 assign one to a journal (in its editor) or pick one per volume / issue.</p>
 <div class="field">
  <label for="patpick">Pattern</label>
  <select id="patpick"></select>
 </div>
 <div class="field">
  <label for="patname">Name</label>
  <input type="text" id="patname" autocomplete="off">
 </div>
 <div class="field">
  <label for="pattemplate">Suffix template</label>
  <input type="text" id="pattemplate" autocomplete="off" spellcheck="false">
 </div>
 <fieldset><legend>Custom variables</legend>
  <div id="patvars"></div>
  <button type="button" class="secondary" id="patvaradd">+ Add variable</button>
  <p class="hint">Fixed = a constant inserted as-is. Counter = a per-article
  number with its own start value and zero-pad width.</p>
 </fieldset>
 <p>Preview: <span class="preview" id="patpreview"></span></p>
 <div class="actions" style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
  <button type="button" id="patsave">Save pattern</button>
  <button type="button" class="secondary" id="patdefault">Set as suite default</button>
  <button type="button" class="linkish" id="patdelete">Delete pattern</button>
  <span id="patmsg" role="status" aria-live="polite"></span>
 </div>
</details>
</main>

<!-- Journal record editor modal -->
<div class="backdrop" id="jbackdrop" role="presentation">
 <div class="modal" role="dialog" aria-modal="true" aria-labelledby="jmtitle" id="jmodal">
  <h3 id="jmtitle">Edit journal</h3>
  <div class="field"><label for="m_id">Structure ID</label>
   <input type="text" id="m_id" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label for="m_name">Full title</label>
   <input type="text" id="m_name" autocomplete="off"></div>
  <div class="field"><label for="m_abbrev">Abbreviation (optional)</label>
   <input type="text" id="m_abbrev" autocomplete="off"></div>
  <div class="field"><label for="m_eissn">eISSN (NNNN-NNNN)</label>
   <input type="text" id="m_eissn" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label for="m_type">Crossref record type</label>
   <select id="m_type"><option value="journal">journal</option>
    <option value="conference">conference</option></select></div>
  <div class="field"><label for="m_structure">DC structure</label>
   <select id="m_structure">
    <option value="volume_issue">volume / issue (journal-style)</option>
    <option value="yearly">yearly series (community with one series per year)</option>
   </select></div>
  <div class="field" id="m_conf_field"><label for="m_conf">Conference name (conference type)</label>
   <input type="text" id="m_conf" autocomplete="off"></div>
  <div class="field checks"><label><input type="checkbox" id="m_doi"> DOI features (minting &amp; Crossref)</label>
   <label><input type="checkbox" id="m_doaj"> DOAJ XML</label></div>
  <div class="field"><label for="m_pattern">DOI suffix pattern</label>
   <select id="m_pattern"></select></div>
  <div class="err" id="m_err" role="alert"></div>
  <div class="actions">
   <button type="button" id="m_save">Save</button>
   <button type="button" class="secondary" id="m_cancel">Cancel</button>
   <span class="spacer"></span>
   <button type="button" class="linkish" id="m_delete">Delete title</button>
  </div>
 </div>
</div>
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

let REG=null, SETID=0;
// Crossref prefix comes from the shell (session.json), not the registry.
const CROSSREF_PREFIX="__CROSSREF_PREFIX__";
const setsBox=document.getElementById('sets'), form=document.getElementById('form'),
      go=document.getElementById('go'), status=document.getElementById('status'),
      logBox=document.getElementById('log'), waitBtns=document.getElementById('waitbtns');
const esc=t=>String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;');

/* ---- shared DOI suffix pattern helpers (mirror the Python engine) ---- */
function jpad(val,width){let s=String(val);while(s.length<width)s='0'+s;return s;}
function patternByName(name){
  return (REG.doi_patterns||[]).find(p=>p.name===name)||null;
}
function suitePattern(){
  return patternByName(REG.default_pattern)||(REG.doi_patterns||[])[0]||
         {name:'',template:'',variables:[]};
}
function renderToken(name,pad,custom,ctx,seq){
  if(name==='id') return ctx.id||'';
  if(name==='year') return ctx.year||'';
  if(name==='volume'||name==='issue'){
    const val=(ctx[name]||'').toString().trim();
    return val?jpad(parseInt(val,10),pad!=null?pad:2):'';
  }
  if(name==='seq') return jpad(seq,pad!=null?pad:2);
  const v=custom[name];
  if(!v) return '{'+name+(pad!=null?(':'+pad):'')+'}';
  if(v.kind==='fixed') return v.value!=null?String(v.value):'';
  const start=parseInt(v.start,10)||1, w=pad!=null?pad:(parseInt(v.pad,10)||2);
  return jpad(start+seq-1,w);
}
function renderSuffix(pattern,ctx,seq){
  const custom={}; (pattern.variables||[]).forEach(v=>{custom[v.name]=v;});
  const t=pattern.template||''; let out='',i=0;
  while(i<t.length){
    if(t[i]==='{'){
      const j=t.indexOf('}',i);
      if(j<0){out+=t.slice(i);break;}
      const inner=t.slice(i+1,j); let name=inner,pad=null,c=inner.indexOf(':');
      if(c>=0){name=inner.slice(0,c);pad=parseInt(inner.slice(c+1),10);if(isNaN(pad))pad=null;}
      out+=renderToken(name,pad,custom,ctx,seq); i=j+1;
    } else {out+=t[i];i++;}
  }
  return out;
}
function patternOptions(sel){
  let h='<option value="">(journal default)</option>';
  h+=(REG.doi_patterns||[]).map(p=>'<option value="'+esc(p.name)+'"'+
      (p.name===sel?' selected':'')+'>'+esc(p.name)+'</option>').join('');
  return h;
}

function journalOptions(sel){
  let h=REG.journals.map(j=>'<option value="'+esc(j.id)+'"'+(j.id===sel?' selected':'')+'>'
      +esc(j.name)+' ('+esc(j.id)+')</option>').join('');
  h+='<option value="__custom__"'+(sel==='__custom__'?' selected':'')+'>Custom structure ID…</option>';
  return h;
}
const DOI_OFF_MSG='DOI features are turned off for this title in the journal registry.';
const DOAJ_OFF_MSG='DOAJ is turned off for this title in the journal registry.';

function updateRowAvailability(div){
  const sel=div.querySelector('[name=journal]');
  let jid=sel.value;
  if(jid==='__custom__') jid=div.querySelector('[name=customid]').value.trim().toLowerCase();
  const entry=(REG.journals||[]).find(j=>j.id===jid)||null;
  const doiOk=!entry||entry.doi_enabled!==false;
  const doajOk=!entry||entry.doaj_enabled!==false;
  const setCtl=(name,ok,msg)=>{
    const inp=div.querySelector('[name='+name+']'), lab=inp.closest('label');
    inp.disabled=!ok;
    if(!ok&&inp.checked) inp.checked=false;
    lab.classList.toggle('off',!ok);
    if(ok) lab.removeAttribute('title'); else lab.title=msg;
  };
  setCtl('mint',doiOk,DOI_OFF_MSG);
  setCtl('crossref',doiOk,DOI_OFF_MSG);
  setCtl('doaj',doajOk,DOAJ_OFF_MSG);
  // yearly-series titles: show the Year input, hide Issue (and the
  // duplicate minting Year — the series year is used for minting too)
  div.classList.toggle('yearly',!!(entry&&entry.structure==='yearly'));
  // if minting got disabled, collapse its extra fields
  div.classList.toggle('minting',div.querySelector('[name=mint]').checked);
}

function addSet(){
  const id=++SETID, div=document.createElement('div');
  div.className='set'; div.dataset.sid=id;
  div.innerHTML=
   '<div class="row">'+
   '<label>Journal <select name="journal" aria-label="Journal">'+journalOptions()+'</select></label>'+
   '<input type="text" name="customid" class="small" placeholder="custom id" aria-label="Custom structure ID" style="display:none">'+
   '<label class="syearlab">Year <input type="text" name="syear" class="tiny" inputmode="numeric" aria-label="Series year"></label>'+
   '<label>Vol. <input type="text" name="volume" class="tiny" inputmode="numeric" aria-label="Volume number"></label>'+
   '<label class="isslab">Iss. <input type="text" name="issue" class="tiny" inputmode="numeric" aria-label="Issue number"></label>'+
   '<button type="button" class="linkish" data-remove>Remove</button>'+
   '</div><div class="row">'+
   '<label><input type="checkbox" name="mint"> Mint DOIs</label>'+
   '<label class="mintextra mintyear">Year <input type="text" name="year" class="tiny" inputmode="numeric" aria-label="Year of publication"></label>'+
   '<label class="mintextra"># DOIs <input type="text" name="count" class="tiny" inputmode="numeric" aria-label="Number of DOIs"></label>'+
   '<label class="mintextra">Pattern <select name="pattern" aria-label="DOI suffix pattern">'+patternOptions('')+'</select></label>'+
   '<label><input type="checkbox" name="bepress"> bepress XML</label>'+
   '<label><input type="checkbox" name="crossref"> Crossref XML</label>'+
   '<label><input type="checkbox" name="doaj"> DOAJ XML</label>'+
   '<label>Pub date override <input type="text" name="pubdate" class="small" placeholder="YYYY-MM-DD" aria-label="Publication date override"></label>'+
   '</div>';
  div.querySelector('[data-remove]').addEventListener('click',()=>{
    if(setsBox.children.length>1) div.remove();
  });
  div.querySelector('[name=mint]').addEventListener('change',e=>{
    div.classList.toggle('minting',e.target.checked);
  });
  div.querySelector('[name=journal]').addEventListener('change',e=>{
    div.querySelector('[name=customid]').style.display=
      e.target.value==='__custom__'?'inline-block':'none';
    updateRowAvailability(div);
  });
  div.querySelector('[name=customid]').addEventListener('input',()=>updateRowAvailability(div));
  setsBox.appendChild(div);
  updateRowAvailability(div);
}
document.getElementById('addset').addEventListener('click',addSet);

function refreshRows(){
  for(const div of setsBox.children){
    const sel=div.querySelector('[name=journal]'), cur=sel.value;
    sel.innerHTML=journalOptions(cur==='__custom__'?'__custom__':cur);
    const pat=div.querySelector('[name=pattern]'), pcur=pat.value;
    pat.innerHTML=patternOptions(pcur);
    updateRowAvailability(div);
  }
}

function readSets(){
  const out=[];
  for(const div of setsBox.children){
    const v=n=>div.querySelector('[name='+n+']').value.trim();
    const c=n=>div.querySelector('[name='+n+']').checked;
    let jid=v('journal'); if(jid==='__custom__') jid=v('customid').toLowerCase();
    out.push({journal:jid,volume:v('volume'),issue:v('issue'),syear:v('syear'),
      mint:c('mint'),year:v('year'),count:v('count'),pattern:v('pattern'),
      bepress:c('bepress'),crossref:c('crossref'),doaj:c('doaj'),
      pubdate:v('pubdate')});
  }
  return out;
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
form.addEventListener('submit',async e=>{
  e.preventDefault();
  clearErr();
  const sets=readSets(), outdir=form.outdir.value.trim();
  if(!outdir){showErr('Enter an output folder.');return;}
  go.disabled=true;
  const r=await fetch('/api/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({out_dir:outdir,sets})});
  const j=await r.json();
  if(!r.ok){showErr(j.error||'Could not start.');go.disabled=false;return;}
  poll();
});
document.getElementById('continue').addEventListener('click',()=>fetch('/api/continue',{method:'POST'}));
document.getElementById('endjob').addEventListener('click',()=>{
  if(confirm('End the job now? XML output for this run will be skipped.'))
    fetch('/api/stop',{method:'POST'});
});

async function poll(){
  const s=await (await fetch('/api/state')).json();
  renderLog(s.log); logStateSeen(s);
  const p=s.progress&&s.progress.total?(' ('+s.progress.current+'/'+s.progress.total+')'):'';
  const m=s.progress&&s.progress.msg?' — '+s.progress.msg:'';
  waitBtns.style.display=s.phase==='waiting'?'flex':'none';
  if(s.phase==='done'){
    status.className='done';
    let h='Done: '+esc(s.summary);
    if(s.files.length) h+='<ul id="files">'+s.files.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul>';
    status.innerHTML=h; go.disabled=false;
  }else if(s.phase==='error'){
    status.className='error'; status.textContent='Error: '+s.summary; go.disabled=false;
  }else if(s.phase==='waiting'){
    status.className='waiting';
    status.textContent='Paused — add the minted DOIs to the articles in Digital Commons and '
      +'save each revision, then click "Continue to XML" (or "End job" to stop here).';
    go.disabled=true;
  }else if(s.phase==='idle'){
    status.className=''; status.textContent='Idle — configure sets and click Run.';
  }else{
    status.className='';
    status.textContent=s.phase.charAt(0).toUpperCase()+s.phase.slice(1)+'…'+p+m;
    go.disabled=true;
  }
}
setInterval(poll,1500);

/* ---------------- registry persistence ---------------- */
async function persistRegistry(candidate){
  const r=await fetch('/api/journals',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(candidate)});
  const j=await r.json();
  if(!r.ok) return {ok:false,error:j.error||'Could not save.'};
  REG=j.registry;
  renderReg(); refreshRows(); refreshPatternPickers();
  return {ok:true};
}

/* ---------------- registry summary table ---------------- */
const regBody=document.querySelector('#regtable tbody'),
      regMsg=document.getElementById('regmsg');
function flagPill(on){return on?'<span class="pill on">on</span>':'<span class="pill no">off</span>';}
function renderReg(){
  regBody.innerHTML='';
  (REG.journals||[]).forEach((j,idx)=>{
    const tr=document.createElement('tr');
    const patName=j.doi_pattern?esc(j.doi_pattern):'(suite default)';
    tr.innerHTML=
     '<td><button type="button" class="rowbtn" aria-label="Edit '+esc(j.name||j.id||'title')+'">'+esc(j.name||'(untitled)')+'</button></td>'+
     '<td>'+esc(j.id||'')+'</td>'+
     '<td>'+(j.eissn?esc(j.eissn):'&mdash;')+'</td>'+
     '<td>'+esc(j.type||'journal')+(j.structure==='yearly'?' &middot; yearly':'')+'</td>'+
     '<td class="flag">'+flagPill(j.doi_enabled!==false)+'</td>'+
     '<td class="flag">'+flagPill(j.doaj_enabled!==false)+'</td>'+
     '<td>'+patName+'</td>';
    const open=()=>openJournalModal(idx);
    tr.querySelector('.rowbtn').addEventListener('click',e=>{e.stopPropagation();open();});
    tr.addEventListener('click',open);
    regBody.appendChild(tr);
  });
}
document.getElementById('regadd').addEventListener('click',()=>openJournalModal(-1));

/* ---------------- journal record modal ---------------- */
const mGet=id=>document.getElementById(id);
const jbackdrop=mGet('jbackdrop'), jmodal=mGet('jmodal'),
      mErr=mGet('m_err'), mTitle=mGet('jmtitle'), mDelete=mGet('m_delete');
let mIndex=-1, mTrigger=null;
function fillModalPatterns(cur){
  mGet('m_pattern').innerHTML='<option value="">(suite default)</option>'+
    (REG.doi_patterns||[]).map(p=>'<option value="'+esc(p.name)+'"'+
      (p.name===cur?' selected':'')+'>'+esc(p.name)+'</option>').join('');
}
function toggleConf(){
  mGet('m_conf_field').style.display=mGet('m_type').value==='conference'?'block':'none';
}
mGet('m_type').addEventListener('change',toggleConf);
function openJournalModal(index){
  mIndex=index; mErr.textContent=''; mTrigger=document.activeElement;
  const j=index>=0?REG.journals[index]:{};
  mTitle.textContent=index>=0?'Edit journal':'Add journal';
  mGet('m_id').value=j.id||''; mGet('m_name').value=j.name||'';
  mGet('m_abbrev').value=j.abbrev||''; mGet('m_eissn').value=j.eissn||'';
  mGet('m_type').value=j.type==='conference'?'conference':'journal';
  mGet('m_structure').value=j.structure==='yearly'?'yearly':'volume_issue';
  mGet('m_conf').value=j.conference_name||'';
  mGet('m_doi').checked=j.doi_enabled!==false;
  mGet('m_doaj').checked=j.doaj_enabled!==false;
  fillModalPatterns(j.doi_pattern||''); toggleConf();
  mDelete.style.display=index>=0?'inline-block':'none';
  jbackdrop.classList.add('open'); mGet('m_id').focus();
}
function closeJournalModal(){
  jbackdrop.classList.remove('open');
  if(mTrigger&&mTrigger.focus) mTrigger.focus();
}
mGet('m_cancel').addEventListener('click',closeJournalModal);
jbackdrop.addEventListener('click',e=>{if(e.target===jbackdrop)closeJournalModal();});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&jbackdrop.classList.contains('open')) closeJournalModal();
});
jbackdrop.addEventListener('keydown',e=>{  // focus trap
  if(e.key!=='Tab') return;
  const f=[...jmodal.querySelectorAll('input,select,button')]
            .filter(el=>el.offsetParent!==null&&!el.disabled);
  if(!f.length) return;
  const first=f[0], last=f[f.length-1];
  if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
});
mGet('m_save').addEventListener('click',async ()=>{
  const id=mGet('m_id').value.trim().toLowerCase();
  if(!/^[a-z0-9][a-z0-9_-]*$/.test(id)){
    setMsg(mErr,'Enter a structure ID (lowercase letters, digits, - _).',true);return;}
  if(!mGet('m_name').value.trim()){setMsg(mErr,'Enter the full title.',true);return;}
  const rec={id:id,name:mGet('m_name').value.trim(),abbrev:mGet('m_abbrev').value.trim(),
    eissn:mGet('m_eissn').value.trim(),type:mGet('m_type').value,
    structure:mGet('m_structure').value,
    conference_name:mGet('m_conf').value.trim(),
    doi_enabled:mGet('m_doi').checked,doaj_enabled:mGet('m_doaj').checked,
    doi_pattern:mGet('m_pattern').value};
  const journals=[...(REG.journals||[])];
  if(mIndex>=0) journals[mIndex]=rec; else journals.push(rec);
  const res=await persistRegistry({...REG,journals});
  if(!res.ok){setMsg(mErr,res.error,true);return;}
  closeJournalModal();
});
mDelete.addEventListener('click',async ()=>{
  if(mIndex<0) return;
  const j=REG.journals[mIndex];
  if(!confirm('Delete "'+(j.name||j.id)+'" from the registry?')) return;
  const journals=[...REG.journals]; journals.splice(mIndex,1);
  const res=await persistRegistry({...REG,journals});
  if(!res.ok){setMsg(mErr,res.error,true);return;}
  closeJournalModal();
});

/* ---------------- DOI suffix pattern editor ---------------- */
const patpick=mGet('patpick'), patname=mGet('patname'), pattemplate=mGet('pattemplate'),
      patvars=mGet('patvars'), patpreview=mGet('patpreview'), patmsg=mGet('patmsg'),
      patprefix=mGet('patprefix');
let PATCUR='';
function patVarRow(v){
  v=v||{name:'',kind:'fixed',value:'',start:1,pad:2};
  const kind=v.kind==='seq'?'seq':'fixed', row=document.createElement('div');
  row.className='varrow';
  row.innerHTML=
   '<input type="text" class="small" data-k="name" placeholder="name" value="'+esc(v.name||'')+'" aria-label="Variable name">'+
   '<select data-k="kind" aria-label="Variable kind"><option value="fixed"'+(kind==='fixed'?' selected':'')+'>fixed</option>'+
   '<option value="seq"'+(kind==='seq'?' selected':'')+'>counter</option></select>'+
   '<span data-part="fixed"'+(kind==='seq'?' style="display:none"':'')+'>value '+
     '<input type="text" class="small" data-k="value" value="'+esc(v.value!=null?v.value:'')+'" aria-label="Fixed value"></span>'+
   '<span data-part="seq"'+(kind==='fixed'?' style="display:none"':'')+'>start '+
     '<input type="text" class="tiny" data-k="start" inputmode="numeric" value="'+esc(v.start!=null?v.start:1)+'" aria-label="Counter start"> pad '+
     '<input type="text" class="tiny" data-k="pad" inputmode="numeric" value="'+esc(v.pad!=null?v.pad:2)+'" aria-label="Counter pad width"></span>'+
   '<button type="button" class="linkish" data-del>Remove</button>';
  row.querySelector('[data-k=kind]').addEventListener('change',e=>{
    row.querySelector('[data-part=fixed]').style.display=e.target.value==='seq'?'none':'inline';
    row.querySelector('[data-part=seq]').style.display=e.target.value==='seq'?'inline':'none';
    updatePatPreview();
  });
  row.querySelector('[data-del]').addEventListener('click',()=>{row.remove();updatePatPreview();});
  row.querySelectorAll('input').forEach(inp=>inp.addEventListener('input',updatePatPreview));
  patvars.appendChild(row);
}
function renderPatVars(vars){patvars.innerHTML=''; (vars||[]).forEach(patVarRow);}
mGet('patvaradd').addEventListener('click',()=>{patVarRow();updatePatPreview();});
function readPatVars(){
  return [...patvars.children].map(row=>{
    const g=k=>{const el=row.querySelector('[data-k='+k+']');return el?el.value.trim():'';};
    return g('kind')==='seq'
      ?{name:g('name'),kind:'seq',start:parseInt(g('start'),10)||1,pad:parseInt(g('pad'),10)||2}
      :{name:g('name'),kind:'fixed',value:row.querySelector('[data-k=value]').value};
  }).filter(v=>v.name!=='');
}
function currentEditorPattern(){
  return {name:patname.value.trim(),template:pattemplate.value,variables:readPatVars()};
}
function updatePatPreview(){
  const ctx={id:'cimle',year:'2019',volume:'24',issue:'1'};
  try{
    patpreview.textContent=(CROSSREF_PREFIX||'10.XXXXX')+'/'+renderSuffix(currentEditorPattern(),ctx,6);
  }catch(e){patpreview.textContent='(invalid pattern)';}
}
function loadPatternEditor(name){
  const p=patternByName(name);
  if(p){PATCUR=p.name; patname.value=p.name; pattemplate.value=p.template||''; renderPatVars(p.variables);}
  else{PATCUR=''; patname.value=''; pattemplate.value=suitePattern().template||''; renderPatVars([]);}
  updatePatPreview();
}
pattemplate.addEventListener('input',updatePatPreview);
patname.addEventListener('input',updatePatPreview);
patpick.addEventListener('change',()=>loadPatternEditor(patpick.value));
mGet('patsave').addEventListener('click',async ()=>{
  const p=currentEditorPattern();
  if(!p.name){setMsg(patmsg,'Give the pattern a name.',true);return;}
  const patterns=[...(REG.doi_patterns||[])];
  const oldIdx=PATCUR?patterns.findIndex(x=>x.name===PATCUR):-1;
  const dupIdx=patterns.findIndex(x=>x.name===p.name);
  if(dupIdx>=0&&dupIdx!==oldIdx){
    setMsg(patmsg,'A pattern named "'+p.name+'" already exists.',true);return;}
  let default_pattern=REG.default_pattern;
  if(oldIdx>=0){ if(REG.default_pattern===PATCUR) default_pattern=p.name; patterns[oldIdx]=p; }
  else patterns.push(p);
  const res=await persistRegistry({...REG,doi_patterns:patterns,default_pattern});
  if(!res.ok){setMsg(patmsg,res.error,true);return;}
  PATCUR=p.name; patpick.value=p.name; loadPatternEditor(p.name);
  setMsg(patmsg,'Saved.',false,true);
});
mGet('patdefault').addEventListener('click',async ()=>{
  const name=patname.value.trim();
  if(!patternByName(name)){
    setMsg(patmsg,'Save the pattern first, then set it as default.',true);return;}
  const res=await persistRegistry({...REG,default_pattern:name});
  if(!res.ok){setMsg(patmsg,res.error,true);return;}
  setMsg(patmsg,'"'+name+'" is now the suite default.',false,true);
});
mGet('patdelete').addEventListener('click',async ()=>{
  if(!PATCUR||!patternByName(PATCUR)){
    setMsg(patmsg,'Nothing to delete (unsaved pattern).',true);return;}
  if((REG.doi_patterns||[]).length<=1){
    setMsg(patmsg,'Keep at least one pattern in the library.',true);return;}
  if(!confirm('Delete pattern "'+PATCUR+'"?')) return;
  const patterns=REG.doi_patterns.filter(p=>p.name!==PATCUR);
  let default_pattern=REG.default_pattern===PATCUR?patterns[0].name:REG.default_pattern;
  const journals=REG.journals.map(j=>j.doi_pattern===PATCUR?{...j,doi_pattern:''}:j);
  const res=await persistRegistry({...REG,doi_patterns:patterns,default_pattern,journals});
  if(!res.ok){setMsg(patmsg,res.error,true);return;}
  patpick.value=patterns[0].name; loadPatternEditor(patterns[0].name);
  setMsg(patmsg,'Deleted.',false,true);
});
function refreshPatternPickers(){
  patprefix.textContent=CROSSREF_PREFIX||'';
  patpick.innerHTML=(REG.doi_patterns||[]).map(p=>'<option value="'+esc(p.name)+'"'+
      (p.name===PATCUR?' selected':'')+'>'+esc(p.name)+
      (p.name===REG.default_pattern?' (default)':'')+'</option>').join('')+
      '<option value="__new__">+ New pattern…</option>';
}

(async function init(){
  REG=await (await fetch('/api/journals')).json();
  renderReg();
  loadPatternEditor(REG.default_pattern); refreshPatternPickers();
  patpick.value=REG.default_pattern||'';
  addSet(); poll();
})();
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
        "__NAME__": MANIFEST["name"],
        "__SUITE__": brand.get("suite_name", "DC Admin Suite"),
        "__INSTITUTION__": brand.get("institution", ""),
        "__PRIMARY__": primary,
        "__ACCENT__": colors.get("accent", FALLBACK_BRAND["accent"]),
        "__ONPRIMARY__": contrast_text(primary),
        "__HUB__": session.get("hub_url", "#"),
        "__BASE_URL__": session.get("base_url", ""),
        "__CROSSREF_PREFIX__": (session.get("crossref_prefix")
                                or DEFAULT_REGISTRY["prefix"]),
    }.items():
        page = page.replace(token, value)
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
def validate_sets(raw_sets, reg):
    """Normalize and validate the volume / issue payload. Returns
    (sets, error). Enforces the per-title DOI/DOAJ toggles and resolves the
    suffix pattern each minting item will use."""
    entries = {j["id"]: j for j in reg.get("journals", []) if isinstance(j, dict)}
    patterns = {p.get("name"): p for p in reg.get("doi_patterns", [])
                if isinstance(p, dict)}
    if not isinstance(raw_sets, list) or not raw_sets:
        return None, "Add at least one volume / issue."
    sets = []
    for i, s in enumerate(raw_sets, start=1):
        if not isinstance(s, dict):
            return None, "Bad volume / issue #{}.".format(i)
        jid = str(s.get("journal", "")).strip().lower()
        if not ID_RE.match(jid):
            return None, ("Volume / issue #{}: enter a journal structure ID."
                          .format(i))
        entry = entries.get(jid)
        doi_ok = entry is None or bool(entry.get("doi_enabled", True))
        doaj_ok = entry is None or bool(entry.get("doaj_enabled", True))
        vol = str(s.get("volume", "")).strip()
        iss = str(s.get("issue", "")).strip()
        if (vol and not vol.isdigit()) or (iss and not iss.isdigit()):
            return None, ("Volume / issue #{}: volume and issue must be "
                          "numbers.".format(i))
        yearly = bool(entry) and entry.get("structure") == "yearly"
        syear = str(s.get("syear", "")).strip()
        if yearly:
            if not YEAR_RE.match(syear):
                return None, ("Volume / issue #{}: '{}' is a yearly series — "
                              "enter its 4-digit year (the harvest targets "
                              "{}_YYYY).".format(i, jid, jid))
            iss = ""  # per-year series have no issues
        else:
            syear = ""
        mint = bool(s.get("mint"))
        bepress = bool(s.get("bepress"))
        crossref = bool(s.get("crossref"))
        doaj = bool(s.get("doaj"))
        if not (mint or bepress or crossref or doaj):
            return None, ("Volume / issue #{}: pick at least one action (mint "
                          "DOIs and/or XML output).".format(i))
        if (mint or crossref) and not doi_ok:
            return None, ("Volume / issue #{}: DOI features are turned off for "
                          "'{}' in the registry — minting and Crossref XML are "
                          "unavailable. Only bepress or DOAJ XML can be "
                          "produced.".format(i, jid))
        if doaj and not doaj_ok:
            return None, ("Volume / issue #{}: DOAJ is turned off for '{}' in "
                          "the registry — DOAJ XML is unavailable.".format(i, jid))
        year, count = "", 0
        pattern = None
        if mint:
            year = str(s.get("year", "")).strip() or syear
            if not YEAR_RE.match(year):
                return None, ("Volume / issue #{}: minting needs a 4-digit "
                              "year of publication.".format(i))
            try:
                count = int(str(s.get("count", "")).strip())
            except ValueError:
                count = 0
            if not 1 <= count <= 999:
                return None, ("Volume / issue #{}: minting needs the number of "
                              "DOIs (1–999).".format(i))
            want = str(s.get("pattern", "")).strip()
            if want:
                if want not in patterns:
                    return None, ("Volume / issue #{}: DOI pattern {!r} is not "
                                  "in the pattern library.".format(i, want))
                pattern = patterns[want]
            else:
                pattern = resolve_pattern(reg, entry)
            msg = validate_pattern(pattern)
            if msg:
                return None, "Volume / issue #{}: {}".format(i, msg)
        pubdate = str(s.get("pubdate", "")).strip()
        if pubdate and not DATE_RE.match(pubdate):
            return None, ("Volume / issue #{}: pub date override must be "
                          "YYYY-MM-DD.".format(i))
        if crossref or doaj:
            if entry is None:
                return None, ("Volume / issue #{}: '{}' is not in the journal "
                              "registry — add it (with its eISSN) before "
                              "requesting Crossref/DOAJ output.".format(i, jid))
            # Conference-type Crossref can deposit without an ISSN (the
            # proceedings_metadata + noisbn markup); journal-type Crossref
            # and DOAJ output cannot.
            needs_issn = doaj or (crossref
                                  and entry.get("type") != "conference")
            if needs_issn and not str(entry.get("eissn", "")).strip():
                return None, ("Volume / issue #{}: '{}' has no eISSN in the "
                              "registry — add it before requesting "
                              "{} output.".format(
                                  i, jid,
                                  "DOAJ" if doaj else "Crossref/DOAJ"))
        sets.append({"journal": jid, "volume": vol, "issue": iss,
                     "syear": syear,
                     "mint": mint, "year": year, "count": count,
                     "bepress": bepress, "crossref": crossref, "doaj": doaj,
                     "pubdate": pubdate, "pattern": pattern})
    return sets, ""


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

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def do_GET(self):
            if not request_allowed(self):
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/log":
                return send_run_log(self)
            if self.path == "/api/state":
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/api/journals":
                return self._json(load_registry())
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
                if not path or not os.path.isfile(path):
                    return self._json({"error": "No file available."}, 404)
                data = Path(path).read_bytes()
                ctype = ("text/plain" if path.endswith(".txt")
                         else "application/xml")
                self.send_response(200)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
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
            if self.path == "/api/clear":
                with LOCK:
                    busy = STATE["phase"] in RUNNING_PHASES
                if busy:
                    return self._json({"error": CLEAR_REFUSED}, 409)
                clear_for_new_run()
                return self._json({"ok": True})
            if self.path == "/api/start":
                return self.start()
            if self.path == "/api/continue":
                with LOCK:
                    if STATE["phase"] != "waiting":
                        return self._json({"error": "Not waiting."}, 409)
                CONTINUE_EVENT.set()
                return self._json({"ok": True})
            if self.path == "/api/stop":
                with LOCK:
                    if STATE["phase"] != "waiting":
                        return self._json({"error": "Not waiting."}, 409)
                STOP_EVENT.set()
                return self._json({"ok": True})
            if self.path == "/api/journals":
                try:
                    reg = self._read_json()
                except (ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                msg = validate_registry(reg)
                if msg:
                    return self._json({"error": msg}, 400)
                for j in reg["journals"]:  # normalize
                    j["id"] = str(j["id"]).strip().lower()
                    for k in ("name", "abbrev", "eissn", "conference_name"):
                        j[k] = str(j.get(k, "")).strip()
                    j["type"] = str(j.get("type", "journal"))
                    j["structure"] = str(j.get("structure", "volume_issue")).strip()
                    j["doi_enabled"] = bool(j.get("doi_enabled", True))
                    j["doaj_enabled"] = bool(j.get("doaj_enabled", True))
                    j["doi_pattern"] = str(j.get("doi_pattern", "")).strip()
                for p in reg.get("doi_patterns", []):  # normalize patterns
                    p["name"] = str(p.get("name", "")).strip()
                    p["template"] = str(p.get("template", ""))
                    norm_vars = []
                    for v in p.get("variables", []):
                        vv = {"name": str(v.get("name", "")).strip(),
                              "kind": str(v.get("kind", "")).strip()}
                        if vv["kind"] == "fixed":
                            vv["value"] = str(v.get("value", ""))
                        else:
                            vv["start"] = int(v.get("start", 1))
                            vv["pad"] = int(v.get("pad", 2))
                        norm_vars.append(vv)
                    p["variables"] = norm_vars
                reg["default_pattern"] = str(reg.get("default_pattern", "")).strip()
                backfill_registry(reg)
                save_registry(reg)
                log("Journal registry saved ({} journal(s)).".format(
                    len(reg["journals"])))
                return self._json({"ok": True, "registry": reg})
            return self._json({"error": "Not found."}, 404)

        def start(self):
            with LOCK:
                if STATE["phase"] in RUNNING_PHASES:
                    return self._json({"error": "A run is already active."}, 409)
            try:
                req = self._read_json()
                out_dir = os.path.expanduser(str(req["out_dir"]).strip())
                raw_sets = req["sets"]
            except (KeyError, ValueError, json.JSONDecodeError):
                return self._json({"error": "Bad request."}, 400)
            if not os.path.isdir(out_dir):
                return self._json(
                    {"error": "Output folder does not exist: " + out_dir}, 400)
            reg = load_registry()
            sets, msg = validate_sets(raw_sets, reg)
            if msg:
                return self._json({"error": msg}, 400)
            if any(s["mint"] for s in sets):
                pfx = effective_prefix(session, reg)
                if not PREFIX_RE.match(pfx):
                    return self._json(
                        {"error": "The Crossref DOI prefix {!r} is not valid. "
                                  "Set it on the suite's Settings page (for "
                                  "example 10.12345), then relaunch this "
                                  "module.".format(pfx)}, 400)
            # Refuse a deposit whose identity is not configured, BEFORE the
            # run starts, rather than letting every set fail one by one.
            # Only what each requested output actually needs is required: a
            # journal Crossref deposit carries no publisher, so a blank one
            # must not block it.
            entries = {j["id"]: j for j in reg.get("journals", [])
                       if isinstance(j, dict)}
            wants_crossref = [s for s in sets if s["crossref"]]
            wants_doaj = any(s["doaj"] for s in sets)
            needs_publisher = wants_doaj or any(
                (entries.get(s["journal"]) or {}).get("type") == "conference"
                for s in wants_crossref)
            if wants_crossref or wants_doaj:
                ident = effective_crossref(session, reg)
                if wants_crossref:
                    missing = missing_crossref_fields(ident, needs_publisher)
                elif not ident.get("publisher", "").strip():
                    missing = ["publisher"]      # DOAJ alone needs only this
                else:
                    missing = []
                if missing:
                    what = ("a Crossref deposit" if wants_crossref
                            else "DOAJ XML")
                    return self._json(
                        {"error": crossref_identity_error(missing, what)}, 400)
            if not start_run(run_worker, (session, out_dir, sets, reg),
                             events=(CONTINUE_EVENT, STOP_EVENT)):
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
