#!/usr/bin/env python3
"""Image Description Generator v1.2 — DC Admin Suite module.

v1.2 (2026-08-23) reads AI endpoints from the SUITE-LEVEL registry the shell
now owns (config/ai_endpoints.json, path supplied in session.json as
ai_endpoints_path) instead of keeping its own editor. Endpoints are added,
edited and rotated once, in Settings › AI endpoints; this module picks up a
change immediately because it reads that file per request rather than at
launch. The picker and the Test button stay here, where they are used.

v1.1 (2026-08-23) fixes the failure found in the first real run over 78
scanned pages: 12 of them failed with "Remote end closed connection without
response", every one at almost exactly 60 seconds, and re-running never
helped. Two causes, both mine:

  · Requests were NOT streamed, so the server sent nothing back until the
    whole answer was generated and the connection sat idle meanwhile. Any
    intermediary with a 60-second idle-read timeout closed it mid-answer.
    The pages that failed were the text-densest ones — the transcriptions
    that take longest to generate — so they failed deterministically.
    Requests now stream (see ai_generate); bytes flow continuously and the
    idle timer never starts.
  · A dropped connection carries no HTTP status, and the retry loop only
    looked at statuses, so it gave up instantly every time (Retries=0 in
    every failed row of every report). Connection-level failures are now
    marked transient and retried like a rate limit.

A stream that ends without its provider's completion signal is treated as a
failure and retried, never saved — a transcription truncated mid-word that
looks complete is worse than no file at all.


Generates a plain-text "sidecar" text equivalent (<image stem>.txt) for every
image in a folder, using a vision-capable AI endpoint. Built to replace a
Google Apps Script that did the same job against Drive + the Gemini API, with
three differences that matter:

  · BACKEND-AGNOSTIC. The endpoint registry is shared with the OCR &
    Accessibility Toolkit (config/ocr_toolkit.json), so Gemini, any
    OpenAI-compatible endpoint (incl. Azure/Copilot deployments), and
    Anthropic Claude are interchangeable — swap the endpoint, not the code.
  · EDITABLE PROMPTS. The instruction sent with each image lives in a named
    profile in config/image_describer.json, edited in the UI (suite
    JSON-registry + modal pattern). Different collections can have different
    house styles without touching this file.
  · REVIEWABLE OUTPUT. Every sidecar carries a provenance header naming the
    endpoint, model, profile, and timestamp, and is stamped AI-GENERATED —
    REVIEW PRIOR TO USE. Each run also writes an xlsx report whose rows
    surface the drafted alt text, its character count, and anything the model
    flagged as an uncertain reading, so staff can triage instead of
    re-reading every file.

SIDECAR FORMAT (factory profile "Archival"):

    ================================================================================
    TEXT EQUIVALENT
    ================================================================================
    Source image:  <filename>
    Generated:     <timestamp> · DC Admin Suite · Image Description Generator 1.0
    Endpoint:      <name> (<kind>)
    Model:         <model>
    Profile:       <profile>
    Status:        AI-GENERATED — REVIEW PRIOR TO USE

    --- ALT TEXT ---------------------------------------------------------------
    (under 125 characters; this is the text that goes in an alt attribute)

    --- DESCRIPTION ------------------------------------------------------------
    --- TRANSCRIPTION ----------------------------------------------------------
    --- NOTES ------------------------------------------------------------------
    ================================================================================

The module writes the header itself — the model is only ever asked for the
--- SECTION --- blocks, so timestamps, model names, and file names can never
be hallucinated. A reply whose sections cannot be parsed is still saved (as a
DESCRIPTION block) and flagged in the report rather than discarded.

SOURCES: a folder of images (optionally recursive — Google Drive for Desktop
folders work like any other local folder), a Batch File Downloader run folder
(so descriptions come back tagged with Context ID / Article ID / record
title), or individually pasted paths.

RESTART SAFETY: images that already have a sidecar are skipped, so a stopped
or failed run can simply be started again. A checkbox overrides this.

Run standalone:  python3 modules/dc_image_describer.py --port 8799 \
                     --session config/session.json
Or launch it from the Main Menu.

v1.6 (2026-09-04) — identity refactor Step 3: this module no longer
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
    "id": "image-describer",
    "name": "Image Description Generator",
    "description": "Generate reviewable alt-text and transcription sidecar files for folders of scanned images, using a Gemini, OpenAI, or Claude endpoint.",
    "version": "1.8.0",
    "requires": ["openpyxl", "pymupdf"],
}

import argparse
import base64
import concurrent.futures
import glob
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------
DEFAULT_SESSION = Path(__file__).resolve().parent.parent / "config" / "session.json"

XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

# Batch File Downloader artifacts recognized during run-folder ingest.
DL_RUN_PREFIX = "DC_FileDownloads_"
DL_REPORT_GLOB = "DC_FileDownload_Report_*.xlsx"

SCAN_MAX_FILES = 5000       # hard cap per source scan (safety)
# Because requests stream, these are IDLE timeouts — the longest gap allowed
# between chunks — not caps on total generation time. A dense page can take
# several minutes to transcribe and that is fine; what must never happen is
# the connection sitting silent long enough for something in the middle to
# close it (see ai_generate).
AI_TIMEOUT = 180            # max seconds between chunks during a run
PREVIEW_TIMEOUT = 180       # same, for the single-image preview

# Pacing defaults. The Apps Script this replaces slept 5 s between requests
# and backed off 10 s → 20 s → 40 s → 80 s on HTTP 429/503; the same shape is
# used here, with both ends configurable in the UI because paid tiers do not
# need anything like that much headroom.
DEFAULT_DELAY = 2.0         # seconds between successful requests
DEFAULT_RETRIES = 4         # retries after a rate-limit / overload response
BACKOFF_START = 10.0        # first backoff wait, doubling per retry
BACKOFF_CAP = 120.0         # never wait longer than this between retries

# Image handling. Vision APIs reject very large payloads, and every provider
# downsamples internally anyway, so oversized images are re-encoded before
# sending. 2000 px on the long edge keeps small print and secretary hands
# legible while staying well inside every provider's limit.
DEFAULT_MAX_DIM = 2000
# What each provider actually charges for, as opposed to what it accepts.
# Gemini tiles the image at 768 px, so 2000x1494 is 3x2 = 6 tiles = 1,548
# input tokens while 1536x1147 is 2x2 = 4 tiles = 1,032. Anthropic rescales
# to about 1.15 megapixels regardless, so anything over roughly 1,470 px on
# the long edge is bandwidth paid for and tokens not counted.
#
# These are SHOWN, not applied: the shipped default stays 2000 px until the
# A/B on the densest archival pages says small print survives
# the cut. Transcription quality on this material is the product.
RECOMMENDED_MAX_DIM = {"gemini": 1536, "anthropic": 1470, "openai": 2000}
MAX_SEND_BYTES = 3_500_000  # re-encode anything larger than this
JPEG_QUALITY = 88

SIDECAR_EXT = ".txt"
LINE_WIDTH = 80


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
# Image formats. Providers accept jpeg / png / gif / webp directly; TIFF and
# BMP are common in digitization workflows but are NOT accepted by any of
# them, so those are always re-encoded through PyMuPDF before sending.
# ---------------------------------------------------------------------------
NATIVE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
CONVERT_EXTS = (".tif", ".tiff", ".bmp")
IMAGE_EXTS = NATIVE_EXTS + CONVERT_EXTS

_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".bmp": "image/bmp",
             ".tif": "image/tiff", ".tiff": "image/tiff"}


def guess_image_mime(ext: str) -> str:
    return _IMG_MIME.get((ext or "").lower(), "image/png")


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def needs_conversion(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CONVERT_EXTS


def _pkg(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def tool_status() -> dict:
    return {"pymupdf": _pkg("fitz"), "openpyxl": _pkg("openpyxl")}


# ---------------------------------------------------------------------------
# Description-profile registry — config/image_describer.json.
#
# Suite JSON-registry pattern: created from in-code defaults on first run,
# edited only through the UI, validated server-side, and backfilled on load
# so a file written by an earlier version never needs a hand-edit. A profile
# is a named prompt plus the alt-text budget the report checks against.
# ---------------------------------------------------------------------------
PROFILE_PATH = None         # set in main(); guarded for test imports

# The Archival prompt as it shipped in v1.2, frozen for one purpose: to tell
# an untouched factory prompt from one the user has edited. The comparison in
# backfill_profiles() is byte-exact, so this literal was lifted out of the
# v1.2 file mechanically and must never be re-wrapped or "tidied" — a single
# moved line break would make every saved profile look customised and stop
# the migration dead.
ARCHIVAL_PROMPT_V12 = """Role: You are an archivist and web-accessibility \
specialist (WCAG 2.1 AA) describing digitized library and archival materials.

Task: Describe the attached image so that someone who cannot see it receives \
the same information and function it carries. Reply with ONLY the sections \
below, in this order, each introduced by its rule line exactly as shown. Do \
not add any preamble, commentary, or closing remark.

--- ALT TEXT ---
One or two sentences, UNDER 125 CHARACTERS IN TOTAL, giving the core meaning \
and purpose of the image. Never begin with "Image of", "Picture of", "Photo \
of", or "Scan of" — assistive technology already announces that.

--- DESCRIPTION ---
A fuller prose description, two to six sentences: what the object is, its \
physical form and layout, its notable visual features, and its condition. \
Describe what is visible. Do not speculate about history the image does not \
show.

--- TRANSCRIPTION ---
Transcribe ALL legible text, preserving layout and reading order. Introduce \
each distinct zone with a bracketed label, for example [Left page - printed \
title page] or [Upper right]. Transcribe printed and handwritten text in the \
original language and spelling, keeping archaic forms, ligatures, and \
abbreviations as written. Where text is set in a second colour and that is \
meaningful, mark it (red). For text in a language other than English, follow \
the transcription with an expansion of any abbreviations and an English \
translation, each on its own labelled line. Omit this entire section, rule \
line included, if the image contains no text.

--- NOTES ---
Short "Label: value" lines for whichever of these apply: Object type, Title, \
Creator, Imprint, Date, Languages, Condition. Finish with an "Uncertain:" \
line naming any reading you are not confident about and why. Write \
"Uncertain: None." only when the whole image is cleanly legible.

Accuracy rules — these outrank completeness:
- Transcribe only what is actually legible. Write [illegible] for text you \
cannot read, and put [?] immediately after any single word you are unsure of.
- Never invent names, dates, places, or catalogue numbers. If a detail is not \
visible in the image, leave it out.
- If the image is purely decorative and carries no information, reply with \
the ALT TEXT section alone."""

ARCHIVAL_PROMPT = """Role: You are an archivist and web-accessibility \
specialist (WCAG 2.1 AA) describing digitized library and archival materials.

Task: Describe the attached image so that someone who cannot see it receives \
the same information and function it carries. Reply with ONLY the sections \
below, in this order, each introduced by its rule line exactly as shown. Do \
not add any preamble, commentary, or closing remark.

--- ALT TEXT ---
One or two sentences, UNDER 125 CHARACTERS IN TOTAL, giving the core meaning \
and purpose of the image. Never begin with "Image of", "Picture of", "Photo \
of", or "Scan of" — assistive technology already announces that.

--- DESCRIPTION ---
A fuller prose description, two to six sentences: what the object is, its \
physical form and layout, its notable visual features, and its condition. \
Describe what is visible. Do not speculate about history the image does not \
show.

--- TRANSCRIPTION ---
Transcribe ALL legible text, preserving layout and reading order. Introduce \
each distinct zone with a bracketed label, for example [Left page - printed \
title page] or [Upper right]. Transcribe printed and handwritten text in the \
original language and spelling, keeping archaic forms, ligatures, and \
abbreviations as written. Where text is set in a second colour and that is \
meaningful, mark it (red). Follow the transcription with one expanded \
reading of the passage as continuous text, not a word-by-word glossary, and \
an English translation of headings, colophons, captions and inscriptions \
only — not of the body text. Omit this entire section, rule line included, \
if the image contains no text.

--- NOTES ---
Short "Label: value" lines for whichever of these apply: Object type, Title, \
Creator, Imprint, Date, Languages, Condition. Finish with an "Uncertain:" \
line naming any reading you are not confident about and why. Write \
"Uncertain: None." only when the whole image is cleanly legible. If any zone \
of the image contains text you did not transcribe — because it is reversed, \
obscured, cropped, or too small — you MUST name that zone under Uncertain. \
Writing "Uncertain: None." asserts that every legible character in the image \
appears above.

Accuracy rules — these outrank completeness:
- Transcribe only what is actually legible. Write [illegible] for text you \
cannot read, and put [?] immediately after any single word you are unsure of.
- Never invent names, dates, places, or catalogue numbers. If a detail is not \
visible in the image, leave it out.
- Do not supply a date, place, printer or work identity that is not printed \
or written in the image. If you recognise the work, give it on a separate \
"Possible identification:" line under NOTES — never in Imprint, Date or Title.
- If the image is purely decorative and carries no information, reply with \
the ALT TEXT section alone."""

FACTORY_PROFILES = [
    {
        "name": "Archival",
        "description": ("Full archival record: alt text, prose description, "
                        "layout-preserving transcription with translations, "
                        "and cataloguing notes. Built for scanned books, "
                        "manuscripts, and catalogue cards."),
        "prompt": ARCHIVAL_PROMPT,
        "max_alt_chars": 125,
    },
    {
        "name": "Alt text only",
        "description": ("Just the alt attribute — for photographs and "
                        "illustrations that carry no text worth "
                        "transcribing."),
        "prompt": (
            "Role: You are a web accessibility expert specializing in WCAG "
            "2.1 AA compliance.\n\nTask: Write alt text for the attached "
            "image. Reply with ONLY the section below, introduced by its "
            "rule line exactly as shown, and nothing else.\n\n"
            "--- ALT TEXT ---\nOne or two sentences, UNDER 125 CHARACTERS "
            "IN TOTAL, conveying the core meaning and purpose of the image. "
            "Never begin with \"Image of\", \"Picture of\", or \"Photo of\". "
            "If the image contains text that matters to its meaning, work "
            "that text into the description. Do not invent detail that is "
            "not visible."),
        "max_alt_chars": 125,
    },
]

# The ceiling the request builders fall back to when none is supplied.
DEFAULT_MAX_TOKENS = 4000
# The REPLY LIMIT is a setting of the run, on the job form, not of the
# profile (1.0.1, decided after a measurement on a managed computer). On a
# thinking model the limit counts the reasoning trace as well as the
# answer, and the trace varies a great deal: one short alt text took 199
# thinking tokens in one run and 877 on the SAME image, profile and model
# minutes later. A limit set near typical consumption therefore fails
# unpredictably - the factory "Alt text only" at 300 refused 6 of 8 images
# in one run and 0 of 8 at 2,000 in the next. So the default is generous,
# and the operator can lower it for a first run and adjust it between runs
# on the form, where a run's settings belong. A profile saved by an older
# version may still carry a max_tokens key; it is ignored and dropped on
# the next save.
DEFAULT_REPLY_LIMIT = 8000
MIN_MAX_TOKENS = 64
# The validator, not the provider, was the binding constraint once the
# Archival default moved to 8,000. Gemini 3.x and Claude both accept far
# more than this; the cap exists to catch a typo, not to ration output.
MAX_MAX_TOKENS = 32000


def parse_reply_limit(raw):
    """(limit, error) for the job form's Reply limit. ONE function, called by
    /api/start and /api/preview alike (contract 11). Absent from the request
    means the default; present but blank or out of range is an error that
    names the field."""
    if raw is None:
        return DEFAULT_REPLY_LIMIT, ""
    field = "Reply limit (output tokens)"
    if str(raw).strip() == "":
        return None, ("{} is empty - enter a number from {} to {:,} "
                      "({:,} suits most runs).".format(
                          field, MIN_MAX_TOKENS, MAX_MAX_TOKENS,
                          DEFAULT_REPLY_LIMIT))
    try:
        limit = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, "{} must be a whole number of tokens.".format(field)
    if not MIN_MAX_TOKENS <= limit <= MAX_MAX_TOKENS:
        return None, ("{} must be between {} and {:,}.".format(
            field, MIN_MAX_TOKENS, MAX_MAX_TOKENS))
    return limit, ""

DEFAULT_PROFILE_CONFIG = {"profiles": FACTORY_PROFILES,
                          "default": "Archival"}


def profile_path() -> Path:
    if PROFILE_PATH is not None:
        return PROFILE_PATH
    return DEFAULT_SESSION.parent / "image_describer.json"


# One-shot notice for the page: set when a saved Archival prompt was found
# to be customised and therefore left alone. Read (and cleared) by
# GET /api/profiles, so the user is told once rather than at every poll.
_NOTICE_LOCK = threading.Lock()
PROMPT_NOTICE = ""
STALE_PROMPT_NOTICE = (
    "Your Archival prompt predates v1.3. The factory prompt now asks the "
    "model to declare zones it did not transcribe — open Manage profiles "
    "to compare.")


def take_prompt_notice() -> str:
    """The pending notice, cleared as it is handed over."""
    global PROMPT_NOTICE
    with _NOTICE_LOCK:
        msg, PROMPT_NOTICE = PROMPT_NOTICE, ""
    return msg


def _set_prompt_notice(msg: str) -> None:
    global PROMPT_NOTICE
    with _NOTICE_LOCK:
        PROMPT_NOTICE = msg


def backfill_profiles(cfg, migrate=False) -> dict:
    """Upgrade older config files silently — never require a hand-edit.

    `migrate` is off by default because this function does double duty: it
    also normalises a candidate registry arriving on POST /api/profiles, and
    rewriting a prompt the user is in the middle of saving would be the
    opposite of helpful. load_profiles() passes migrate=True.
    """
    out = {"profiles": [], "default": ""}
    if isinstance(cfg, dict):
        raw = cfg.get("profiles")
        default = str(cfg.get("default", "")).strip()
    else:
        raw, default = None, ""
    if not isinstance(raw, list) or not raw:
        return {"profiles": [dict(p) for p in FACTORY_PROFILES],
                "default": DEFAULT_PROFILE_CONFIG["default"]}
    upgraded, stale = [], []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            budget = int(p.get("max_alt_chars", 125))
        except (TypeError, ValueError):
            budget = 125
        prompt = str(p.get("prompt", ""))
        name = str(p.get("name", "")).strip()
        # The factory Archival prompt, and ONLY the factory Archival prompt,
        # is upgraded in place. The comparison is byte-exact: anything the
        # user has touched — including by a single space — is left alone and
        # raises a notice instead, because a customised prompt is work.
        if migrate and name == "Archival" and prompt == ARCHIVAL_PROMPT_V12:
            prompt = ARCHIVAL_PROMPT
            upgraded.append(name)
        elif (migrate and name == "Archival"
                and prompt not in (ARCHIVAL_PROMPT, ARCHIVAL_PROMPT_V12)):
            stale.append(name)
        out["profiles"].append({
            "name": name,
            "description": str(p.get("description", "")).strip(),
            "prompt": prompt,
            "max_alt_chars": budget,
        })
    names = [p["name"] for p in out["profiles"]]
    out["default"] = default if default in names else (names[0] if names
                                                       else "")
    if migrate:
        out["_upgraded"] = upgraded
        out["_stale"] = stale
    return out


def load_profiles() -> dict:
    try:
        raw = json.loads(profile_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    cfg = backfill_profiles(raw, migrate=True)
    upgraded = cfg.pop("_upgraded", [])
    stale = cfg.pop("_stale", [])
    if raw is None:                     # first run — seed the file
        try:
            save_profiles(cfg)
        except OSError:
            pass
    elif upgraded:
        try:
            save_profiles(cfg)
            log("Factory prompt updated to v1.3 for the untouched "
                "'{}' profile (it now asks the model to declare zones it "
                "did not transcribe).".format("', '".join(upgraded)))
        except OSError as e:
            log("NOTE: could not save the updated factory prompt: "
                "{}".format(e))
    if stale:
        _set_prompt_notice(STALE_PROMPT_NOTICE)
    return cfg


def save_profiles(cfg: dict) -> None:
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def validate_profiles(cfg) -> str:
    """Return "" when valid, else a user-facing message (400)."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("profiles"), list):
        return "Bad profile registry format."
    if not cfg["profiles"]:
        return "Keep at least one description profile."
    seen = set()
    for p in cfg["profiles"]:
        if not isinstance(p, dict):
            return "Bad profile entry."
        name = str(p.get("name", "")).strip()
        if not name:
            return "Every profile needs a name."
        if name.lower() in seen:
            return "Duplicate profile name: " + name
        seen.add(name.lower())
        prompt = str(p.get("prompt", "")).strip()
        if len(prompt) < 40:
            return ("Profile '{}': the prompt is too short to be useful."
                    .format(name))
        if "--- ALT TEXT ---" not in prompt.upper().replace("-- -", "---"):
            return ("Profile '{}': the prompt must ask for an "
                    "'--- ALT TEXT ---' section, or the sidecar will have "
                    "no alt text.".format(name))
        try:
            budget = int(p.get("max_alt_chars", 125))
        except (TypeError, ValueError):
            return "Profile '{}': alt-text limit must be a number.".format(
                name)
        if not 40 <= budget <= 500:
            return ("Profile '{}': alt-text limit must be between 40 and "
                    "500 characters.".format(name))
    default = str(cfg.get("default", "")).strip()
    if default and default not in [str(p.get("name", "")).strip()
                                   for p in cfg["profiles"]]:
        return "Default profile '{}' is not in the list.".format(default)
    return ""


def find_profile(cfg: dict, name: str):
    for p in cfg.get("profiles", []):
        if p.get("name") == name:
            return p
    return None


# ---------------------------------------------------------------------------
# AI endpoint registry — SHARED with the OCR & Accessibility Toolkit
# (config/ocr_toolkit.json), so a key added in either module works in both
# and a Gemini endpoint can be swapped in later with no code change. Read and
# written with the same shape and the same validation rules.
#
# Keys are stored in PLAIN TEXT in config/ — the UI says so.
# ---------------------------------------------------------------------------
AI_CONFIG_PATH = None       # set in main(); guarded for test imports
AI_KINDS = ("gemini", "openai", "anthropic")
DEFAULT_AI_CONFIG = {"endpoints": [], "default": ""}


def resolve_ai_config_path(session, session_file) -> Path:
    """Where the AI endpoint registry lives, most authoritative first.

    1. `ai_endpoints_path` from session.json — the shell tells us outright.
    2. config/ai_endpoints.json beside the session file — a module launched
       standalone against a v1.3 suite.
    3. config/ocr_toolkit.json — the pre-v1.3 location, so this module still
       works if it is dropped into an older suite.
    """
    stated = str((session or {}).get("ai_endpoints_path", "")).strip()
    # Trust the shell's path only if it is actually there. The suite is
    # transportable, so a session.json can name a path from the machine or
    # folder it was written on; falling through to the local config/ folder
    # keeps a moved or copied suite working instead of silently reporting
    # no endpoints.
    if stated and Path(stated).exists():
        return Path(stated)
    config_dir = Path(session_file).resolve().parent
    modern = config_dir / "ai_endpoints.json"
    if modern.exists():
        return modern
    legacy = config_dir / "ocr_toolkit.json"
    return legacy if legacy.exists() else modern


def ai_config_path() -> Path:
    if AI_CONFIG_PATH is not None:
        return AI_CONFIG_PATH
    return DEFAULT_SESSION.parent / "ai_endpoints.json"


def backfill_ai_config(cfg: dict) -> dict:
    out = dict(DEFAULT_AI_CONFIG)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in out if k in cfg})
    eps = []
    for ep in (out.get("endpoints") or []):
        if not isinstance(ep, dict):
            continue
        eps.append({
            "name": str(ep.get("name", "")).strip(),
            "kind": str(ep.get("kind", "gemini")).strip().lower(),
            "url": str(ep.get("url", "")).strip(),
            "model": str(ep.get("model", "")).strip(),
            "api_key": str(ep.get("api_key", "")).strip(),
            # Read-only here. The shell owns the registry (Settings > AI
            # endpoints); this module never writes it back. A field missing
            # from this whitelist is silently dropped on read.
            "prompt_cache": bool(ep.get("prompt_cache", False)),
        })
    out["endpoints"] = eps
    out["default"] = str(out.get("default", "")).strip()
    if out["default"] and out["default"] not in [e["name"] for e in eps]:
        out["default"] = ""
    return out


def load_ai_config() -> dict:
    try:
        raw = json.loads(ai_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return backfill_ai_config(raw)


def find_endpoint(cfg: dict, name: str):
    for ep in cfg.get("endpoints", []):
        if ep.get("name") == name:
            return ep
    return None


# ---------------------------------------------------------------------------
# AI client — urllib only. One builder/parser pair per provider shape; every
# builder returns (url, headers, body) so they are unit-testable with no
# network access. AIError carries the HTTP status so the retry loop can tell
# a rate limit (retry) from a bad key (give up immediately).
# ---------------------------------------------------------------------------
# The streaming AI client below is DUPLICATED, verbatim, in the other AI
# module. That duplication is deliberate — the module contract keeps every
# module a single self-contained file that can be dropped in or removed on
# its own, so there is no shared library to import. This constant is how the
# copies are kept honest: bump it in BOTH files whenever the client changes,
# and a mismatch is the signal that one copy has drifted.
#     1.0  non-streaming (withdrawn — see the v1.1 timeout incident)
#     1.1  streaming + transient-retry client
#     1.2  token-usage capture, per-request max_tokens, optional Anthropic
#          prompt-prefix caching
#     1.3  early-stop detection (finishReason / stop_reason / finish_reason)
#     1.4  reasoning-trace tokens captured as usage["think"]
#     1.5  billed usage carried on AIError; retries sum every attempt
AI_CLIENT_VERSION = "1.5"

def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(kind, usage) -> dict:
    """Provider token counts -> {"in", "out", "think", "cache_read"}.

    Zeros when the provider said nothing, so no call site has to know which
    provider it is talking to. "in" is the input billed at full rate and
    "cache_read" the discounted remainder; the providers disagree about
    whether cached tokens sit inside or outside their own input figure, and
    that is resolved here rather than wherever the numbers are used.

    "think" is the model's reasoning trace. It is billed as output and it
    counts against the reply ceiling, and the providers disagree about it
    too — which is not a tidiness problem, it is the thing that hid seven
    truncated pages for two days:

      · Gemini reports thoughtsTokenCount SEPARATELY from
        candidatesTokenCount, so "out" adds the two together. Reporting
        candidates alone showed a comfortable 2,381-token maximum against a
        4,000 ceiling while pages were being refused for max_tokens.
      · OpenAI puts reasoning_tokens INSIDE completion_tokens, so there
        "think" is a subset of "out", not an addition to it.
      · Anthropic folds thinking into output_tokens and publishes no
        separate figure, so "think" is 0 — which means "not reported", not
        "none".
    """
    u = usage if isinstance(usage, dict) else {}
    if kind == "gemini":
        cached = _int(u.get("cachedContentTokenCount"))
        think = _int(u.get("thoughtsTokenCount"))
        return {"in": max(0, _int(u.get("promptTokenCount")) - cached),
                "out": _int(u.get("candidatesTokenCount")) + think,
                "think": think,
                "cache_read": cached}
    if kind == "anthropic":
        # input_tokens EXCLUDES both cache counters; a cache write is billed
        # above the input rate but is rare enough to fold into "in".
        return {"in": (_int(u.get("input_tokens"))
                       + _int(u.get("cache_creation_input_tokens"))),
                "out": _int(u.get("output_tokens")),
                "think": 0,
                "cache_read": _int(u.get("cache_read_input_tokens"))}
    # openai-compatible: prompt_tokens INCLUDES the cached portion, and
    # completion_tokens already INCLUDES the reasoning tokens.
    cached = _int((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
    details = u.get("completion_tokens_details") or {}
    return {"in": max(0, _int(u.get("prompt_tokens")) - cached),
            "out": _int(u.get("completion_tokens")),
            "think": _int(details.get("reasoning_tokens")),
            "cache_read": cached}


# Provider signals that generation ended NORMALLY. Everything else — a
# length cap, a recitation or safety stop, a content filter — means the model
# gave up part-way through and the bytes already received are an incomplete
# answer that ends mid-sentence.
#
# This is a different failure from the dropped connection v1.1 was written
# for, and it defeated that guard completely. Gemini has no stream
# terminator, so `done` is true from the first byte and a tidy EOF after an
# early stop is indistinguishable from a finished reply. In one 78-image
# archival run that cost seven pages: every one ended mid-word,
# every one was written to disk, and the report called all of them
# "Described" with zero warnings.
_NORMAL_STOP = {
    "gemini": ("stop",),
    "anthropic": ("end_turn", "stop_sequence"),
    "openai": ("stop",),
}


def stop_reason(kind, obj):
    """The provider's finish reason from one streamed event, or None.

    Providers report it in three different places and spell it three
    different ways; callers get one lowercase string or nothing.
    """
    if not isinstance(obj, dict):
        return None
    if kind == "gemini":
        try:
            reason = obj["candidates"][0].get("finishReason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    elif kind == "anthropic":
        if obj.get("type") != "message_delta":
            return None
        reason = (obj.get("delta") or {}).get("stop_reason")
    else:
        try:
            reason = obj["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    return str(reason).strip().lower() if reason else None


def collect_usage(kind, obj, usage):
    """Fold one streamed event into the running raw usage payload."""
    if not isinstance(obj, dict):
        return usage
    if kind == "anthropic":
        # message_start carries the input and cache counters; message_delta
        # carries the running output count.
        if obj.get("type") == "message_start":
            usage.update((obj.get("message") or {}).get("usage") or {})
        elif obj.get("type") == "message_delta":
            usage.update(obj.get("usage") or {})
        return usage
    if kind == "gemini":
        # Every SSE chunk carries usageMetadata; the last one is
        # authoritative, so replace rather than merge.
        if isinstance(obj.get("usageMetadata"), dict):
            return dict(obj["usageMetadata"])
        return usage
    # openai-compatible: one final chunk carries usage, and only when
    # stream_options.include_usage was set on the request.
    if isinstance(obj.get("usage"), dict):
        return dict(obj["usage"])
    return usage


class AIError(Exception):
    """User-facing AI request failure (endpoint, auth, or parse).

    `status`    HTTP status when the endpoint answered with one.
    `transient` True for a connection that dropped, reset, or timed out —
                no status code, but worth retrying. Failing to mark these
                is what made the first release give up instantly on every
                dropped connection (Retries=0 in every failed report row).
    """

    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.retry_after = None
        # Client 1.5: what the provider reported billing for this request
        # (or for every attempt, once ai_generate_with_retry gives up), so a
        # failed row can record what it cost. None means "not reported".
        self.usage = None
        self.retries = 0


class FileError(Exception):
    """Per-image failure with user-facing advice; never kills the run."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def build_gemini_request(ep, prompt, image_bytes=None,
                         image_mime="image/png", stream=False,
                         max_tokens=None):
    url = ep["url"].rstrip("/")
    if ":generatecontent" in url.lower() or ":streamgeneratecontent" \
            in url.lower():
        if stream:
            url = re.sub(r":generateContent", ":streamGenerateContent", url,
                         flags=re.I)
    else:
        url = "{}/models/{}:{}".format(
            url, ep["model"],
            "streamGenerateContent" if stream else "generateContent")
    if stream and "alt=sse" not in url:
        url += ("&" if "?" in url else "?") + "alt=sse"
    headers = {"Content-Type": "application/json",
               "x-goog-api-key": ep["api_key"]}
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": image_mime,
                                      "data": _b64(image_bytes)}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {
                "maxOutputTokens": int(max_tokens or DEFAULT_MAX_TOKENS)}}
    return url, headers, body


def gemini_delta(obj):
    """Incremental text from one Gemini SSE event."""
    try:
        parts = obj["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        return ""


def parse_gemini_response(obj) -> str:
    try:
        parts = obj["candidates"][0]["content"]["parts"]
        return "\n".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected Gemini response shape.")


def build_openai_request(ep, prompt, image_bytes=None,
                         image_mime="image/png", stream=False,
                         max_tokens=None):
    url = ep["url"].rstrip("/")
    if "chat/completions" not in url.lower():
        url = url + "/chat/completions"
    # Both auth header styles: OpenAI reads Authorization, Azure reads
    # api-key; each ignores the other.
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + ep["api_key"],
               "api-key": ep["api_key"]}
    if image_bytes:
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {
                       "url": "data:{};base64,{}".format(image_mime,
                                                         _b64(image_bytes))}}]
    else:
        content = prompt
    body = {"model": ep["model"],
            "messages": [{"role": "user", "content": content}],
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS)}
    if stream:
        body["stream"] = True
        # Usage is opt-in on this shape: without it the final chunk carries
        # no token counts at all and every cost cell would be blank.
        body["stream_options"] = {"include_usage": True}
    return url, headers, body


def openai_delta(obj):
    try:
        return obj["choices"][0].get("delta", {}).get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def parse_openai_response(obj) -> str:
    try:
        return (obj["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected OpenAI-compatible response shape.")


def build_anthropic_request(ep, prompt, image_bytes=None,
                            image_mime="image/png", stream=False,
                            max_tokens=None):
    url = ep["url"].rstrip("/")
    if not url.lower().endswith("/messages"):
        url = url + "/v1/messages"
    headers = {"Content-Type": "application/json",
               "x-api-key": ep["api_key"],
               "anthropic-version": "2023-06-01"}
    image_block = None
    if image_bytes:
        image_block = {"type": "image",
                       "source": {"type": "base64",
                                  "media_type": image_mime,
                                  "data": _b64(image_bytes)}}
    text_block = {"type": "text", "text": prompt}
    # Image first is Anthropic's own recommendation for a single-image
    # prompt, and it is the default here for that reason. Caching needs the
    # opposite order — the cached prefix must come first — so the two are a
    # genuine trade, decided per endpoint rather than assumed. Note also
    # that the prompt must clear the model's minimum cacheable length (512
    # tokens on Opus, 1,024 on Sonnet, 4,096 on Haiku); below it the request
    # is simply processed uncached, with no error to notice.
    if ep.get("prompt_cache"):
        text_block["cache_control"] = {"type": "ephemeral"}
        content = [text_block] + ([image_block] if image_block else [])
    else:
        content = ([image_block] if image_block else []) + [text_block]
    body = {"model": ep["model"],
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
            "messages": [{"role": "user", "content": content}]}
    if stream:
        body["stream"] = True
    return url, headers, body


def anthropic_delta(obj):
    try:
        if obj.get("type") == "content_block_delta":
            return obj.get("delta", {}).get("text") or ""
    except (AttributeError, TypeError):
        pass
    return ""


def parse_anthropic_response(obj) -> str:
    try:
        return "\n".join(b.get("text", "") for b in obj["content"]
                         if b.get("type") == "text").strip()
    except (KeyError, TypeError):
        raise AIError("Unexpected Anthropic response shape.")


_AI_BUILDERS = {
    "gemini": (build_gemini_request, parse_gemini_response, gemini_delta),
    "openai": (build_openai_request, parse_openai_response, openai_delta),
    "anthropic": (build_anthropic_request, parse_anthropic_response,
                  anthropic_delta),
}

# Provider signals that a stream finished cleanly. Without one of these we
# must assume the connection was cut mid-answer and retry, rather than save
# a transcription that stops in the middle of a word.
_STREAM_DONE = {"openai": ("[DONE]",), "anthropic": ("message_stop",),
                "gemini": ()}   # Gemini has no terminator; clean EOF is done


def _http_error(e):
    """urllib HTTPError -> AIError carrying status and any Retry-After."""
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        pass
    try:
        retry_after = float(e.headers.get("Retry-After") or 0) or None
    except (TypeError, ValueError, AttributeError):
        retry_after = None
    err = AIError("HTTP {} from endpoint: {}".format(e.code, detail),
                  status=e.code)
    err.retry_after = retry_after
    return err


def ai_generate(ep, prompt, image_bytes=None, image_mime="image/png",
                timeout=AI_TIMEOUT, stream=True, max_tokens=None):
    """One request to the endpoint. Raises AIError (with .status/.transient).

    Returns (text, usage) where usage is the normalised token dict — see
    normalize_usage. Before v1.2 the usage payload rode in on every stream
    and was thrown away, which is why no report could say what a run cost.

    STREAMING IS THE DEFAULT, and it is not an optimisation — it is what
    makes long answers possible at all. With a non-streaming request the
    server sends nothing until the whole answer is generated, so the
    connection sits idle; any intermediary with an idle-read timeout (60 s is
    a very common default) kills it mid-generation and the client sees
    "Remote end closed connection without response". That is exactly what
    happened to every text-dense page in the first real run: everything that
    needed more than ~60 s of generation failed, deterministically, no matter
    how often it was retried. Streaming keeps bytes flowing the whole time,
    so the idle timer never starts.

    stream=False is kept only for the tiny endpoint-test ping.
    """
    kind = (ep.get("kind") or "").lower()
    if kind not in _AI_BUILDERS:
        raise AIError("Unknown endpoint kind: " + kind)
    build, parse, delta = _AI_BUILDERS[kind]
    url, headers, body = build(ep, prompt, image_bytes, image_mime, stream,
                               max_tokens)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST")
    try:
        if not stream:
            # The endpoint-test ping only. Not worth instrumenting: it
            # reports zeros rather than pretending to a measurement.
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (parse(json.loads(
                    resp.read().decode("utf-8", "replace"))),
                    normalize_usage(kind, None))
        chunks = []
        usage = {}
        finish = None
        done = not _STREAM_DONE.get(kind)   # providers with no terminator
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue            # SSE comment / keep-alive
                if line.startswith("event:"):
                    if line.split(":", 1)[1].strip() in _STREAM_DONE[kind]:
                        done = True
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line.split(":", 1)[1].strip()
                if payload in _STREAM_DONE.get(kind, ()):
                    done = True
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "error":
                    raise AIError("Endpoint error during stream: {}".format(
                        str(obj.get("error"))[:200]))
                if isinstance(obj, dict) and obj.get("error"):
                    raise AIError("Endpoint error during stream: {}".format(
                        str(obj["error"])[:200]))
                if isinstance(obj, dict) and obj.get("type") == "message_stop":
                    done = True
                usage = collect_usage(kind, obj, usage)
                finish = stop_reason(kind, obj) or finish
                chunks.append(delta(obj))
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except urllib.error.URLError as e:
        err = AIError("Could not reach endpoint: {}".format(e.reason))
        err.transient = True
        raise err
    except (ConnectionError, TimeoutError, socket.timeout) as e:
        err = AIError("Connection to the endpoint failed: {}".format(e))
        err.transient = True
        raise err
    except AIError:
        raise
    except (ValueError, OSError) as e:
        err = AIError("Endpoint request failed: {}".format(e))
        err.transient = True
        raise err
    text = "".join(chunks)
    if finish and finish not in _NORMAL_STOP.get(kind, ()):
        # NOT transient: a length cap or a recitation stop is deterministic,
        # and retrying it four times only spends the backoff. The row fails
        # with the provider's own reason in it, which is what tells a user
        # to escalate this page rather than re-run it.
        err = AIError(
            "The endpoint stopped generating early after {} characters "
            "(the provider gave the reason '{}'), so the reply is "
            "incomplete".format(len(text), finish))
        err.usage = normalize_usage(kind, usage)     # billed all the same
        raise err
    if not done:
        # Cut off mid-answer. Retry rather than save a truncated
        # transcription that looks complete but stops in the middle.
        err = AIError("The endpoint's reply was cut off after {} characters "
                      "(stream ended without a completion signal)"
                      .format(len(text)))
        err.transient = True
        err.usage = normalize_usage(kind, usage)
        raise err
    if not text.strip():
        err = AIError("The endpoint returned an empty reply")
        err.usage = normalize_usage(kind, usage)
        raise err
    return text, normalize_usage(kind, usage)


def sum_usage(total, more):
    """Sum two normalised usage dicts; None counts as nothing reported."""
    out = dict(total or {"in": 0, "out": 0, "think": 0, "cache_read": 0})
    for k, v in (more or {}).items():
        out[k] = out.get(k, 0) + _int(v)
    return out


# HTTP statuses worth waiting out: rate limit, and the various "busy"
# responses providers return under load.
RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)


def is_retryable(err) -> bool:
    """A rate limit, an overloaded provider, or a connection that dropped.
    NOT a bad key or an unknown model — retrying those just wastes time."""
    return (getattr(err, "status", None) in RETRY_STATUSES
            or bool(getattr(err, "transient", False)))


def ai_generate_with_retry(ep, prompt, image_bytes, image_mime,
                           max_retries, sleep_fn, log_fn,
                           timeout=AI_TIMEOUT, max_tokens=None):
    """ai_generate plus exponential backoff.

    Returns (text, usage, retries_used). Raises AIError once retries are
    exhausted
    or immediately for an error retrying cannot fix (bad key, bad model).
    sleep_fn must honour Pause/Stop so a backoff never blocks the UI.
    """
    # Client 1.5: usage is the SUM over every attempt the provider reported
    # on, success or failure, because each one was billed. Before, a retried
    # success reported only its last attempt and a failure reported nothing.
    wait = BACKOFF_START
    attempt = 0
    billed = None
    while True:
        try:
            text, usage = ai_generate(ep, prompt, image_bytes, image_mime,
                                      timeout=timeout,
                                      max_tokens=max_tokens)
            return text, sum_usage(billed, usage), attempt
        except AIError as e:
            if e.usage is not None:
                billed = sum_usage(billed, e.usage)
            if not is_retryable(e) or attempt >= max_retries:
                e.usage = billed
                e.retries = attempt
                raise
            attempt += 1
            pause = getattr(e, "retry_after", None) or wait
            pause = min(pause, BACKOFF_CAP)
            reason = ("returned {}".format(e.status)
                      if getattr(e, "status", None) else "connection problem")
            log_fn("    endpoint {} — waiting {:.0f}s, retry {}/{}".format(
                reason, pause, attempt, max_retries))
            sleep_fn(pause)
            wait = min(wait * 2, BACKOFF_CAP)


# ---------------------------------------------------------------------------
# Image preparation. Providers accept only jpeg/png/gif/webp and reject large
# payloads, so anything in a scanning format (TIFF/BMP) or over the size
# budget is re-encoded through PyMuPDF. Returns (bytes, mime, note).
# ---------------------------------------------------------------------------
def image_dimensions(path):
    """(width, height) or (0, 0) when they cannot be read."""
    try:
        import fitz
    except ImportError:
        return 0, 0
    try:
        doc = fitz.open(path)
        try:
            r = doc[0].rect
            return int(r.width), int(r.height)
        finally:
            doc.close()
    except Exception:
        return 0, 0


def reencode_image(path, max_dim):
    """Downscale/convert via PyMuPDF. Returns (jpeg_or_png_bytes, mime)."""
    import fitz
    doc = fitz.open(path)
    try:
        page = doc[0]
        rect = page.rect
        longest = max(rect.width, rect.height) or 1
        scale = min(1.0, float(max_dim) / longest)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    finally:
        doc.close()
    # JPEG cannot carry alpha, and CMYK needs converting to RGB first.
    try:
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.colorspace is not None and pix.colorspace.n == 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
    except Exception:
        pass
    try:
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY), "image/jpeg"
    except (TypeError, ValueError, RuntimeError):
        return pix.tobytes("png"), "image/png"


def prepare_image(path, max_dim, have_pymupdf):
    """Bytes ready to send, plus the mime type and a note for the log."""
    ext = os.path.splitext(path)[1].lower()
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise FileError("Could not read the image: {}".format(e))
    if not raw:
        raise FileError("The image file is empty (0 bytes).")
    w, h = (image_dimensions(path) if have_pymupdf else (0, 0))
    oversize = len(raw) > MAX_SEND_BYTES or (max(w, h) > max_dim > 0)
    if ext in NATIVE_EXTS and not oversize:
        return raw, guess_image_mime(ext), ""
    if not have_pymupdf:
        if ext in CONVERT_EXTS:
            raise FileError(
                "{} images must be converted before sending and PyMuPDF is "
                "not installed — install it, or convert to JPEG/PNG first"
                .format(ext.lstrip(".").upper()))
        raise FileError(
            "Image is {:.1f} MB, above the {:.1f} MB send limit, and PyMuPDF "
            "is not installed to downscale it".format(
                len(raw) / 1e6, MAX_SEND_BYTES / 1e6))
    try:
        data, mime = reencode_image(path, max_dim)
    except Exception as e:
        raise FileError("Could not re-encode the image ({}: {})".format(
            e.__class__.__name__, e))
    if len(data) > MAX_SEND_BYTES * 1.5:
        raise FileError("Image is still {:.1f} MB after downscaling — lower "
                        "the maximum dimension".format(len(data) / 1e6))
    note = "converted from {}".format(ext.lstrip(".").upper()) \
        if ext in CONVERT_EXTS else "downscaled to {} px".format(max_dim)
    return data, mime, note


# ---------------------------------------------------------------------------
# Source scanning. Every scan produces entry dicts:
#   {path, rel, name, size, ctx, article, title, state, admin_url, sidecar}
# rel = the path preserved when mirroring into a separate output folder.
# Sidecar .txt files and DC_* suite artifacts are never queued as input.
# ---------------------------------------------------------------------------
_ARTIFACT_RE = re.compile(r"^DC_[A-Za-z]+_?.*\.(xlsx|txt|xml)$", re.I)

# A DC context slug: lowercase, no spaces. Used to decide whether a folder
# name is safe to record as a Context ID — digitization folders are commonly
# named for the structure they came from (photo-gallery19/), but an ordinary
# folder of photos ("Scans May 2026") must not be mistaken for one.
_CTX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,60}$")
# Article ID as written by the download tools: an explicit _article_NNNN, or
# the Batch File Downloader's <context>_<article>_<kind>_… convention.
_ARTICLE_RE = re.compile(r"_article_(\d+)", re.I)


def is_suite_artifact(name: str) -> bool:
    return bool(_ARTIFACT_RE.match(name))


def sidecar_for(path: str) -> str:
    return os.path.splitext(path)[0] + SIDECAR_EXT


def infer_meta(path: str, root: str) -> dict:
    """Context ID / Article ID inferred from a plain folder's own layout.

    Digitization folders are usually named for the DC structure they came
    from, with one subfolder per context, so a plain folder scan can still
    produce a report that links back to the repository — without the file
    ever having passed through the Batch File Downloader.
    """
    meta = {}
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if parent == os.path.basename(os.path.abspath(root)) or not parent:
        parent = os.path.basename(os.path.abspath(root))
    if _CTX_RE.match(parent or ""):
        meta["ctx"] = parent
    name = os.path.splitext(os.path.basename(path))[0]
    m = _ARTICLE_RE.search(name)
    if m:
        meta["article"] = m.group(1)
    elif meta.get("ctx") and name.startswith(meta["ctx"] + "_"):
        tail = name[len(meta["ctx"]) + 1:].split("_", 1)[0]
        if tail.isdigit():
            meta["article"] = tail
    return meta


def _entry(path, rel, meta=None):
    meta = meta or {}
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"path": path, "rel": rel.replace(os.sep, "/"),
            "name": os.path.basename(path), "size": size,
            "sidecar": os.path.isfile(sidecar_for(path)),
            "ctx": meta.get("ctx", ""), "article": meta.get("article", ""),
            "title": meta.get("title", ""), "state": meta.get("state", ""),
            "admin_url": meta.get("admin_url", "")}


# Extensions that legitimately sit alongside images in a digitization
# folder and are not worth reporting as "skipped" (spreadsheets from the
# batch-revise workflow, existing sidecars, and the like).
_EXPECTED_COMPANIONS = (".txt", ".xls", ".xlsx", ".csv", ".pdf", ".md",
                        ".json", ".db")


def _is_noteworthy_skip(name: str) -> bool:
    """True for a non-image file a user would want flagged rather than
    silently ignored — e.g. a failed download saved with no real extension,
    which is an image that is MISSING, not an image to skip."""
    ext = os.path.splitext(name)[1].lower()
    return ext not in _EXPECTED_COMPANIONS


def scan_folder(folder: str, recursive: bool):
    """Queue every image in a plain folder (Drive for Desktop folders behave
    like any other local folder). Returns (entries, skipped_names)."""
    entries, skipped = [], []

    def consider(path, rel):
        fname = os.path.basename(path)
        if fname.startswith(".") or is_suite_artifact(fname):
            return
        if not is_image(path):
            if _is_noteworthy_skip(fname):
                skipped.append(rel.replace(os.sep, "/"))
            return
        entries.append(_entry(path, rel, infer_meta(path, folder)))

    if recursive:
        for base, dirs, files in os.walk(folder):
            dirs.sort()
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in sorted(files):
                path = os.path.join(base, fname)
                consider(path, os.path.relpath(path, folder))
                if len(entries) >= SCAN_MAX_FILES:
                    return entries, skipped
    else:
        for fname in sorted(os.listdir(folder)):
            path = os.path.join(folder, fname)
            if not os.path.isfile(path):
                continue
            consider(path, fname)
            if len(entries) >= SCAN_MAX_FILES:
                break
    return entries, skipped


def scan_paths(text: str):
    """Parse an individual-paths textarea. Returns (entries, bad_lines)."""
    entries, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        path = os.path.expanduser(line)
        if os.path.isfile(path) and is_image(path):
            entries.append(_entry(path, os.path.basename(path)))
        else:
            bad.append(line)
        if len(entries) >= SCAN_MAX_FILES:
            break
    return entries, bad


def _header_map(ws):
    """Header text → 1-based column index for row 1 (columns are located by
    name, never by position, so downloader report changes stay safe)."""
    return {str(c.value).strip(): i + 1
            for i, c in enumerate(ws[1]) if c.value is not None}


def load_downloader_meta(report_path: str) -> dict:
    """Saved Filename → record metadata from one per-structure report."""
    from openpyxl import load_workbook
    wb = load_workbook(report_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        cols = _header_map(ws)
        if any(h not in cols for h in ("Saved Filename", "Article ID",
                                       "Title")):
            return {}
        meta = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            def val(header):
                idx = cols.get(header)
                if not idx or idx > len(row):
                    return ""
                v = row[idx - 1]
                return "" if v is None else str(v)
            saved = val("Saved Filename")
            if saved:
                meta[saved] = {"article": val("Article ID"),
                               "title": val("Title"),
                               "state": val("Record State"),
                               "admin_url": val("Admin Record URL")}
        return meta
    finally:
        wb.close()


def scan_run_folder(folder: str):
    """Queue the images in a Batch File Downloader run. `folder` is either a
    DC_FileDownloads_* run folder or a folder holding one or more of them
    (newest picked). Returns (entries, run_folder); raises ValueError with a
    user-facing message when no run is found."""
    base = os.path.basename(os.path.normpath(folder))
    if base.startswith(DL_RUN_PREFIX):
        run = folder
    else:
        runs = [r for r in sorted(
            glob.glob(os.path.join(folder, DL_RUN_PREFIX + "*")),
            key=os.path.basename, reverse=True) if os.path.isdir(r)]
        if not runs:
            raise ValueError(
                "No {}* run folder found in {} — point this at a Batch File "
                "Downloader run (or its parent folder).".format(
                    DL_RUN_PREFIX, folder))
        run = runs[0]
    entries, skipped = [], []
    for sub in sorted(os.listdir(run)):
        sub_path = os.path.join(run, sub)
        if not os.path.isdir(sub_path):
            continue
        meta_by_name = {}
        for rep in glob.glob(os.path.join(sub_path, DL_REPORT_GLOB)):
            try:
                meta_by_name.update(load_downloader_meta(rep))
            except Exception:
                pass    # a damaged report never blocks the image queue
        for fname in sorted(os.listdir(sub_path)):
            path = os.path.join(sub_path, fname)
            if (fname.startswith(".") or is_suite_artifact(fname)
                    or not os.path.isfile(path)):
                continue
            if not is_image(path):
                if _is_noteworthy_skip(fname):
                    skipped.append("{}/{}".format(sub, fname))
                continue
            meta = dict(meta_by_name.get(fname, {}))
            meta["ctx"] = sub
            if not meta.get("article"):
                meta.update({k: v for k, v in
                             infer_meta(path, sub_path).items()
                             if k == "article"})
            entries.append(_entry(path, os.path.join(sub, fname), meta))
            if len(entries) >= SCAN_MAX_FILES:
                return entries, run, skipped
    return entries, run, skipped


# ---------------------------------------------------------------------------
# Reply parsing and sidecar assembly.
#
# The model is asked for --- SECTION --- blocks only; the module writes the
# provenance header itself so a timestamp, model name, or file name can never
# be hallucinated. Parsing is deliberately lenient about the exact number of
# dashes and about trailing punctuation, and a reply that parses to nothing
# is still preserved (as DESCRIPTION) and flagged, rather than discarded.
# ---------------------------------------------------------------------------
SECTION_ORDER = ["ALT TEXT", "DESCRIPTION", "TRANSCRIPTION", "NOTES"]

_RULE_RE = re.compile(r"^\s*-{2,}\s*([A-Za-z][A-Za-z /&]*?)\s*-{2,}\s*$")
# Also accept a bare heading the model may fall back to, e.g. "ALT TEXT:".
_BARE_RE = re.compile(r"^\s*([A-Z][A-Z /&]{2,30})\s*:\s*$")


def rule_line(name: str) -> str:
    prefix = "--- {} ".format(name)
    return prefix + "-" * max(3, LINE_WIDTH - len(prefix))


def parse_sections(text: str) -> dict:
    """Split a model reply into {SECTION NAME: body}. Returns {} when the
    reply carries no recognizable section rule at all."""
    sections, current, buf = {}, None, []

    def flush():
        if current is not None:
            sections[current] = "\n".join(buf).strip()

    for line in (text or "").splitlines():
        m = _RULE_RE.match(line) or _BARE_RE.match(line)
        if m:
            flush()
            current = " ".join(m.group(1).upper().split())
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    return {k: v for k, v in sections.items() if k}


def build_body(sections: dict) -> str:
    """Canonical section blocks, in suite order, with uniform rule lines."""
    out = []
    for name in SECTION_ORDER:
        # .strip() first: a section the model returned as whitespace only
        # must be dropped, not emitted as an empty block with a heading.
        body = (sections.get(name) or "").strip()
        if body:
            out.append(rule_line(name))
            out.append(body)
            out.append("")
    for name, body in sections.items():          # anything unexpected, last
        body = (body or "").strip()
        if name not in SECTION_ORDER and body:
            out.append(rule_line(name))
            out.append(body)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def build_sidecar(entry, sections, ep, profile_name, generated):
    """Full sidecar text: provenance header + canonical section blocks."""
    bar = "=" * LINE_WIDTH
    head = [
        bar, "TEXT EQUIVALENT", bar,
        "Source image:  {}".format(entry["name"]),
        "Generated:     {} · DC Admin Suite · {} {}".format(
            generated, MANIFEST["name"], MANIFEST["version"]),
        "Endpoint:      {} ({})".format(ep.get("name", "?"),
                                        ep.get("kind", "?")),
        "Model:         {}".format(ep.get("model", "?")),
        "Profile:       {}".format(profile_name),
        "Status:        AI-GENERATED — REVIEW PRIOR TO USE",
    ]
    if entry.get("ctx"):
        head.append("Context ID:    {}".format(entry["ctx"]))
    if entry.get("article"):
        head.append("Article ID:    {}".format(entry["article"]))
    head.append("")
    return "\n".join(head) + build_body(sections) + bar + "\n"


_UNCERTAIN_RE = re.compile(r"^\s*Uncertain\s*:\s*(.*)$", re.I | re.M)


# A reply sometimes echoes the label ("Uncertain: Uncertain: None."). The
# echo is the label, not a reading: strip every leading repeat before the
# value is judged, or "None" is counted as a listed doubt. Duplicated in
# both AI modules (the image describer and the OCR toolkit) - change both.
_UNCERTAIN_ECHO_RE = re.compile(r"^(?:uncertain[ \t]*:[ \t]*)+", re.I)


def uncertain_said(raw: str) -> str:
    """The text after "Uncertain:", whitespace-collapsed, echoed labels
    removed. One function both modules call (see the comment above)."""
    return _UNCERTAIN_ECHO_RE.sub("", " ".join((raw or "").split())).strip()


def extract_alt(sections: dict) -> str:
    return " ".join((sections.get("ALT TEXT") or "").split())


def uncertain_state(sections: dict) -> str:
    """"none" when the reply asserts a clean read, "listed" when it names
    something, "absent" when it never wrote the line. Only an explicit
    "Uncertain: None." is a claim, and only a claim can be doubted."""
    m = _UNCERTAIN_RE.search(sections.get("NOTES") or "")
    if not m:
        return "absent"
    text = uncertain_said(m.group(1))
    return "none" if text.rstrip(".").strip().lower() == "none" else "listed"


def extract_uncertain(sections: dict) -> str:
    m = _UNCERTAIN_RE.search(sections.get("NOTES") or "")
    if not m:
        return ""
    text = uncertain_said(m.group(1))
    return "" if text.rstrip(".").strip().lower() == "none" else text


def analyze_reply(raw: str):
    """(sections, warnings) — normalizes a reply and reports what is off."""
    warnings = []
    sections = parse_sections(raw)
    if not sections:
        # Nothing parseable: keep the text rather than lose it.
        body = (raw or "").strip()
        if not body:
            raise FileError("The endpoint returned an empty reply")
        sections = {"DESCRIPTION": body}
        warnings.append("reply had no section rules — saved as DESCRIPTION")
    elif not sections.get("ALT TEXT"):
        warnings.append("reply had no ALT TEXT section")
    return sections, warnings


def _atomic_write(path, text):
    """Write a sidecar without leaving a half-file behind on failure."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    os.close(fd)
    try:
        Path(tmp).write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Excel output (openpyxl) — suite conventions.
# ---------------------------------------------------------------------------
def contrast_text(hex_color: str) -> str:
    """White or black header text, whichever meets contrast on hex_color."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if lum > 140 else "#ffffff"


def _styles(header_rgb):
    from openpyxl.styles import Alignment, Font, PatternFill
    return {
        "header_fill": PatternFill("solid", fgColor=header_rgb),
        "header_font": Font(name="Calibri", size=11, bold=True,
                            color=contrast_text("#" + header_rgb)
                            .lstrip("#").upper()),
        "alt_fill": PatternFill("solid", fgColor="FDFAF5"),
        "link_font": Font(name="Calibri", size=11, underline="single",
                          color="0563C1"),
        "valign": Alignment(vertical="center"),
    }


def _finish_sheet(ws, headers, n_rows, widths):
    from openpyxl.utils import get_column_letter
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:{}{}".format(
        get_column_letter(len(headers)), max(n_rows + 1, 2))
    for col, width in zip(range(1, len(headers) + 1), widths):
        ws.column_dimensions[get_column_letter(col)].width = width


def _write_sheet(ws, headers, rows, widths, link_cols, st):
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill, cell.font = st["header_fill"], st["header_font"]
        cell.alignment = st["valign"]
    for i, values in enumerate(rows):
        ws.append(list(values))
        row = i + 2
        for col in link_cols:
            cell = ws.cell(row=row, column=col)
            if cell.value:
                cell.hyperlink = cell.value
                cell.font = st["link_font"]
        if i % 2 == 1:
            for col in range(1, len(headers) + 1):
                ws.cell(row=row, column=col).fill = st["alt_fill"]
    _finish_sheet(ws, headers, len(rows), widths)


# New columns are APPENDED, never inserted: REPORT_LINK_COLS is a column
# number, and the Admin Record URL hyperlink must stay at 18.
REPORT_HEADERS = ["Order", "Image File", "Folder", "Context ID",
                  "Article ID", "Record Title", "Pixels", "Size (KB)",
                  "Alt Text", "Alt Chars", "Alt Over Limit", "Sections",
                  "Uncertain Readings", "Sidecar File", "Status",
                  "Retries", "Time", "Admin Record URL",
                  "In Tok", "Out Tok", "Think Tok", "Cached Tok",
                  "Transcription Chars", "Flags"]
REPORT_WIDTHS = [8, 44, 28, 26, 10, 36, 13, 11, 70, 11, 14, 34, 60, 44,
                 30, 9, 9, 60, 10, 10, 11, 12, 20, 30]
REPORT_LINK_COLS = (18,)

# Under-transcription is judged against the run's OWN median, not a fixed
# character count: a folder of blank endpapers is uniformly short and must
# not light up every row, while one truncated page in a dense folder must.
SHORT_TRANSCRIPTION_RATIO = 0.40
# A long transcription that reports no doubt at all, on material like faded
# marginalia or mirrored show-through, is a claim worth checking rather than
# a green light.
#
# INCOMPLETE REPLY is the structural one, and it is the flag a 78-image run
# proved was missing: seven replies stopped mid-sentence before
# writing their NOTES, and neither of the other two flags could see them —
# they were the LONGEST rows in the run, so nothing median-relative applied,
# and they had no "Uncertain: None." to be suspicious of. The client now
# refuses such a reply outright when the provider names a reason (see
# stop_reason), but a provider that says nothing still gets caught here: a
# reply that transcribed text and then never wrote its notes is malformed
# whatever the endpoint claims. Profiles that ask only for alt text produce
# no transcription, so this never fires on them.
CLEAN_READ_MIN_CHARS = 1000


def _median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def apply_quality_flags(rows):
    """Fill each described row's Flags cell. Returns
    (n_short, n_clean_read, n_incomplete, median_transcription_chars)."""
    described = [r for r in rows if r["status"].startswith("Described")]
    median = _median([r["tchars"] for r in described if r["tchars"]])
    n_short = n_clean = n_cut = 0
    for r in described:
        flags = []
        if r["tchars"] and (not r["has_notes"]
                            or r["uncertain_state"] == "absent"):
            flags.append("INCOMPLETE REPLY")
            n_cut += 1
        if (median and r["tchars"]
                and r["tchars"] < median * SHORT_TRANSCRIPTION_RATIO):
            flags.append("SHORT TRANSCRIPTION")
            n_short += 1
        if (r["tchars"] >= CLEAN_READ_MIN_CHARS
                and r["uncertain_state"] == "none"):
            flags.append("CLAIMS CLEAN READ")
            n_clean += 1
        r["flags"] = "; ".join(flags)
    return n_short, n_clean, n_cut, median

REVIEW_NOTE = [
    "Every sidecar in this run is AI-generated and should be reviewed:",
    "· Verify transcriptions against the image — especially names, dates,",
    "  catalogue numbers, and any line listed under Uncertain Readings.",
    "· Confirm the alt text conveys the image's purpose in its context; the",
    "  same image can need different alt text on different pages.",
    "· Alt text over the character limit still works, but is usually a sign",
    "  the description belongs in the extended text instead.",
    "· Out Tok includes Think Tok, the model's reasoning. Both count",
    "  against the run's reply limit, so a page refused for max_tokens",
    "  may show far less visible text than the limit would suggest.",
    "· INCOMPLETE REPLY flags a row whose reply stopped before it wrote its",
    "  notes. The transcription there is cut off mid-sentence — re-run the",
    "  page, or send it to a stronger model.",
    "· SHORT TRANSCRIPTION flags a row whose transcription is under 40% of",
    "  this run's median — usually a page the model stopped reading early.",
    "· CLAIMS CLEAN READ flags a long transcription reported as wholly",
    "  legible. Check it against the image before trusting it.",
    "· Nothing here evaluates colour contrast, reading order, or how the",
    "  image is actually used on a page (WCAG 2.1 AA, 1.4.3 / 1.3.2).",
]


def write_report(path, rows, summary_pairs, header_rgb):
    from openpyxl import Workbook
    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Images"
    _write_sheet(ws, REPORT_HEADERS, rows, REPORT_WIDTHS,
                 REPORT_LINK_COLS, st)
    ws2 = wb.create_sheet("Summary")
    body = list(summary_pairs) + [("", "")] + \
        [(line, "") for line in REVIEW_NOTE]
    _write_sheet(ws2, ["Item", "Value"], body, [70, 50], (), st)
    wb.save(path)


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s. "files"
# carries a version counter; when it changes the page refetches
# GET /api/files to rebuild the selection list (report-consuming pattern).
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",   # idle | starting | scanning | describing | writing | done | error
    "paused": False,
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "last_file": None,      # report workbook
    "out_dir": None,        # sidecar destination when not beside the image
    "summary": "",
    "warnings": 0,
    "tools": {},
    "files": {"loaded": False, "source": "", "count": 0, "version": 0,
              "with_sidecar": 0},
}
LOCK = threading.Lock()
MODEL = None                # scanned image entries
STOP_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()

RUNNING_PHASES = ("starting", "scanning", "describing", "writing")


class StopRequested(Exception):
    """Raised inside the worker when the user clicks Stop."""


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary.
RUN_LOG_NAME = None
CLEAR_RESETS = {"last_file": None, "out_dir": None, "warnings": 0,
                "paused": False}


# ---- DC-LOG 1 -------------------------------------------------------------
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
FORM_FIELDS = ("srcfolder", "srcrun", "srcpaths", "srcdir", "recursive",
    "pathbox", "profile", "endpoint", "dbeside", "dfolder", "skip", "outdir",
    "delay", "retries", "maxdim", "conc", "maxtok",
)
# The page's other controls, which are NOT kept, and why: the queue filter, the log copy box, and the profile dialog, which saves itself.
# Every control on the page is in one list or the other (_verify_pages.py).
FORM_NOT_KEPT = (
    "filter", "logfull", "p_name", "p_desc", "p_alt", "p_prompt",
    "p_default",
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
    # Console echo AFTER the state update, and encoding-safe: Windows
    # consoles (cp1252/cp437) cannot print characters like "·", and a
    # print() crash must never kill a worker.
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


def add_warning():
    with LOCK:
        STATE["warnings"] += 1


def set_model(entries, source):
    global MODEL
    with LOCK:
        MODEL = entries
        STATE["files"] = {
            "loaded": True, "source": source, "count": len(entries),
            "with_sidecar": sum(1 for e in entries if e["sidecar"]),
            "version": STATE["files"]["version"] + 1,
        }


def check_pause_stop():
    """Call between units of work: honours Stop immediately and blocks while
    paused (Stop still works during a pause)."""
    if STOP_EVENT.is_set():
        raise StopRequested()
    while PAUSE_EVENT.is_set():
        if STOP_EVENT.is_set():
            raise StopRequested()
        time.sleep(0.2)


def sleep_interruptible(seconds):
    """Wait without going deaf to Pause/Stop — used for pacing and backoff."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        check_pause_stop()
        time.sleep(min(0.2, max(0.0, end - time.time())))


def eta_text(done, total, t0):
    if not done:
        return "ETA --:--"
    remaining = int((time.time() - t0) / done * (total - done))
    return "ETA {:02d}:{:02d}".format(*divmod(remaining, 60))


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _primary_rgb(session) -> str:
    return session.get("branding", {}).get("colors", {}) \
                  .get("primary", FALLBACK_BRAND["primary"]).lstrip("#")


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return "{}:{:02d}".format(m, s)


# ---------------------------------------------------------------------------
# Per-image work. describe_one() is the whole pipeline for a single image and
# is reused by both the batch worker and the single-image Preview button, so
# what Preview shows is exactly what a run would write.
# ---------------------------------------------------------------------------
def describe_one(entry, ep, profile, max_dim, max_retries, have_pymupdf,
                 sleep_fn, log_fn, timeout=AI_TIMEOUT, max_tokens=None):
    """Returns (sidecar_text, sections, warnings, retries, prep_note,
    usage)."""
    data, mime, note = prepare_image(entry["path"], max_dim, have_pymupdf)
    if note:
        log_fn("    {}".format(note))
    ceiling = max_tokens or DEFAULT_REPLY_LIMIT
    raw, usage, retries = ai_generate_with_retry(
        ep, profile["prompt"], data, mime, max_retries, sleep_fn, log_fn,
        timeout=timeout, max_tokens=ceiling)
    sections, warnings = analyze_reply(raw)
    text = build_sidecar(entry, sections, ep, profile["name"],
                         time.strftime("%Y-%m-%d %H:%M"))
    return text, sections, warnings, retries, note, usage


def _sidecar_dest(entry, cfg):
    """Where this image's sidecar goes."""
    if cfg["dest"] == "beside":
        return sidecar_for(entry["path"])
    rel = os.path.splitext(entry["rel"])[0] + SIDECAR_EXT
    return os.path.join(cfg["out_dir"], "DC_ImageDescriptions_"
                        + cfg["run_stamp"], rel)


def process_one(entry, idx, cfg, ep, profile):
    """Finished report-row dict. Never raises for per-image problems —
    a failure becomes a Failed row plus a warning and the run continues."""
    t_file = time.time()
    w, h = (image_dimensions(entry["path"]) if cfg["have_pymupdf"]
            else (0, 0))
    row = {"order": idx, "file": entry["name"],
           "folder": os.path.dirname(entry["rel"]) or ".",
           "ctx": entry["ctx"], "article": entry["article"],
           "title": entry["title"],
           "pixels": "{}×{}".format(w, h) if w else "",
           "size": round(entry["size"] / 1024) if entry["size"] else "",
           "alt": "", "alt_chars": "", "over": "", "sections": "",
           "uncertain": "", "sidecar": "", "status": "", "retries": 0,
           "admin_url": entry["admin_url"],
           "in_tok": 0, "out_tok": 0, "think_tok": 0, "cache_tok": 0,
           "tchars": 0,
           "uncertain_state": "absent", "has_notes": False, "flags": ""}
    dest = _sidecar_dest(entry, cfg)
    try:
        if entry["sidecar"] and cfg["dest"] == "beside" and cfg["skip"]:
            row["status"] = "Skipped (sidecar exists)"
            row["sidecar"] = dest
            return _finish_row(row, t_file)
        if cfg["dest"] != "beside" and os.path.isfile(dest) and cfg["skip"]:
            row["status"] = "Skipped (sidecar exists)"
            row["sidecar"] = dest
            return _finish_row(row, t_file)
        text, sections, warnings, retries, _note, usage = describe_one(
            entry, ep, profile, cfg["max_dim"], cfg["retries"],
            cfg["have_pymupdf"], sleep_interruptible, log,
            max_tokens=cfg.get("max_tokens"))
        _atomic_write(dest, text)
        alt = extract_alt(sections)
        row["alt"] = alt
        row["alt_chars"] = len(alt)
        row["over"] = ("Yes" if alt and len(alt) > profile["max_alt_chars"]
                       else "")
        row["sections"] = ", ".join(k for k in SECTION_ORDER
                                    if sections.get(k)) or "—"
        row["uncertain"] = extract_uncertain(sections)
        row["uncertain_state"] = uncertain_state(sections)
        row["has_notes"] = bool((sections.get("NOTES") or "").strip())
        row["tchars"] = len(sections.get("TRANSCRIPTION", "") or "")
        row["in_tok"] = usage.get("in", 0)
        row["out_tok"] = usage.get("out", 0)
        row["think_tok"] = usage.get("think", 0)
        row["cache_tok"] = usage.get("cache_read", 0)
        row["sidecar"] = dest
        row["retries"] = retries
        if warnings:
            add_warning()
            for warn in warnings:
                log("    WARNING: {}".format(warn))
            row["status"] = "Described (" + "; ".join(warnings) + ")"
        else:
            row["status"] = "Described"
    except StopRequested:
        raise
    except (FileError, AIError) as e:
        add_warning()
        log("  WARNING: {}: {}".format(entry["name"], e))
        row["status"] = "Failed: {}".format(e)
        # A refused reply is still billed. 1.0.0 left In/Out/Think blank on
        # every failed row, so the report understated what a run cost and
        # hid exactly the rows where the reply limit was spent.
        billed = getattr(e, "usage", None) or {}
        row["in_tok"] = billed.get("in", 0)
        row["out_tok"] = billed.get("out", 0)
        row["think_tok"] = billed.get("think", 0)
        row["cache_tok"] = billed.get("cache_read", 0)
        row["retries"] = getattr(e, "retries", 0)
    except Exception as e:
        add_warning()
        log("  WARNING: {}: unexpected {}: {}".format(
            entry["name"], e.__class__.__name__, e))
        row["status"] = "Failed: unexpected {}: {}".format(
            e.__class__.__name__, e)
    return _finish_row(row, t_file)


def _finish_row(row, t_file):
    row["time"] = _hms(time.time() - t_file)
    return row


def _row_values(row):
    return [row["order"], row["file"], row["folder"], row["ctx"],
            row["article"], row["title"], row["pixels"], row["size"],
            row["alt"], row["alt_chars"], row["over"], row["sections"],
            row["uncertain"], row["sidecar"], row["status"],
            row["retries"], row["time"], row["admin_url"],
            row["in_tok"] or "", row["out_tok"] or "",
            row["think_tok"] or "", row["cache_tok"] or "",
            row["tchars"] or "", row["flags"]]


# ---------------------------------------------------------------------------
# Worker — one background thread per run, optionally fanning out to a small
# pool. Rows are appended under ROWS_LOCK, deliberately NOT under LOCK: LOCK
# guards STATE and set_state() takes it, so a worker holding it while
# reporting progress would deadlock against the poller.
# ---------------------------------------------------------------------------
ROWS_LOCK = threading.Lock()
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 4


class Pacer:
    """A shared minimum interval between request STARTS.

    With one worker this is the old per-image delay. With several it is a
    token bucket: pacing is a property of the endpoint, not of a thread, so
    four workers must not all fire the moment they are free.
    """

    def __init__(self, interval):
        self.interval = max(0.0, float(interval))
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            start = max(time.time(), self.next_at)
            self.next_at = start + self.interval
        delay = start - time.time()
        if delay > 0:
            sleep_interruptible(delay)      # still honours Pause and Stop


def run_pool(entries, cfg, ep, profile, rows, total, t0):
    """The per-image loop across `cfg["concurrency"]` workers.

    A run at concurrency 1 never comes here — it keeps the original serial
    loop — so nothing this adds can change a run that did not ask for it.
    """
    pacer = Pacer(cfg["delay"])
    done = [0]

    def work(idx, entry):
        check_pause_stop()               # Stop lands before the request
        pacer.wait()
        log("({}/{}) {}".format(idx, total, entry["rel"]))
        row = process_one(entry, idx, cfg, ep, profile)
        with ROWS_LOCK:
            rows.append(row)
            done[0] += 1
            n = done[0]
        set_progress(n, total, "{} · {}".format(
            entry["name"], eta_text(n, total, t0)))
        return row

    stopped = False
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg["concurrency"]) as pool:
        futures = [pool.submit(work, i, e)
                   for i, e in enumerate(entries, start=1)]
        for fut in futures:
            try:
                fut.result()
            except StopRequested:
                stopped = True           # the rest will refuse in turn
            except Exception as e:
                # process_one already isolates per-image failure; anything
                # reaching here is the pool itself, and must not take the
                # run down with rows still to write.
                add_warning()
                log("  WARNING: worker failed: {}: {}".format(
                    e.__class__.__name__, e))
    if stopped:
        raise StopRequested()



def describe_worker(session, cfg, ep, profile):
    header_rgb = _primary_rgb(session)
    rows = []
    t0 = time.time()
    entries = cfg["entries"]
    total = len(entries)

    def finish_report(note=""):
        if not rows:
            return None
        suffix = "_PARTIAL" if note else ""
        counts = {}
        for r in rows:
            key = r["status"].split(":")[0].split("(")[0].strip()
            counts[key] = counts.get(key, 0) + 1
        with LOCK:
            n_warn = STATE["warnings"]
        rows.sort(key=lambda r: r["order"])   # pooled runs finish out of order
        described = [r for r in rows if r["status"].startswith("Described")]
        over = sum(1 for r in described if r["over"] == "Yes")
        flagged = sum(1 for r in described if r["uncertain"])
        n_short, n_clean, n_cut, median_chars = apply_quality_flags(rows)
        tok_in = sum(r["in_tok"] for r in rows)
        tok_out = sum(r["out_tok"] for r in rows)
        tok_think = sum(r["think_tok"] for r in rows)
        tok_cache = sum(r["cache_tok"] for r in rows)
        described_n = len(described) or 1
        pairs = [
            ("Run", time.strftime("%Y-%m-%d %H:%M")),
            ("Module", "{} v{}".format(MANIFEST["name"],
                                       MANIFEST["version"])),
            ("Source", cfg["source"]),
            ("Profile", "{} (alt-text limit {} characters)".format(
                profile["name"], profile["max_alt_chars"])),
            ("Endpoint", "{} ({}) · model {}".format(
                ep["name"], ep["kind"], ep["model"])),
            ("Sidecars written", "Beside each image"
             if cfg["dest"] == "beside" else
             "DC_ImageDescriptions_{}/".format(cfg["run_stamp"])),
            ("Existing sidecars", "Skipped" if cfg["skip"]
             else "Overwritten"),
            ("Pacing", "{:.1f}s between requests, up to {} retries{}"
             .format(cfg["delay"], cfg["retries"],
                     "" if cfg["concurrency"] == 1 else
                     ", {} concurrent".format(cfg["concurrency"]))),
            ("Max image dimension", "{} px".format(cfg["max_dim"])),
            ("Reply limit", "{} output tokens".format(cfg["max_tokens"])),
            ("Images in run", "{} of {} queued{}".format(
                len(rows), total, " — " + note if note else "")),
            ("Alt text over the limit", over),
            ("Rows with uncertain readings", flagged),
            ("Total input tokens", "{:,}".format(tok_in)),
            ("Total output tokens", "{:,}".format(tok_out)),
            ("  of which reasoning", "{:,}{}".format(
                tok_think,
                " — counted in the output total above, and against the "
                "reply limit" if tok_think else
                " (this endpoint reports no separate figure)")),
            ("Total cached input tokens", "{:,}".format(tok_cache)),
            # Described rows only: the totals above now include what
            # failed rows were billed (client 1.5), and dividing those by
            # the described count would overstate the mean.
            ("Mean output tokens per described image",
             "{:,}".format(int(sum(r["out_tok"] for r in described)
                               / described_n))),
            ("Output tokens billed for failed images",
             "{:,}".format(sum(r["out_tok"] for r in rows
                               if r["status"].startswith("Failed")))),
            ("Median transcription characters",
             "{:,}".format(int(median_chars))),
            ("Rows with an incomplete reply",
             "{} (stopped before writing NOTES)".format(n_cut)),
            ("Rows with unusually short transcription",
             "{} (under {:.0f}% of this run's median)".format(
                 n_short, SHORT_TRANSCRIPTION_RATIO * 100)),
            ("Rows claiming a clean read",
             "{} (over {:,} characters transcribed, nothing uncertain)"
             .format(n_clean, CLEAN_READ_MIN_CHARS)),
        ]
        pairs += [("Result: " + k, v) for k, v in sorted(counts.items())]
        pairs.append(("Warnings", n_warn))
        try:
            fname = "DC_ImageDescriptions_Report_{}{}.xlsx".format(
                cfg["run_stamp"], suffix)
            path = os.path.join(cfg["out_dir"], fname)
            write_report(path, [_row_values(r) for r in rows], pairs,
                         header_rgb)
            log("Report written: " + path)
            set_state(last_file=path)
            return path
        except Exception as e:
            log("ERROR writing report: {}: {}".format(
                e.__class__.__name__, e))
            return None

    try:
        set_state(phase="describing", summary="", warnings=0,
                  last_file=None, out_dir=None)
        if cfg["dest"] != "beside":
            folder = os.path.join(cfg["out_dir"], "DC_ImageDescriptions_"
                                  + cfg["run_stamp"])
            os.makedirs(folder, exist_ok=True)
            set_state(out_dir=folder)
            log("Sidecar folder: " + folder)
        log("Describing {} image(s) — profile '{}' via {} ({}).".format(
            total, profile["name"], ep["name"], ep["model"]))
        if cfg["concurrency"] > 1:
            log("Running {} requests at a time.".format(cfg["concurrency"]))
            run_pool(entries, cfg, ep, profile, rows, total, t0)
        else:
            for idx, entry in enumerate(entries, start=1):
                check_pause_stop()
                set_progress(idx - 1, total, "{} · {}".format(
                    entry["name"], eta_text(idx - 1, total, t0)))
                log("({}/{}) {}".format(idx, total, entry["rel"]))
                row = process_one(entry, idx, cfg, ep, profile)
                rows.append(row)
                if row["status"].startswith("Described") and idx < total:
                    sleep_interruptible(cfg["delay"])
        set_state(phase="writing")
        set_progress(total, total, "Writing report…")
        path = finish_report()
        with LOCK:
            n_warn = STATE["warnings"]
        n_ok = sum(1 for r in rows if r["status"].startswith("Described"))
        n_skip = sum(1 for r in rows if r["status"].startswith("Skipped"))
        n_fail = sum(1 for r in rows if r["status"].startswith("Failed"))
        summary = "{} described".format(n_ok)
        if n_skip:
            summary += ", {} skipped".format(n_skip)
        if n_fail:
            summary += ", {} failed".format(n_fail)
        if n_warn:
            summary += " · {} warning(s), see log".format(n_warn)
        if path:
            summary += " · report: " + os.path.basename(path)
        set_progress(total, total, "Done")
        # Skipped = a sidecar already there, which is what was asked for.
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(n_fail + n_warn,
                                       succeeded=n_ok + n_skip))
    except StopRequested:
        log("Stop requested — {} of {} image(s) done.".format(
            len(rows), total))
        path = finish_report("stopped early")
        if path:
            set_state(phase="done", paused=False,
                      outcome=outcome_tone(
                          sum(1 for r in rows
                              if r["status"].startswith("Failed"))
                          + run_warnings()),
                      summary="Stopped early ({} of {} image(s)). Partial "
                              "report: {}".format(len(rows), total,
                                                  os.path.basename(path)))
        else:
            set_state(phase="idle", paused=False)
            set_progress(0, 0, "")
            log("Stopped before any image was described.")
    except Exception as e:
        finish_report("unexpected error")
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# Local-request guard (v1.2 shell hardening) — the server binds to 127.0.0.1,
# but a malicious page could still reach it via DNS rebinding (bad Host) or a
# cross-site form/fetch (foreign Origin). Called FIRST in do_GET and do_POST.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# /api/start validation — a pure function, so it is unit-testable.
# Returns (error_message_or_"", cfg_or_None, endpoint_or_None,
#          profile_or_None).
# ---------------------------------------------------------------------------
def validate_start(req, model, tools, ai_cfg, profile_cfg):
    if model is None:
        return "Scan a folder to queue images first.", None, None, None
    try:
        paths = [str(p) for p in req.get("paths", [])]
        cfg = {
            "dest": str(req.get("dest", "beside")),
            "out_dir": os.path.expanduser(str(req.get("out_dir",
                                                      "")).strip()),
            "skip": bool(req.get("skip", True)),
            "delay": float(req.get("delay", DEFAULT_DELAY)),
            "retries": int(req.get("retries", DEFAULT_RETRIES)),
            "max_dim": int(req.get("max_dim", DEFAULT_MAX_DIM)),
            "concurrency": int(req.get("concurrency",
                                       DEFAULT_CONCURRENCY)),
            "endpoint": str(req.get("endpoint", "")).strip(),
            "profile": str(req.get("profile", "")).strip(),
        }
    except (TypeError, ValueError):
        return "Bad request.", None, None, None
    if cfg["dest"] not in ("beside", "folder"):
        return "Bad request.", None, None, None
    err_ = folder_error("Report folder", cfg["out_dir"])
    if err_:
        return err_, None, None, None
    if not 0 <= cfg["delay"] <= 120:
        return ("Delay between requests must be between 0 and 120 seconds.",
                None, None, None)
    if not 0 <= cfg["retries"] <= 10:
        return "Retries must be between 0 and 10.", None, None, None
    if not 512 <= cfg["max_dim"] <= 8000:
        return ("Maximum image dimension must be between 512 and 8000 "
                "pixels.", None, None, None)
    if not 1 <= cfg["concurrency"] <= MAX_CONCURRENCY:
        return ("Concurrent requests must be between 1 and {}."
                .format(MAX_CONCURRENCY), None, None, None)
    ep = find_endpoint(ai_cfg, cfg["endpoint"])
    if ep is None:
        return ("Choose an AI endpoint — add one under Manage endpoints if "
                "the list is empty.", None, None, None)
    if not ep.get("api_key"):
        return ("AI endpoint '{}' has no API key.".format(cfg["endpoint"]),
                None, None, None)
    profile = find_profile(profile_cfg, cfg["profile"])
    if profile is None:
        return ("Description profile '{}' not found.".format(cfg["profile"]),
                None, None, None)
    # The reply limit is the run's, from the job form (1.0.1), range-checked
    # here rather than trusted into a request body.
    ceiling, err = parse_reply_limit(req.get("max_tokens"))
    if err:
        return err, None, None, None
    cfg["max_tokens"] = ceiling
    by_path = {e["path"]: e for e in model}
    entries = [by_path[p] for p in paths if p in by_path]
    if not entries:
        return "Check at least one queued image.", None, None, None
    if len(entries) != len(paths):
        return ("The checked images no longer match the last scan — rescan "
                "and retry.", None, None, None)
    missing = [e["path"] for e in entries if not os.path.isfile(e["path"])]
    if missing:
        return ("{} checked image(s) no longer exist on disk — rescan."
                .format(len(missing)), None, None, None)
    if not tools.get("openpyxl"):
        return ("openpyxl is missing — the run report cannot be written. "
                "See section 5 for the install command.", None, None, None)
    cfg["have_pymupdf"] = bool(tools.get("pymupdf"))
    if not cfg["have_pymupdf"]:
        bad = [e["name"] for e in entries if needs_conversion(e["path"])]
        if bad:
            return ("{} checked image(s) are TIFF/BMP, which no vision "
                    "endpoint accepts, and PyMuPDF is not installed to "
                    "convert them — see section 5.".format(len(bad)),
                    None, None, None)
    cfg["entries"] = entries
    cfg["run_stamp"] = stamp()
    return "", cfg, ep, profile


# ---------------------------------------------------------------------------
# UI — branded page per suite conventions (WCAG 2.1 AA). Raw string + token
# replace (never str.format), so CSS/JS braces and backslashes stay intact.
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
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
 main{max-width:50rem;margin:0 auto;padding:1.25rem}
 fieldset{border:1px solid #d7d4cc;border-radius:6px;margin:0 0 1rem;padding:.75rem 1rem;background:#fff}
 legend{font-weight:600;padding:0 .35rem}
 input[type=text],input[type=number],textarea,select{width:100%;padding:.45rem .6rem;
   border:1px solid #a9a396;border-radius:5px;font:inherit;background:#fff}
 textarea{font:13px/1.5 ui-monospace,monospace;min-height:5.5rem}
 button{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
        padding:.6rem 1.3rem;font:inherit;font-weight:600;cursor:pointer}
 button.secondary{background:#fff;color:var(--primary);border:2px solid var(--primary)}
 button.small{padding:.45rem .8rem;font-weight:600}
 button:disabled{opacity:.55;cursor:not-allowed}
 .row{display:flex;gap:.6rem;align-items:end;flex-wrap:wrap;margin:.4rem 0}
 .row>div{flex:1;min-width:11rem}
 .controls{display:flex;gap:.6rem;flex-wrap:wrap}
 label{display:block;margin-bottom:.3rem}
 .inline{display:flex;gap:.45rem;align-items:baseline;margin:.35rem 0}
 .inline label{margin:0}
 .hint{font-size:.85rem;color:#54524c;margin:.35rem 0 0}
 .sum{margin:.6rem 0 0;padding:.5rem .7rem;border-radius:6px;background:#eef1f5;
      border:1px solid #d7d4cc;font-size:.9rem}
 .sum.loaded{background:#e8f0e6;border-color:#4a6741}
 #list{border:1px solid #d7d4cc;border-radius:6px;max-height:16rem;overflow:auto;
       margin-top:.5rem;background:#fff}
 #list .item{display:flex;gap:.5rem;align-items:baseline;padding:.3rem .6rem;border-bottom:1px solid #efece6}
 #list .item:last-child{border-bottom:0}
 #list .meta{font-size:.82rem;color:#54524c}
 #list .has{font-size:.82rem;color:#4a6741;font-weight:600}
 details{margin:.5rem 0;border:1px solid #e2ded5;border-radius:6px;padding:.4rem .7rem;background:#fbfaf7}
 details summary{cursor:pointer;font-weight:600;font-size:.92rem}
 details p,details ul{font-size:.88rem;margin:.4rem 0}
 details code{background:#eef1f5;padding:0 .25rem;border-radius:3px;font-size:.85em}
 table.reg{width:100%;border-collapse:collapse;margin:.5rem 0;font-size:.9rem}
 table.reg th,table.reg td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #e2ded5}
 table.reg .rowbtn{background:none;border:0;color:#1a5dc8;text-decoration:underline;
   cursor:pointer;font:inherit;padding:0;text-align:left}
 .chips{display:flex;gap:.4rem;flex-wrap:wrap;margin:.3rem 0}
 .chip{font-size:.82rem;padding:.15rem .55rem;border-radius:999px;border:1px solid #4a6741;
   background:#e8f0e6}
 .chip.miss{border-color:#a33a12;background:#f7e8e2}
 :focus-visible{outline:3px solid #1a5dc8;outline-offset:2px}
 #status{margin:1rem 0;padding:.7rem .9rem;border-radius:6px;background:#eef1f5;border:1px solid #d7d4cc}
 #status.done{background:#e8f0e6;border-color:#4a6741}
 #status.error{background:#f7e8e2;border-color:#a33a12}
 #status a{font-weight:600}
 progress{width:100%;height:.8rem;margin:.5rem 0 0}
 #log{background:#1b1b1f;color:#d8d8de;border-radius:6px;padding:.7rem .9rem;
      font:13px/1.5 ui-monospace,monospace;max-height:14rem;overflow:auto;white-space:pre-wrap}
 #prevwrap{margin:.6rem 0 0}
 #prevout{background:#fff;border:1px solid #d7d4cc;border-radius:6px;padding:.7rem .9rem;
   font:13px/1.5 ui-monospace,monospace;white-space:pre-wrap;max-height:22rem;overflow:auto}
 .modalback{position:fixed;inset:0;background:rgba(20,20,25,.45);display:none;
   align-items:flex-start;justify-content:center;padding:2rem 1rem;z-index:9;overflow:auto}
 .modal{background:#fff;border-radius:8px;max-width:44rem;width:100%;padding:1rem 1.2rem}
 .modal h2{margin:.1rem 0 .6rem;font-size:1.05rem}
 .modal .foot{display:flex;gap:.6rem;justify-content:flex-end;margin-top:.9rem}
 #p_prompt{min-height:18rem}
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


<fieldset><legend>1 · Images to describe</legend>
 <div class="inline">
  <input type="radio" id="srcfolder" name="src" value="folder" checked>
  <label for="srcfolder">A folder of images — including a Google Drive for
   Desktop folder</label>
 </div>
 <div class="inline">
  <input type="radio" id="srcrun" name="src" value="run">
  <label for="srcrun">A Batch File Downloader run — record details are read
   from its reports</label>
 </div>
 <div class="inline">
  <input type="radio" id="srcpaths" name="src" value="paths">
  <label for="srcpaths">Individual images (one full path per line)</label>
 </div>
 <div class="row" id="folderrow">
  <div>
   <label for="srcdir" id="srcdirlabel">Folder to scan</label>
   <input type="text" id="srcdir" value="" autocomplete="off" spellcheck="false">
  </div>
  <div style="flex:0;min-width:auto" id="recwrap">
   <div class="inline"><input type="checkbox" id="recursive" checked>
    <label for="recursive">Include subfolders</label></div>
  </div>
  <button type="button" id="scan" class="small">Scan</button>
 </div>
 <div id="pathswrap" hidden>
  <label for="pathbox">Image paths</label>
  <textarea id="pathbox" spellcheck="false"
   placeholder="/Users/you/Desktop/page1.jpg&#10;/Users/you/Desktop/page2.tif"></textarea>
  <div class="row"><button type="button" id="scanpaths" class="small">Add images</button></div>
 </div>
 <div id="filesum" class="sum" role="status" aria-live="polite">No images queued yet.</div>
 <div id="pickwrap" hidden>
  <div class="row">
   <div><label for="filter">Filter queued images</label>
    <input type="text" id="filter" placeholder="type to filter…" autocomplete="off"></div>
   <button type="button" id="selall" class="small secondary">Check all</button>
   <button type="button" id="selnone" class="small secondary">Uncheck all</button>
   <button type="button" id="selnew" class="small secondary">Check only images with no sidecar</button>
  </div>
  <div id="list" role="group" aria-label="Queued images"></div>
  <p class="hint" id="selcount">0 images checked.</p>
 </div>
</fieldset>

<fieldset><legend>2 · Description profile</legend>
 <div class="row">
  <div>
   <label for="profile">Profile — the instruction sent with every image</label>
   <select id="profile"></select>
  </div>
  <button type="button" id="editprofiles" class="small secondary">Manage profiles</button>
 </div>
 <p class="hint" id="profdesc"></p>
 <div id="profwrap" hidden>
  <table class="reg" aria-label="Description profiles"><thead>
   <tr><th scope="col">Profile</th><th scope="col">Alt limit</th>
   <th scope="col">What it produces</th></tr></thead><tbody id="profbody"></tbody></table>
  <div class="row">
   <button type="button" id="addprof" class="small">Add profile</button>
   <button type="button" id="resetprof" class="small secondary">Restore factory profiles</button>
  </div>
  <p class="hint">Profiles are stored in <code>config/image_describer.json</code>
   and travel with the suite. A prompt must ask for an
   <code>--- ALT TEXT ---</code> section; everything else is yours to change.</p>
 </div>
</fieldset>

<fieldset><legend>3 · AI endpoint</legend>
 <div class="row">
  <div>
   <label for="endpoint">Endpoint</label>
   <select id="endpoint"><option value="">— choose an endpoint —</option></select>
  </div>
  <button type="button" id="testep" class="small secondary" disabled>Test</button>
 </div>
 <div id="epstatus" class="hint" role="status" aria-live="polite"></div>
 <div class="row"><button type="button" id="usedim" class="small secondary"
   hidden>Use the recommended image size for this endpoint</button></div>
 <p class="hint">Endpoints are managed for the whole suite in
  <a href="__HUB__#/settings" target="dcAdminSuiteHub">Settings ›
  AI endpoints</a> — add or rotate a key there once and every module sees it,
  including this one, without relaunching. Use
  <strong>Refresh</strong> after adding one.</p>
 <div class="row">
  <button type="button" id="refreshep" class="small secondary">Refresh
   endpoint list</button>
 </div>
</fieldset>

<fieldset><legend>4 · Output and pacing</legend>
 <div class="inline">
  <input type="radio" id="dbeside" name="dest" value="beside" checked>
  <label for="dbeside">Write each sidecar beside its image
   (<code>photo.jpg</code> → <code>photo.txt</code>)</label>
 </div>
 <div class="inline">
  <input type="radio" id="dfolder" name="dest" value="folder">
  <label for="dfolder">Collect sidecars in a separate folder, mirroring the
   source layout — originals untouched</label>
 </div>
 <div class="inline"><input type="checkbox" id="skip" checked>
  <label for="skip">Skip images that already have a sidecar — leave this on so
   a stopped run can simply be started again</label></div>
 <div class="row">
  <div>
   <label for="outdir" id="outdirlabel">Report folder</label>
   <input type="text" id="outdir" value="" autocomplete="off" spellcheck="false">
  </div>
 </div>
 <div class="row">
  <div><label for="delay">Seconds between requests</label>
   <input type="number" id="delay" min="0" max="120" step="0.5" value="2"></div>
  <div><label for="retries">Retries on rate limit</label>
   <input type="number" id="retries" min="0" max="10" value="4"></div>
  <div><label for="maxdim">Max image dimension (px)</label>
   <input type="number" id="maxdim" min="512" max="8000" step="100" value="2000"></div>
  <div><label for="conc">Concurrent requests</label>
   <input type="number" id="conc" min="1" max="4" value="1"></div>
  <div><label for="maxtok">Reply limit (output tokens)</label>
   <input type="number" id="maxtok" min="64" max="32000" step="100"
    value="8000" aria-describedby="maxtokhint"></div>
 </div>
 <p class="hint" id="maxtokhint">The ceiling on one reply, for every image in
  the run. On a thinking model it counts the model's reasoning as well as the
  text it returns, and the reasoning varies from one run to the next even on
  the same image &mdash; so 8000 is the default even for alt text alone. Lower
  it to save on a first run, and raise it if rows fail with
  <code>max_tokens</code>.</p>
 <p class="hint" id="dimhint"></p>
 <p class="hint">Larger images are downscaled before sending — every provider
  downsamples internally anyway, and 2000 px keeps small print legible. TIFF
  and BMP are always converted, because no vision endpoint accepts them.
  Raise the delay if a free-tier key keeps hitting rate limits. Concurrent
  requests run that many images at once against one shared pacing clock —
  faster on a paid key, and the first thing to put back to 1 if the endpoint
  starts refusing.</p>
</fieldset>

<fieldset><legend>5 · Local tools</legend>
 <div class="chips" id="toolchips" role="status" aria-live="polite"></div>
 <div class="row"><button type="button" id="recheck" class="small secondary">Re-check tools</button></div>
 <details><summary>Install the local tools</summary>
  <p>Run with the same Python that launched the suite:</p>
  <p><code>python3 -m pip install openpyxl pymupdf</code></p>
  <p>openpyxl writes the run report; PyMuPDF downscales large images and
   converts TIFF/BMP. Then click Re-check tools — no restart needed.</p></details>
</fieldset>

<div class="controls">
 <button type="button" id="preview" class="secondary" disabled>Preview one image</button>
 <button type="button" id="go" disabled>Start</button>
 <button type="button" id="pause" class="secondary" disabled>Pause</button>
 <button type="button" id="stop" class="secondary" disabled>Stop</button>
</div>

<div id="status" role="status" aria-live="polite">Idle — scan a folder to queue images.</div>
<progress id="prog" max="1" value="0" hidden aria-label="Job progress"></progress>

<div id="prevwrap" hidden>
 <h2 style="font-size:1rem">Preview — nothing has been written to disk</h2>
 <div id="prevout" tabindex="0" aria-label="Preview of the generated sidecar"></div>
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
</main>

<div class="modalback" id="pback"><div class="modal" role="dialog" aria-modal="true"
     aria-labelledby="ptitle">
 <h2 id="ptitle">Edit profile</h2>
 <label for="p_name">Name</label><input type="text" id="p_name" autocomplete="off">
 <label for="p_desc" style="margin-top:.5rem">What it produces (shown under the picker)</label>
 <input type="text" id="p_desc" autocomplete="off">
 <label for="p_alt" style="margin-top:.5rem">Alt-text character limit (reported, not enforced)</label>
 <input type="number" id="p_alt" min="40" max="500" value="125">
 <label for="p_prompt" style="margin-top:.5rem">Prompt sent with every image</label>
 <textarea id="p_prompt" spellcheck="false"></textarea>
 <div class="inline" style="margin-top:.5rem"><input type="checkbox" id="p_default">
  <label for="p_default">Use as the default profile</label></div>
 <p id="p_err" class="dc-bad" role="alert" hidden
    style="font-weight:600;margin:.6rem 0 0"></p>
 <div id="p_delask" class="clearask" hidden>
  <span id="p_delq"></span>
  <button type="button" id="p_delyes" class="small">Delete</button>
  <button type="button" id="p_delno" class="small secondary">Keep</button>
 </div>
 <div class="foot">
  <button type="button" id="p_delete" class="small secondary">Delete</button>
  <button type="button" id="p_dup" class="small secondary">Duplicate</button>
  <button type="button" id="p_cancel" class="small secondary">Cancel</button>
  <button type="button" id="p_save" class="small">Save</button>
 </div>
</div></div>

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
  const txt = (lines || []).join('\n');
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
let paused=false,filesVersion=0,entries=[],checked=new Set(),
    aiCfg={endpoints:[],'default':''},profCfg={profiles:[],'default':''},
    epIndex=-1,profIndex=-1,lastFocus=null,tools={},busy=false;
const RUNNING=['starting','scanning','describing','writing'];

/* ---- dismissible contextual messages -----------------------------------
   showErr() paints the callout above the status line and leaves it there;
   only the X (or the next action that clears it) takes it down. Never put a
   contextual error in #status: the poller rewrites that element every 1.5 s
   and the message would vanish before it could be read. showNote() is the
   same callout in a neutral tone. */
const cerrBox=$('cerr'),cerrMsg=$('cerr-msg'),cerrX=$('cerr-x');
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
/* Inline message spans. Errors get a close button and stay; only
   confirmations fade on the timer. */
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
async function api(path,body){
 const opts=body?{method:'POST',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify(body)}:{};
 const r=await fetch(path,opts);
 let data=null;
 try{data=await r.json()}catch(e){data={error:'HTTP '+r.status}}
 if(!r.ok&&!data.error)data.error='HTTP '+r.status;
 return data;
}

/* ---- source ------------------------------------------------------------ */
function srcMode(){
 return $('srcpaths').checked?'paths':($('srcrun').checked?'run':'folder');
}
function syncSource(){
 const m=srcMode();
 $('pathswrap').hidden=m!=='paths';
 $('folderrow').style.display=m==='paths'?'none':'';
 $('recwrap').style.display=m==='folder'?'':'none';
 $('srcdirlabel').textContent=m==='run'
   ?'Downloader run folder (or the folder that holds it)':'Folder to scan';
}
['srcfolder','srcrun','srcpaths'].forEach(id=>
 $(id).addEventListener('change',syncSource));

function fmtSize(n){
 if(n>=1048576)return (n/1048576).toFixed(1)+' MB';
 if(n>=1024)return Math.round(n/1024)+' KB';
 return n+' B';
}
function renderList(){
 const q=$('filter').value.trim().toLowerCase();
 const list=$('list');
 list.textContent='';
 let shown=0;
 entries.forEach((e,i)=>{
  const hay=(e.rel+' '+(e.title||'')).toLowerCase();
  if(q&&!hay.includes(q))return;
  if(++shown>1500)return;
  const div=document.createElement('div');div.className='item';
  const cb=document.createElement('input');cb.type='checkbox';
  cb.id='f'+i;cb.checked=checked.has(e.path);
  cb.addEventListener('change',()=>{
   cb.checked?checked.add(e.path):checked.delete(e.path);updateCount();});
  const lab=document.createElement('label');lab.htmlFor='f'+i;
  lab.textContent=e.rel;
  const meta=document.createElement('span');meta.className='meta';
  meta.textContent=' '+fmtSize(e.size)+(e.title?' · '+e.title:'');
  div.append(cb,lab,meta);
  if(e.sidecar){
   const has=document.createElement('span');has.className='has';
   has.textContent=' · has sidecar';div.append(has);
  }
  list.append(div);
 });
 if(shown>1500){const d=document.createElement('div');d.className='item meta';
  d.textContent='… list capped at 1500 rows — use the filter.';list.append(d);}
 updateCount();
}
function updateCount(){
 $('selcount').textContent=checked.size+' of '+entries.length+
  ' image(s) checked.';
 syncGo();
}
$('filter').addEventListener('input',renderList);
$('selall').addEventListener('click',()=>{
 entries.forEach(e=>checked.add(e.path));renderList();});
$('selnone').addEventListener('click',()=>{checked.clear();renderList();});
$('selnew').addEventListener('click',()=>{
 checked.clear();
 entries.forEach(e=>{if(!e.sidecar)checked.add(e.path);});
 renderList();});

async function refetchFiles(){
 const data=await api('/api/files');
 entries=data.entries||[];
 checked=new Set(entries.filter(e=>!e.sidecar).map(e=>e.path));
 $('pickwrap').hidden=entries.length===0;
 renderList();
}
async function doScan(){
 clearErr();   /* a fixed problem takes its message down */
 const m=srcMode();
 const body=m==='paths'?{mode:m,paths:$('pathbox').value}
   :{mode:m,folder:$('srcdir').value,recursive:$('recursive').checked};
 $('filesum').textContent='Scanning…';
 const data=await api('/api/scan',body);
 if(data.error){$('filesum').className='sum bad';
  $('filesum').textContent='Scan failed.';showErr(data.error);return;}
 const notes=[];
 if(data.bad&&data.bad.length)
  notes.push(data.bad.length+' path(s) were skipped — not found, or not an '+
   'image format this module handles.');
 if(data.skipped&&data.skipped.length)
  notes.push(data.skipped.length+' non-image file(s) were found among the '+
   'images and skipped ('+data.skipped.slice(0,3).join(', ')+
   (data.skipped.length>3?', …':'')+'). Check the log — a stray .unknown or '+
   '.html file is usually a download that failed, not a file to ignore.');
 if(notes.length)showNote(notes.join(' '));
}
$('scan').addEventListener('click',doScan);
$('scanpaths').addEventListener('click',doScan);

/* ---- profiles ---------------------------------------------------------- */
function renderProfiles(){
 const cur=$('profile').value;
 const sel=$('profile');
 sel.textContent='';
 profCfg.profiles.forEach(p=>{
  const o=document.createElement('option');
  o.value=p.name;o.textContent=p.name;sel.append(o);
 });
 sel.value=profCfg.profiles.some(p=>p.name===cur)?cur:
   (profCfg['default']||(profCfg.profiles[0]||{}).name||'');
 syncProfDesc();
 const body=$('profbody');
 body.textContent='';
 profCfg.profiles.forEach((p,i)=>{
  const tr=document.createElement('tr');
  const td=document.createElement('td');
  const b=document.createElement('button');b.type='button';b.className='rowbtn';
  b.textContent=p.name+(profCfg['default']===p.name?' (default)':'');
  b.addEventListener('click',()=>openProf(i));
  td.append(b);tr.append(td);
  [String(p.max_alt_chars),
   p.description].forEach(v=>{
   const c=document.createElement('td');c.textContent=v;tr.append(c);});
  tr.addEventListener('click',ev=>{if(ev.target===b)return;openProf(i);});
  body.append(tr);
 });
}
function syncProfDesc(){
 const p=profCfg.profiles.find(x=>x.name===$('profile').value);
 $('profdesc').textContent=p?p.description:'';
}
$('profile').addEventListener('change',syncProfDesc);
$('editprofiles').addEventListener('click',()=>{
 $('profwrap').hidden=!$('profwrap').hidden;});

function openProf(i){
 profIndex=i;lastFocus=document.activeElement;
 const p=i>=0?profCfg.profiles[i]
   :{name:'',description:'',prompt:'',max_alt_chars:125};
 $('ptitle').textContent=i>=0?'Edit profile':'Add profile';
 $('p_name').value=p.name;$('p_desc').value=p.description;
 $('p_alt').value=p.max_alt_chars;$('p_prompt').value=p.prompt;
 $('p_default').checked=i>=0&&profCfg['default']===p.name;
 $('p_delete').style.display=i>=0?'':'none';
 $('p_dup').style.display=i>=0?'':'none';
 $('p_delask').hidden=true;
 profErr('');
 $('pback').style.display='flex';
 $('p_name').focus();
}
/* An error from the dialog's own Save or Delete is shown IN the dialog
   (1.0.0): the page callout sits behind an open dialog, so a
   refused Save looked like a Save that did nothing. It stays until the
   next Save, Delete or opened profile; the dialog stays open. */
function profErr(msg){
 const el=$('p_err');el.textContent=msg||'';el.hidden=!msg;
}
async function saveProfiles(cand){
 const data=await api('/api/profiles',cand);
 if(data.error){
  if($('pback').style.display==='flex')profErr(data.error);
  else showErr(data.error);
  return false;}
 profCfg=data.config;renderProfiles();
 showOk('Profiles saved.');
 return true;
}
$('p_save').addEventListener('click',async()=>{
 clearErr();profErr('');
 const p={name:$('p_name').value.trim(),description:$('p_desc').value.trim(),
  prompt:$('p_prompt').value,
  max_alt_chars:parseInt($('p_alt').value||'125',10)};
 const cand={profiles:profCfg.profiles.slice(),'default':profCfg['default']};
 if(profIndex>=0)cand.profiles[profIndex]=p;else cand.profiles.push(p);
 if($('p_default').checked)cand['default']=p.name;
 else if(cand['default']===p.name)cand['default']='';
 if(await saveProfiles(cand))closeModal('pback');
});
/* Delete asks first, inline (1.0.0): a profile can hold an
   afternoon's work on a prompt, and Restore brings back only the factory
   ones. */
$('p_delete').addEventListener('click',()=>{
 if(profIndex<0)return;
 $('p_delq').textContent='Delete the profile \u201c'+profCfg.profiles[profIndex].name+
   '\u201d? This cannot be undone: Restore factory profiles brings back only '+
   'the factory ones.';
 $('p_delask').hidden=false;$('p_delyes').focus();
});
$('p_delno').addEventListener('click',()=>{
 $('p_delask').hidden=true;$('p_delete').focus();});
$('p_delyes').addEventListener('click',async()=>{
 clearErr();profErr('');
 $('p_delask').hidden=true;
 if(profIndex<0)return;
 const cand={profiles:profCfg.profiles.slice(),'default':profCfg['default']};
 const name=cand.profiles[profIndex].name;
 cand.profiles.splice(profIndex,1);
 if(cand['default']===name)cand['default']='';
 if(await saveProfiles(cand))closeModal('pback');
});
$('resetprof').addEventListener('click',async()=>{
 clearErr();
 if(!confirm('Replace the profile list with the factory profiles? Your own '+
             'profiles will be removed.'))return;
 const data=await api('/api/profiles/reset',{});
 if(data.error){showErr(data.error);return;}
 profCfg=data.config;renderProfiles();showOk('Factory profiles restored.');
});
/* Duplicate (2026-10-02, for the public release): copy a profile, then
   edit it. The copy takes what is in the form NOW, so nothing typed is
   lost, and the profile it came from keeps its saved values. It is a new
   profile from this moment: not the default, nothing to delete, and
   nothing saved until Save. The name is the first free "(copy)",
   "(copy 2)" ... — free as the module judges it, ignoring case. */
function copyName(name,names){
 const taken=new Set(names.map(n=>String(n).toLowerCase()));
 let cand=name+' (copy)',n=2;
 while(taken.has(cand.toLowerCase())){cand=name+' (copy '+n+')';n+=1;}
 return cand;
}
$('p_dup').addEventListener('click',()=>{
 if(profIndex<0)return;
 const from=profCfg.profiles[profIndex].name;
 profIndex=-1;
 $('p_name').value=copyName(from,profCfg.profiles.map(p=>p.name));
 $('p_default').checked=false;
 $('ptitle').textContent='Copy of '+from+' (not saved yet)';
 $('p_delete').style.display='none';
 $('p_dup').style.display='none';
 $('p_delask').hidden=true;
 $('p_name').focus();$('p_name').select();
});
$('p_cancel').addEventListener('click',()=>closeModal('pback'));

/* ---- endpoints --------------------------------------------------------- */
const RECDIM=__RECDIM__;
/* Shown, never applied. The recommendation is what the provider actually
   charges input tokens for; whether small print survives the cut is a
   question for the A/B, not for a silent default change. */
function syncDimHint(){
 const ep=aiCfg.endpoints.find(e=>e.name===$('endpoint').value);
 const rec=ep?RECDIM[ep.kind]:0;
 const btn=$('usedim');
 if(!rec||rec===parseInt($('maxdim').value||'0',10)){
  $('dimhint').textContent=rec
   ?'2000 px is this run\'s setting; '+rec+' px is what '+ep.kind+
    ' bills for.':'';
  btn.hidden=true;return;
 }
 $('dimhint').textContent=ep.kind+' re-scales before it counts tokens, so '+
  rec+' px costs less input for the same picture. The shipped default is '+
  'still 2000 px because small print is what this module is for — try '+
  'both on a dense page before switching.';
 btn.hidden=false;
}
function syncEp(){
 const has=$('endpoint').value!=='';
 $('testep').disabled=!has;
 syncDimHint();
 syncGo();
}
$('maxdim').addEventListener('input',syncDimHint);
$('usedim').addEventListener('click',()=>{
 const ep=aiCfg.endpoints.find(e=>e.name===$('endpoint').value);
 if(ep&&RECDIM[ep.kind])$('maxdim').value=RECDIM[ep.kind];
 syncDimHint();
});
$('endpoint').addEventListener('change',syncEp);

function renderEndpoints(){
 const cur=$('endpoint').value;
 const sel=$('endpoint');
 sel.textContent='';
 const none=document.createElement('option');
 none.value='';none.textContent='— choose an endpoint —';
 sel.append(none);
 aiCfg.endpoints.forEach(ep=>{
  const o=document.createElement('option');
  o.value=ep.name;o.textContent=ep.name+' ('+ep.kind+' · '+ep.model+')';
  sel.append(o);
 });
 sel.value=aiCfg.endpoints.some(e=>e.name===cur)?cur:(aiCfg['default']||'');
 if(!aiCfg.endpoints.length)
  setMsg($('epstatus'),'No AI endpoints yet — add one in Settings › '+
   'AI endpoints, then Refresh.',false);
 syncEp();
}
/* Endpoints are owned by the shell now, so this module only re-reads them.
   The button exists because a key added in Settings while this page is open
   should not require a relaunch. */
$('refreshep').addEventListener('click',async()=>{
 clearErr();
 const a=await api('/api/ai');
 if(a.error){showErr(a.error);return;}
 aiCfg=a.config;renderEndpoints();
 setMsg($('epstatus'),aiCfg.endpoints.length
  ?'Endpoint list refreshed.':'Still no endpoints — add one in Settings.',
  false);
});
$('testep').addEventListener('click',async()=>{
 clearErr();
 setMsg($('epstatus'),'Testing '+$('endpoint').value+'…',false);
 const data=await api('/api/ai/test',{name:$('endpoint').value});
 if(data.error)setMsg($('epstatus'),'Test failed: '+data.error,true);
 else setMsg($('epstatus'),'Test OK — reply: '+data.reply,false,true);
});

/* ---- shared modal behaviour -------------------------------------------- */
function closeModal(id){
 $(id).style.display='none';
 if(lastFocus)lastFocus.focus();
}
['pback'].forEach(id=>{
 const back=$(id);
 back.addEventListener('mousedown',ev=>{if(ev.target===back)closeModal(id);});
 back.querySelector('.modal').addEventListener('keydown',ev=>{
  if(ev.key==='Escape'){closeModal(id);return;}
  if(ev.key!=='Tab')return;
  const nodes=back.querySelectorAll('input,select,button,textarea');
  const items=Array.prototype.filter.call(nodes,el=>el.offsetParent!==null);
  if(!items.length)return;
  const first=items[0],last=items[items.length-1];
  if(ev.shiftKey&&document.activeElement===first){ev.preventDefault();last.focus();}
  else if(!ev.shiftKey&&document.activeElement===last){ev.preventDefault();first.focus();}
 });
});
$('addprof').addEventListener('click',()=>openProf(-1));

/* ---- tools ------------------------------------------------------------- */
function renderTools(t){
 tools=t||{};
 const box=$('toolchips');
 box.textContent='';
 Object.keys(tools).forEach(k=>{
  const s=document.createElement('span');
  s.className='chip'+(tools[k]?'':' miss');
  s.textContent=k+(tools[k]?' ✓':' missing');
  box.append(s);
 });
}
$('recheck').addEventListener('click',async()=>{
 const data=await api('/api/tools/check',{});
 if(data.tools)renderTools(data.tools);
});

/* ---- run --------------------------------------------------------------- */
function syncGo(){
 const ready=checked.size>0&&$('endpoint').value!==''&&!busy;
 $('go').disabled=!ready;
 $('preview').disabled=!ready;
}
function runBody(){
 return {paths:Array.from(checked),dest:$('dfolder').checked?'folder':'beside',
  out_dir:$('outdir').value,skip:$('skip').checked,
  delay:parseFloat($('delay').value||'2'),
  retries:parseInt($('retries').value||'4',10),
  max_dim:parseInt($('maxdim').value||'2000',10),
  concurrency:parseInt($('conc').value||'1',10),
  max_tokens:$('maxtok').value.trim(),
  endpoint:$('endpoint').value,profile:$('profile').value};
}
$('go').addEventListener('click',async()=>{
 clearErr();
 const data=await api('/api/start',runBody());
 if(data.error)showErr(data.error);
});
$('preview').addEventListener('click',async()=>{
 clearErr();
 busy=true;syncGo();
 $('preview').textContent='Working…';
 $('prevwrap').hidden=false;
 $('prevout').textContent='Sending the first checked image to '+
  $('endpoint').value+'…';
 const data=await api('/api/preview',runBody());
 busy=false;
 $('preview').textContent='Preview one image';
 syncGo();
 if(data.error){$('prevwrap').hidden=true;showErr(data.error);return;}
 $('prevout').textContent=data.text;
 const u=data.usage||{};
 showNote('Preview of '+data.name+' — nothing was written to disk. '+
  (u.in||0).toLocaleString()+' input and '+(u.out||0).toLocaleString()+
  ' output tokens. Adjust the profile prompt and preview again, or '+
  'Start the full run.');
});
$('pause').addEventListener('click',async()=>{
 await api('/api/pause',{paused:!paused});
});
$('stop').addEventListener('click',async()=>{
 if(confirm('Stop the run? A partial report will be written.'))
  await api('/api/stop',{});
});

function esc(s){const d=document.createElement('span');d.textContent=s;return d.innerHTML;}
async function poll(){
 try{
  const s=await api('/api/state');
  const running=RUNNING.includes(s.phase);
  paused=!!s.paused;
  $('pause').disabled=!running;
  $('stop').disabled=!running;
  $('pause').textContent=paused?'Resume':'Pause';
  $('scan').disabled=running;$('scanpaths').disabled=running;
  /* `busy` is owned by the Preview handler alone. A preview runs
     synchronously and never enters a worker phase, so the poller must not
     clear it — otherwise Start would light up again mid-preview and a
     second run could be launched over the top of it. */
  if(running){$('go').disabled=true;$('preview').disabled=true;}
  else syncGo();
  if(s.files&&s.files.version!==filesVersion){
   filesVersion=s.files.version;
   $('filesum').className='sum loaded';
   $('filesum').textContent=s.files.count+' image(s) queued from '+
    s.files.source+(s.files.with_sidecar
     ?' — '+s.files.with_sidecar+' already have a sidecar':'');
   await refetchFiles();
  }
  if(s.tools&&Object.keys(s.tools).length&&!Object.keys(tools).length)
   renderTools(s.tools);
  const st=$('status');
  st.className=logTone(s);
  let msg='';
  if(s.phase==='idle')msg='Idle — scan a folder to queue images.';
  else if(running){
   msg=(paused?'Paused — ':'')+(s.progress.msg||s.phase)+
    (s.progress.total?' ('+s.progress.current+'/'+s.progress.total+')':'');
  }
  else msg=s.summary||s.phase;
  st.innerHTML=esc(msg)+
   (s.phase==='done'&&s.last_file
    ?' — <a href="/download">Download the report</a>':'')+
   (s.phase==='error'&&s.last_file
    ?' — <a href="/download">Download the partial report</a>':'');
  const prog=$('prog');
  if(s.progress.total&&running){
   prog.hidden=false;prog.max=s.progress.total;prog.value=s.progress.current;
  }else prog.hidden=true;
  renderLog(s.log); formSeen(s); logStateSeen(s);
 }catch(e){/* transient poll errors are fine */}
 setTimeout(poll,1500);
}
(async function init(){
 syncSource();
 const a=await api('/api/ai');
 if(a.config){aiCfg=a.config;renderEndpoints();}
 const p=await api('/api/profiles');
 if(p.config){profCfg=p.config;renderProfiles();}
 /* Shown once, and only when the saved Archival prompt turned out to be
    customised: it is a notice, not an error, and it never offers Restore
    factory profiles as the fix — that would discard the user's other
    profiles too. */
 if(p.notice)showNote(p.notice);
 syncDimHint();
 poll();
})();
</script>
</body></html>"""


def build_page(session: dict) -> bytes:
    brand = session.get("branding", {})
    colors = brand.get("colors", {})
    primary = colors.get("primary", FALLBACK_BRAND["primary"])
    html = (PAGE
            .replace("__FORM_FIELDS__", json.dumps(list(FORM_FIELDS)))
            .replace("__NAME__", MANIFEST["name"])
            .replace("__SUITE__", brand.get("suite_name", "DC Admin Suite"))
            .replace("__INSTITUTION__", brand.get("institution", ""))
            .replace("__PRIMARY__", primary)
            .replace("__ACCENT__", colors.get("accent", FALLBACK_BRAND["accent"]))
            .replace("__ONPRIMARY__", contrast_text(primary))
            .replace("__HUB__", session.get("hub_url", "#"))
            .replace("__BASE_URL__", session.get("base_url", ""))
            .replace("__RECDIM__", json.dumps(RECOMMENDED_MAX_DIM)))
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
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
                return self._json(
                    {"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/log":
                return send_run_log(self)
            if self.path == "/api/state":
                form_for_page()
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/api/files":
                with LOCK:
                    model = MODEL
                return self._json({"entries": model or []})
            if self.path == "/api/ai":
                return self._json({"config": load_ai_config()})
            if self.path == "/api/profiles":
                cfg = load_profiles()
                return self._json({"config": cfg,
                                   "notice": take_prompt_notice()})
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
                return self._send_file(path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        # -- POST routes ---------------------------------------------------
        def _do_scan(self, req):
            mode = str(req.get("mode", "folder"))
            if mode not in ("run", "folder", "paths"):
                return self._json({"error": "Bad request."}, 400)
            bad, skipped = [], []
            try:
                if mode == "paths":
                    entries, bad = scan_paths(str(req.get("paths", "")))
                    source = "individual paths"
                    if not entries:
                        return self._json(
                            {"error": "No usable image files in the pasted "
                                      "paths."}, 400)
                else:
                    folder = os.path.expanduser(
                        str(req.get("folder", "")).strip())
                    err_ = folder_error(
                        "Downloader run folder" if mode == "run"
                        else "Folder to scan", folder)
                    if err_:
                        return self._json({"error": err_}, 400)
                    if mode == "run":
                        entries, run, skipped = scan_run_folder(folder)
                        source = os.path.basename(run)
                    else:
                        entries, skipped = scan_folder(
                            folder, bool(req.get("recursive", False)))
                        source = folder
                    if not entries:
                        return self._json(
                            {"error": "No images found in {} — this module "
                                      "handles {}.".format(
                                          source,
                                          ", ".join(e.lstrip(".")
                                                    for e in IMAGE_EXTS))},
                            400)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": "Scan failed: {}".format(e)}, 400)
            set_model(entries, source)
            n_side = sum(1 for e in entries if e["sidecar"])
            log("Queued {} image(s) from {} ({} already have a sidecar)."
                .format(len(entries), source, n_side))
            if len(entries) >= SCAN_MAX_FILES:
                log("NOTE: scan capped at {} files.".format(SCAN_MAX_FILES))
            # A non-image file sitting among the images is often a FAILED
            # download (a saved HTML error page, or a file with no real
            # extension) — i.e. an image that is missing, not one to ignore.
            # Name them so they can be chased rather than silently dropped.
            for name in skipped[:20]:
                log("NOTE: not an image, skipped — {}".format(name))
            if len(skipped) > 20:
                log("NOTE: {} further non-image file(s) skipped."
                    .format(len(skipped) - 20))
            return self._json({"ok": True, "count": len(entries),
                               "bad": bad, "skipped": skipped})

        def _do_preview(self, req):
            """Describe ONE checked image and return the sidecar text without
            writing anything — so a prompt can be tuned before committing to
            a batch."""
            with LOCK:
                model = MODEL
                tools = dict(STATE["tools"]) or tool_status()
            msg, cfg, ep, profile = validate_start(
                req, model, tools, load_ai_config(), load_profiles())
            if msg:
                return self._json({"error": msg}, 400)
            entry = cfg["entries"][0]
            STOP_EVENT.clear()
            PAUSE_EVENT.clear()
            log("Preview: {} via {} ({}).".format(entry["name"], ep["name"],
                                                  ep["model"]))
            try:
                (text, _sections, warnings, retries, _note,
                 usage) = describe_one(
                    entry, ep, profile, cfg["max_dim"], cfg["retries"],
                    cfg["have_pymupdf"], sleep_interruptible, log,
                    timeout=PREVIEW_TIMEOUT,
                    max_tokens=cfg.get("max_tokens"))
            except (FileError, AIError) as e:
                log("Preview failed: {}".format(e))
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                log("Preview failed: {}: {}".format(e.__class__.__name__, e))
                return self._json({"error": "Unexpected {}: {}".format(
                    e.__class__.__name__, e)}, 400)
            if warnings:
                log("Preview warnings: " + "; ".join(warnings))
            log("Preview complete ({} retr{}) — {:,} in / {:,} out "
                "tokens{}.".format(
                    retries, "y" if retries == 1 else "ies",
                    usage.get("in", 0), usage.get("out", 0),
                    ", {:,} of the output spent reasoning".format(
                        usage["think"]) if usage.get("think") else ""))
            return self._json({"ok": True, "name": entry["name"],
                               "text": text, "warnings": warnings,
                               "retries": retries, "usage": usage})

        def do_POST(self):
            if not request_allowed(self):
                return self._json(
                    {"error": "Forbidden (non-local request)."}, 403)
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
            try:
                req = self._body()
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "Bad request."}, 400)
            if not isinstance(req, dict):
                return self._json({"error": "Bad request."}, 400)

            if self.path == "/api/scan":
                if self._running():
                    return self._json({"error": "A job is running."}, 409)
                return self._do_scan(req)

            if self.path == "/api/profiles":
                cand = backfill_profiles(req)
                msg = validate_profiles(cand)
                if msg:
                    return self._json({"error": msg}, 400)
                try:
                    save_profiles(cand)
                except OSError as e:
                    return self._json({"error": "Could not save profiles: "
                                                "{}".format(e)}, 400)
                return self._json({"ok": True, "config": cand})

            if self.path == "/api/profiles/reset":
                cfg = {"profiles": [dict(p) for p in FACTORY_PROFILES],
                       "default": DEFAULT_PROFILE_CONFIG["default"]}
                try:
                    save_profiles(cfg)
                except OSError as e:
                    return self._json({"error": "Could not save profiles: "
                                                "{}".format(e)}, 400)
                log("Factory description profiles restored.")
                return self._json({"ok": True, "config": cfg})

            if self.path == "/api/ai/test":
                name = str(req.get("name", "")).strip()
                ep = find_endpoint(load_ai_config(), name)
                if ep is None:
                    return self._json({"error": "Endpoint not found."}, 400)
                if not ep.get("api_key"):
                    return self._json({"error": "This endpoint has no API "
                                                "key."}, 400)
                try:
                    reply, _usage = ai_generate(
                        ep, "Reply with the single word: OK",
                        timeout=30, stream=False)
                except AIError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "reply": reply[:80]})

            if self.path == "/api/tools/check":
                tools = tool_status()
                set_state(tools=tools)
                return self._json({"ok": True, "tools": tools})

            if self.path == "/api/preview":
                if self._running():
                    return self._json({"error": "A job is already "
                                                "running."}, 409)
                return self._do_preview(req)

            if self.path == "/api/start":
                if self._running():
                    return self._json({"error": "A job is already "
                                                "running."}, 409)
                with LOCK:
                    model = MODEL
                    tools = dict(STATE["tools"]) or tool_status()
                msg, cfg, ep, profile = validate_start(
                    req, model, tools, load_ai_config(), load_profiles())
                if msg:
                    return self._json({"error": msg}, 400)
                with LOCK:
                    cfg["source"] = STATE["files"]["source"]
                if not start_run(describe_worker, (session, cfg, ep, profile),
                                 events=(STOP_EVENT, PAUSE_EVENT)):
                    return self._json({"error": "A job is already running."}, 409)
                return self._json({"ok": True})

            if self.path == "/api/pause":
                if not self._running():
                    return self._json({"error": "No job is running."}, 409)
                want_pause = bool(req.get("paused",
                                          not PAUSE_EVENT.is_set()))
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
    global PROFILE_PATH, AI_CONFIG_PATH
    parser = argparse.ArgumentParser(description=MANIFEST["name"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--session", default=str(DEFAULT_SESSION))
    args = parser.parse_args()

    config_dir = Path(args.session).resolve().parent
    PROFILE_PATH = config_dir / "image_describer.json"
    session = load_session(Path(args.session))
    AI_CONFIG_PATH = resolve_ai_config_path(session, args.session)
    set_state(tools=tool_status())
    load_profiles()             # seed config/image_describer.json on first run
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
