#!/usr/bin/env python3
"""OCR & Accessibility Toolkit v1.9 — DC Admin Suite module.

v1.9 (2026-09-05) makes the AI path measurable and its failures visible.
The streaming client was ported from dc_image_describer at v1.2-v1.7 and is
current at AI_CLIENT_VERSION 1.4, but the CALLERS were never brought with
it: ai_alt_text and ai_ocr_page threw the usage payload away and never
passed max_tokens, so every request rode the 4,000-token default. On a
thinking model that ceiling counts the reasoning trace as well as the
answer, and the reasoning is not in the output figure a report shows — so
4,000 was in practice a ~2,000-token answer ceiling on the path that sends
up to AI_MAX_OCR_PAGES pages per file.

  · Ceilings per job (AI_OCR_MAX_TOKENS 8000, AI_ALT_MAX_TOKENS 300), and
    usage captured. It travels by return value where the caller is AI-aware
    and by the ai_ctx sink where the caller is a format processor, so
    tag_pdf and the docx/pptx paths stay ignorant of tokens.
  · A refused page no longer discards the file. The sidecar write moved into
    a finally and each page is isolated, so one AIError on page 31 of 50 can
    no longer throw away thirty completed transcriptions — which is what it
    did, and what a Stop did too. The gap is named in the transcript with a
    placeholder and in the report as NO TRANSCRIPTION.
  · Two run-level guards, because isolation is right per page and wrong in
    aggregate: a 401/403 aborts the run, and so does a window of 8 transport
    failures in 12 completions. Content stops feed neither.
  · Four per-page triage flags — NO TRANSCRIPTION, INCOMPLETE REPLY,
    SHORT PAGE, CLAIMS CLEAN READ — plus token columns and an AI note on the
    Summary sheet. None is an error; all are triage.
  · Both prompts rewritten: OCR_PROMPT now requires a coverage declaration
    on an "Uncertain:" line and quarantines inference; ALT_PROMPT asks for
    125 characters, matching the image module, and length is reported rather
    than truncated.
  · AI_OCR_MAX_DIM clamps the page raster at 1536 px on the long edge — the
    Gemini tile boundary, and worth about a third of the input tokens on an
    ordinary letter page.
  · Optional page-level concurrency for AI OCR (1-4, default 1), within one
    file only. Verified by modules/_verify_ocr_toolkit.py.

v1.8 (2026-09-04) — identity refactor Step 3: this module no longer
carries an institution. The standalone session fallback (used only when
config/session.json is missing) is now blank rather than one institution's
URL and name, and the branding color fallbacks read the neutral
FALLBACK_BRAND constant. Real values come from the shell, which seeds them
from config/profile.json — see profiles/README.md.

v1.4 (2026-08-23) reads AI endpoints from the SUITE-LEVEL registry the shell
now owns (config/ai_endpoints.json, path supplied in session.json as
ai_endpoints_path) instead of owning config/ocr_toolkit.json itself. That
file was named for this module but written by two, which was the reason to
promote it. Endpoints are added, edited and rotated once, in Settings ›
AI endpoints; this module reads the registry per request, so a change there
reaches a running module immediately. The picker and Test button stay here.

v1.3 (2026-08-23) exposes the AI retry count as a UI field (section 3),
matching the Image Description Generator, instead of the AI_MAX_RETRIES
constant fixed in v1.2. The count is threaded explicitly through ai_ctx
and the sidecar helpers rather than mutating a module global from a
request handler; AI_MAX_RETRIES remains the default when a caller does
not supply one.

v1.2 (2026-08-23) ports the streaming AI client from dc_image_describer
v1.1. AI requests now stream, so a long answer can no longer be cut off by
an idle-connection timeout part-way through generation, and dropped
connections are retried with backoff instead of losing the file outright
(this module previously had no AI retry logic at all). The AI OCR path,
which sends up to AI_MAX_OCR_PAGES pages per file, was the most exposed
code in the suite to that failure.

(Renamed from "Batch OCR & Accessibility Toolkit" 2026-08-20; the module
id `ocr-toolkit` and the filename are unchanged, so Main Menu ordering,
saved settings, and report formats are unaffected.)

Ingests files that may need new or updated machine-readable text and/or
text equivalents for non-text elements, evaluates them against automatable
WCAG 2.1 AA checks, processes them, and writes a rich filterable
accessibility report with per-file next-step guidance.

SOURCES (three ways to queue files):
  · A Batch File Downloader run folder (DC_FileDownloads_<stamp>/): every
    supported file in the per-structure subfolders is queued, and each
    file's Context ID / Article ID / Record Title / Record State / Admin
    Record URL are read from the subfolder's
    DC_FileDownload_Report_*.xlsx (columns located by header name) so the
    OCR report links processing results back to the repository records.
  · Any folder of files (optionally recursive).
  · Individually listed file paths (one absolute path per line).

FORMATS:
  · PDF (.pdf)          — OCR via ocrmypdf (Tesseract + Ghostscript), doc
    Title / Language metadata via pikepdf, and a heuristic auto-tagging
    pass (see below).
  · Images (.tif/.tiff/.jpg/.jpeg/.png/.bmp) — OCR to a searchable PDF
    (ocrmypdf --image-dpi), a plain-text sidecar (tesseract), or both.
  · Word (.docx)        — python-docx: image alt-text audit (and AI
    drafting), Heading-style / table audit, core Title.
  · PowerPoint (.pptx)  — python-pptx: image alt-text audit (and AI
    drafting), per-slide title audit, core Title.
  · Legacy .doc/.ppt/.rtf and any other extension are inventoried with
    guidance ("convert and rerun") but never modified.

PROCESSING MODES: Evaluate only (no file is touched — an audit run) |
Process files that need work (OCR only where text is missing — ocrmypdf
--skip-text) | Reprocess everything (--force-ocr, matching the old
standalone toolkit's "Force OCR all pages").

AUTO-TAGGING (PDF, heuristic — honest limits): untagged PDFs get a
structure tree built by wrapping each text object (BT..ET) as a <P> and
each image XObject drawn outside text as a <Figure> in marked content,
plus /MarkInfo, /StructParents, a ParentTree, /Lang, docinfo + XMP Title,
and /DisplayDocTitle. Figures get /Alt from the AI endpoint when enabled.
This yields machine-verifiable "tagged PDF" structure but NOT reviewed
semantics — headings, lists, tables, and reading order still need a human
pass (Acrobat or similar), and the report says so per file. Already-tagged
PDFs are never re-tagged. The tagged file is verified re-openable before
it replaces anything; a failed tag attempt leaves the OCR'd-but-untagged
file and is reported.

AI ENDPOINTS (optional): the SUITE-LEVEL registry config/ai_endpoints.json,
owned by the shell and edited in Settings › AI endpoints — this module reads
it per request and never writes it. (Before v1.4 the file lived here as
config/ocr_toolkit.json; a suite still carrying that file is read from it as
a fallback. See resolve_ai_config_path.) It holds named endpoints — kind gemini | openai (OpenAI-compatible, incl. Azure/Copilot
deployments) | anthropic — with URL, model, and API key, called with
urllib only. Uses: drafting image alt text (embedded in docx/pptx,
written into PDF Figure /Alt, and always listed in the report for review)
and an AI OCR engine that writes plain-text sidecars for image-only pages
(AI OCR cannot embed an invisible text layer — the report notes this).
Keys are stored in plain text in config/ — the UI says so.

OUTPUT: overwrite originals (temp file + atomic replace, JS confirm
required) or new copies under DC_OCR_Output_<stamp>/ mirroring the
source's relative layout, with an optional filename suffix. Every run
writes DC_OCR_Report_<stamp>.xlsx ("Files" sheet: one filterable row per
file incl. Accessibility status + Next Steps; "Summary" sheet: run
options, counts, and the checks that can NEVER be automated). Stop or an
unexpected error still writes a _PARTIAL report.

Run standalone:  python3 modules/dc_ocr_toolkit.py --port 8799 \
                     --session config/session.json
Or launch it from the Main Menu.
"""

# ---------------------------------------------------------------------------
# MANIFEST — read by the shell with `ast`; keep it a dict of literals.
# ---------------------------------------------------------------------------
MANIFEST = {
    "id": "ocr-toolkit",
    "name": "OCR & Accessibility Toolkit",
    "description": "OCR, evaluate, and remediate PDFs, images, Word, and PowerPoint files toward WCAG 2.1 AA, with filterable accessibility reports.",
    "version": "1.9.2",
    "requires": ["pymupdf", "pikepdf", "ocrmypdf", "python-docx",
                 "python-pptx", "openpyxl"],
}

import argparse
import base64
import glob
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
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

# Downloader artifacts recognized during run-folder ingest.
DL_RUN_PREFIX = "DC_FileDownloads_"
DL_REPORT_GLOB = "DC_FileDownload_Report_*.xlsx"

# Caps and pacing. Watch-items: bump AI_TIMEOUT if institutional endpoints
# are slow; the per-file AI caps bound cost on image-heavy files.
# Because AI requests stream (see ai_generate), AI_TIMEOUT is an IDLE
# timeout — the longest allowed gap between chunks — not a cap on total
# generation time. A long transcription may legitimately take minutes; what
# must never happen is the connection sitting silent long enough for
# something in the middle to close it.
AI_TIMEOUT = 180           # max seconds between chunks
AI_MAX_RETRIES = 4         # retries on rate limit / dropped connection
BACKOFF_START = 10.0       # first backoff wait, doubling per retry
BACKOFF_CAP = 120.0        # never wait longer than this between retries
AI_MAX_ALT_PER_FILE = 25   # max alt-text drafts per file
AI_MAX_OCR_PAGES = 50      # max pages sent to an AI OCR pass per file
AI_OCR_DPI = 150           # raster resolution for AI OCR page images
OCR_TIMEOUT = 1800         # seconds per ocrmypdf subprocess (30 min/file)
SCAN_MAX_FILES = 5000      # hard cap per source scan (safety)

# A resolution is not a bound. At 150 dpi a letter page is 1275x1650, and a
# broadside or newspaper sheet rasterises to something far larger with no
# ceiling at all — bandwidth paid for and detail the provider downsamples
# away. 1536 is the Gemini tile boundary: it tiles at 768, so 1650 crosses
# into 3x2 = 6 tiles while 1536 lands exactly on 2x2 = 4. The clamp therefore
# engages on EVERY ordinary page, deliberately, and is worth about a third of
# the input tokens. DPI stays the floor for pages smaller than this.
AI_OCR_MAX_DIM = 1536      # max long edge, in pixels, for an AI OCR raster

# Reply ceilings, per job rather than one number for both.
#
# The client accepts max_tokens and every builder threads it into the request
# body, but until v1.9 no caller in this module ever passed one — so every
# request rode DEFAULT_MAX_TOKENS (4000). On a thinking model that ceiling
# counts the reasoning trace as well as the answer, and the reasoning is NOT
# in the output figure the report shows, so 4000 is in practice a ~2000-token
# answer ceiling. That is exactly where dense archival pages sit. 8000 is the
# value the image module settled on at v1.5 after four pages were refused at
# 4000, on the same class of material.
AI_OCR_MAX_TOKENS = 8000   # matches the describer's Archival ceiling
AI_ALT_MAX_TOKENS = 300    # matches the describer's "Alt text only"

# Alt-text length is REPORTED, never enforced — the describer's rule. Cutting
# a draft at the limit would write a mid-word /Alt into a PDF and would make
# the Alt Over Limit column a flag that can never fire. The hard cap exists
# only so a model that ignores the instruction entirely cannot write an essay
# into a structure element; it is set far above anything legitimate.
AI_ALT_MAX_CHARS = 125     # over this, flag the row (never truncate)
AI_ALT_HARD_CAP = 2000     # absolute safety cap; must never fire in practice

# Concurrency for AI OCR pages within one file (never across files —
# ocrmypdf already parallelises internally via its own jobs argument).
DEFAULT_AI_CONCURRENCY = 1
MAX_AI_CONCURRENCY = 4

# Systemic-failure guard. Isolating a refused page is right per page and
# wrong in aggregate: forty-five refusals out of fifty is a dead key, an
# exhausted quota or the wrong model, and isolating each one bills for
# forty-five more failures before anyone notices.
#
# The window is deliberately NOT a consecutive counter, and is strictly
# weaker than one: F F F F S F F F F S F F trips 8-of-12 with a longest run
# of four. That is the intended behaviour. It is expressed over COMPLETIONS,
# which is the order a thread pool delivers in, so one rule governs the
# serial and pooled paths alike rather than two that diverge under load.
#
# Only transport failures feed it. A content stop (max_tokens, recitation)
# never does, because those are deterministic and genuinely per-page — which
# is why the client deliberately does not retry them.
AI_FAIL_WINDOW = 12        # completions to look back over
AI_FAIL_LIMIT = 8          # transport failures within that window = systemic

ALT_PROMPT = ("Write concise alt text for this image for a screen-reader "
              "user in a library repository document. Describe the content "
              "and purpose in one or two sentences, UNDER 125 CHARACTERS IN "
              "TOTAL. Do not begin with 'Image of' or 'Picture of'. Do not "
              "invent detail that is not visible. If the image is purely "
              "decorative, reply exactly: DECORATIVE. Reply with the alt "
              "text only.")

# Three disciplines the image module proved out over v1.3-v1.5, carried
# across but kept machine-parseable: this is a transcription engine, not the
# describer's four-section sidecar.
#
#  1. A coverage declaration. "Uncertain: None." must assert full coverage,
#     not mere confidence — that is the single change that makes a fast model
#     publishable from. Its ABSENCE is the silent-truncation signal, which is
#     why parse_uncertain records "absent" rather than defaulting benignly.
#  2. Inference quarantined. The describer caught Gemini supplying "circa
#     1528 (Bern Disputation)" for a date printed nowhere on the page.
#  3. [NO TEXT] kept, so a blank plate stays cheap and unflagged.
OCR_PROMPT = (
    "Transcribe ALL text visible in this scanned page image, preserving "
    "reading order and paragraph breaks. Reply with the transcription "
    "only, followed by a single final line beginning 'Uncertain:'.\n\n"
    "If any zone of the page contains text you did not transcribe — "
    "because it is reversed, obscured, cropped, faint or too small — "
    "you MUST name that zone on the 'Uncertain:' line. Writing "
    "'Uncertain: None.' asserts that every legible character on the page "
    "appears above.\n\n"
    "Do not supply a date, place, printer or work identity that is not "
    "printed on the page. Transcribe what is there; do not correct, "
    "modernise or complete it.\n\n"
    "If the page contains no text, reply exactly: [NO TEXT].")


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
# Format classification
# ---------------------------------------------------------------------------
PDF_EXTS = (".pdf",)
IMAGE_EXTS = (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp")
DOCX_EXTS = (".docx",)
PPTX_EXTS = (".pptx",)
LEGACY_EXTS = (".doc", ".ppt", ".rtf")

FORMAT_LABELS = {"pdf": "PDF", "image": "Image", "docx": "Word",
                 "pptx": "PowerPoint", "legacy": "Legacy", "other": "Other"}


def classify(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOCX_EXTS:
        return "docx"
    if ext in PPTX_EXTS:
        return "pptx"
    if ext in LEGACY_EXTS:
        return "legacy"
    return "other"


# Per-format guidance for files this module cannot modify.
LEGACY_ADVICE = ("Open in Word/PowerPoint and Save As the modern format "
                 "(.docx/.pptx), then rerun this module on the converted "
                 "file")
OTHER_ADVICE = ("Format not processed by this module — evaluate manually "
                "against WCAG 2.1 AA or convert to a supported format")


# ---------------------------------------------------------------------------
# Local tool detection — pip packages from MANIFEST plus the system tools
# ocrmypdf itself needs (Tesseract, Ghostscript). Shown in the UI with
# install guidance; re-checked on demand.
# ---------------------------------------------------------------------------
def _pkg(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def tool_status() -> dict:
    return {
        "pymupdf": _pkg("fitz"),
        "pikepdf": _pkg("pikepdf"),
        "ocrmypdf": _pkg("ocrmypdf"),
        "python-docx": _pkg("docx"),
        "python-pptx": _pkg("pptx"),
        "openpyxl": _pkg("openpyxl"),
        "tesseract": shutil.which("tesseract") is not None,
        "ghostscript": bool(shutil.which("gs") or shutil.which("gswin64c")
                            or shutil.which("gswin32c")),
    }


def local_ocr_ready(tools: dict) -> bool:
    return tools["ocrmypdf"] and tools["tesseract"] and tools["ghostscript"]


# ---------------------------------------------------------------------------
# AI endpoint registry — config/ai_endpoints.json, owned by the shell since
# v1.4 and READ-ONLY here (the pre-v1.4 config/ocr_toolkit.json is still
# honoured as a fallback; see resolve_ai_config_path). Fields are backfilled
# on load, and backfill_ai_config is a whitelist, so a field missing from it
# is dropped in silence. Keys are stored in PLAIN TEXT; the UI states this.
# ---------------------------------------------------------------------------
CONFIG_PATH = None          # set in main(); guarded for test imports

AI_KINDS = ("gemini", "openai", "anthropic")

DEFAULT_AI_CONFIG = {
    "endpoints": [],        # {name, kind, url, model, api_key}
    "default": "",          # name of the default endpoint ("" = none)
}


def resolve_ai_config_path(session, session_file) -> Path:
    """Where the AI endpoint registry lives, most authoritative first.

    1. `ai_endpoints_path` from session.json — the shell tells us outright.
    2. config/ai_endpoints.json beside the session file — standalone launch
       against a v1.3 suite.
    3. config/ocr_toolkit.json — this module's own pre-v1.3 file, so it still
       works if dropped into an older suite.
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
    if CONFIG_PATH is not None:
        return CONFIG_PATH
    return DEFAULT_SESSION.parent / "ai_endpoints.json"


def backfill_ai_config(cfg: dict) -> dict:
    """Upgrade older config files silently — never require a hand-edit."""
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
# AI client — urllib only. One builder/parser/delta trio per provider shape;
# ai_generate() dispatches on endpoint kind. Every builder returns
# (url, headers, body) so builders are unit-testable without network access.
#
# STREAMING (ported from dc_image_describer v1.1, 2026-08-23). Requests
# stream by default, and that is not an optimisation — it is what makes long
# answers possible at all. A non-streaming request receives no bytes until
# the whole answer has been generated, so the connection sits idle
# throughout; any intermediary with an idle-read timeout (60 s is a very
# common default) closes it mid-generation and the client sees "Remote end
# closed connection without response". The Image Description Generator hit
# exactly this on its first real run: every page whose answer took more than
# ~60 s failed, deterministically, and no amount of re-running recovered one.
# This module is more exposed than that one — its AI OCR path sends up to
# AI_MAX_OCR_PAGES pages per file — so the same client is used here.
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
AI_CLIENT_VERSION = "1.4"

# The reply ceiling a builder falls back to. The image module lets a
# description profile choose its own; this module has one job per prompt and
# uses the default throughout.
DEFAULT_MAX_TOKENS = 4000

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
# early stop is indistinguishable from a finished reply. In the 78-image
# Jesuits run of 2026-09-02 that cost seven pages: every one ended mid-word,
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
    `transient` True for a connection that dropped, reset, or timed out — no
                status code, but worth retrying. Before v1.2 this module had
                no retry logic at all, so every such failure lost its file.
    """

    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.retry_after = None


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


def parse_gemini_response(obj) -> str:
    try:
        parts = obj["candidates"][0]["content"]["parts"]
        return "\n".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected Gemini response shape.")


def gemini_delta(obj):
    """Incremental text from one Gemini SSE event."""
    try:
        parts = obj["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        return ""


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
        # no token counts at all.
        body["stream_options"] = {"include_usage": True}
    return url, headers, body


def parse_openai_response(obj) -> str:
    try:
        return (obj["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected OpenAI-compatible response shape.")


def openai_delta(obj):
    try:
        return obj["choices"][0].get("delta", {}).get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


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
    # Image first is Anthropic's recommendation for a single-image prompt.
    # Caching needs the opposite order, so the two are a real trade, decided
    # per endpoint in Settings rather than assumed here.
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


def parse_anthropic_response(obj) -> str:
    try:
        return "\n".join(b.get("text", "") for b in obj["content"]
                         if b.get("type") == "text").strip()
    except (KeyError, TypeError):
        raise AIError("Unexpected Anthropic response shape.")


def anthropic_delta(obj):
    try:
        if obj.get("type") == "content_block_delta":
            return obj.get("delta", {}).get("text") or ""
    except (AttributeError, TypeError):
        pass
    return ""


_AI_BUILDERS = {
    "gemini": (build_gemini_request, parse_gemini_response, gemini_delta),
    "openai": (build_openai_request, parse_openai_response, openai_delta),
    "anthropic": (build_anthropic_request, parse_anthropic_response,
                  anthropic_delta),
}

# Provider signals that a stream finished cleanly. Without one of these we
# must assume the connection was cut mid-answer and retry, rather than keep
# a transcription or alt text that stops in the middle.
_STREAM_DONE = {"openai": ("[DONE]",), "anthropic": ("message_stop",),
                "gemini": ()}   # Gemini has no terminator; clean EOF is done

# HTTP statuses worth waiting out: rate limit, and the various "busy"
# responses providers return under load.
RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)


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
    normalize_usage.

    stream=False is kept only for the tiny endpoint-test ping; every real
    request streams (see the module note above).
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
            # The endpoint-test ping only; it reports zeros rather than
            # pretending to a measurement.
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
        raise AIError(
            "The endpoint stopped generating early after {} characters "
            "(the provider gave the reason '{}'), so the reply is "
            "incomplete".format(len(text), finish))
    if not done:
        # Cut off mid-answer. Retry rather than keep a truncated result that
        # looks complete — a half transcription is worse than a failed one.
        err = AIError("The endpoint's reply was cut off after {} characters "
                      "(stream ended without a completion signal)"
                      .format(len(text)))
        err.transient = True
        raise err
    if not text.strip():
        raise AIError("The endpoint returned an empty reply")
    return text, normalize_usage(kind, usage)


def is_retryable(err) -> bool:
    """A rate limit, an overloaded provider, or a connection that dropped.
    NOT a bad key or an unknown model — retrying those just wastes time."""
    return (getattr(err, "status", None) in RETRY_STATUSES
            or bool(getattr(err, "transient", False)))


def ai_generate_with_retry(ep, prompt, image_bytes=None,
                           image_mime="image/png",
                           max_retries=AI_MAX_RETRIES, sleep_fn=None,
                           log_fn=None, timeout=AI_TIMEOUT,
                           max_tokens=None):
    """ai_generate plus exponential backoff on rate limits and dropped
    connections. Returns (text, usage, retries_used); raises AIError once
    retries are exhausted or immediately for an error retrying cannot fix.

    sleep_fn defaults to the interruptible sleep, so a backoff still honours
    Pause and Stop instead of freezing the run.
    """
    sleep_fn = sleep_fn or sleep_interruptible
    log_fn = log_fn or log
    wait = BACKOFF_START
    attempt = 0
    while True:
        try:
            text, usage = ai_generate(ep, prompt, image_bytes, image_mime,
                                      timeout=timeout,
                                      max_tokens=max_tokens)
            return text, usage, attempt
        except AIError as e:
            if not is_retryable(e) or attempt >= max_retries:
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
# Usage travels by two different routes, and the split is deliberate.
#
#   By RETURN VALUE where the caller is AI-aware — ai_ocr_page feeds
#   ai_ocr_pdf_sidecar / ai_ocr_image_sidecar, which also need the per-page
#   stop reason for the escalation work to come.
#
#   By the ai_ctx SINK where the caller is a format processor — tag_pdf,
#   process_docx, process_pptx. Those must stay ignorant of tokens; the
#   alternative puts billing accounting inside the tagger, which has nothing
#   to do with the AI. ai_ctx["budget"] already works exactly this way, for
#   exactly this reason, and _verify_image_describer.py pins the boundary
#   deliberately: ai_alt_text returns a bare string and must keep doing so.
# ---------------------------------------------------------------------------
_USAGE_KEYS = ("in", "out", "think", "cache_read")


def new_usage() -> dict:
    """A zeroed accumulator in the client's own four-key shape."""
    return dict((k, 0) for k in _USAGE_KEYS)


def add_usage(sink, usage):
    """Fold one request's usage into a mutable ai_ctx-style sink.

    Tolerates sink=None so a caller with no context (the endpoint-test ping,
    a unit test) needs no special case."""
    if not isinstance(sink, dict):
        return
    total = sink.get("usage")
    if not isinstance(total, dict):
        total = new_usage()
        sink["usage"] = total
    src = usage if isinstance(usage, dict) else {}
    for key in _USAGE_KEYS:
        try:
            total[key] = total.get(key, 0) + int(src.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass


_REFUSAL_RE = re.compile(r"the provider gave the reason '([^']+)'")


def refusal_reason(err):
    """The provider's own word for a NAMED early stop, or None.

    The client raises on a non-normal finish reason rather than returning it,
    and puts the reason in the message. Reading it back here keeps the client
    untouched while still letting a caller tell the two failure classes
    apart: a named content stop is deterministic and belongs to that page,
    whereas anything else is transport and may be systemic."""
    match = _REFUSAL_RE.search(str(err or ""))
    return match.group(1) if match else None


def ai_alt_text(ep, image_bytes, image_mime,
                retries=AI_MAX_RETRIES, sink=None) -> str:
    """Alt text draft, or "" when the model deems the image decorative.

    Returns a BARE STRING, deliberately — see the block comment above. Usage
    goes to `sink` (an ai_ctx dict) when one is supplied."""
    text, usage, _tries = ai_generate_with_retry(
        ep, ALT_PROMPT, image_bytes, image_mime, max_retries=retries,
        max_tokens=AI_ALT_MAX_TOKENS)
    add_usage(sink, usage)
    if text.strip().upper().startswith("DECORATIVE"):
        return ""
    # Length is REPORTED, not enforced: cutting at AI_ALT_MAX_CHARS would
    # write a mid-word /Alt into a PDF and would make the Alt Over Limit
    # column a flag that can never fire. AI_ALT_HARD_CAP is a guard against a
    # model that ignores the instruction outright, not a formatting rule.
    return " ".join(text.split())[:AI_ALT_HARD_CAP]


def ai_ocr_page(ep, image_bytes, image_mime="image/png",
                retries=AI_MAX_RETRIES):
    """One transcribed page.

    Returns (text, usage, retries_used, stop_reason). `stop_reason` is None
    on a normal completion: today the client REFUSES a non-normal stop by
    raising, so a named refusal reaches the caller as an AIError and is read
    back with refusal_reason(). The slot is in the signature because the
    escalation work reads it, and because a caller should not have to know
    which of the two routes a given provider took."""
    text, usage, tries = ai_generate_with_retry(
        ep, OCR_PROMPT, image_bytes, image_mime, max_retries=retries,
        max_tokens=AI_OCR_MAX_TOKENS)
    if text.strip().upper().startswith("[NO TEXT]"):
        return "", usage, tries, None
    return text, usage, tries, None


# The coverage declaration, read back. This is a tri-state and must not be
# collapsed to a boolean: "the model said it read everything" and "the model
# never got that far" are different claims with different remedies, and two
# of the three quality flags read different values of it.
_UNCERTAIN_RE = re.compile(r"^[ \t]*Uncertain[ \t]*:[ \t]*(.*)$", re.I | re.M)


def parse_ocr_reply(reply):
    """Split a page reply into {"text", "uncertain", "state"}.

    Lenient in one direction only. A missing Uncertain: line never fails the
    page and never discards the transcription — but it is recorded as
    "absent", which is what fires INCOMPLETE REPLY. Treating it as a benign
    default would leave that backstop live and permanently silent, which is
    the exact failure the image module spent three releases learning to
    detect."""
    body = reply or ""
    last = None
    for last in _UNCERTAIN_RE.finditer(body):
        pass                      # the line is the reply's last; last wins
    if last is None:
        return {"text": body.strip(), "uncertain": "", "state": "absent"}
    said = " ".join(last.group(1).split())
    text = (body[:last.start()] + body[last.end():]).strip()
    state = "none" if said.rstrip(".").strip().lower() == "none" else "listed"
    return {"text": text, "uncertain": "" if state == "none" else said,
            "state": state}


_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".bmp": "image/bmp", ".tif": "image/tiff",
             ".tiff": "image/tiff", ".jpx": "image/jp2",
             ".jb2": "image/png"}


def guess_image_mime(ext: str) -> str:
    return _IMG_MIME.get((ext or "").lower(), "image/png")


# ---------------------------------------------------------------------------
# Source scanning. Every scan produces a list of entry dicts:
#   {path, rel, name, format, size, ctx, article, title, state, admin_url}
# rel = the path preserved when mirroring into a new-copies output run.
# Suite/module reports (DC_*.xlsx etc.) are never queued.
# ---------------------------------------------------------------------------
_REPORT_NAME_RE = re.compile(r"^DC_[A-Za-z]+_?.*\.(xlsx|txt|xml)$", re.I)


def is_suite_artifact(name: str) -> bool:
    return bool(_REPORT_NAME_RE.match(name))


def _entry(path, rel, meta=None):
    meta = meta or {}
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"path": path, "rel": rel.replace(os.sep, "/"),
            "name": os.path.basename(path), "format": classify(path),
            "size": size,
            "ctx": meta.get("ctx", ""), "article": meta.get("article", ""),
            "title": meta.get("title", ""), "state": meta.get("state", ""),
            "admin_url": meta.get("admin_url", "")}


def scan_folder(folder: str, recursive: bool) -> list:
    """Queue every non-artifact file in a plain folder."""
    entries = []
    if recursive:
        for base, dirs, files in os.walk(folder):
            dirs.sort()
            for fname in sorted(files):
                if fname.startswith(".") or is_suite_artifact(fname):
                    continue
                path = os.path.join(base, fname)
                rel = os.path.relpath(path, folder)
                entries.append(_entry(path, rel))
                if len(entries) >= SCAN_MAX_FILES:
                    return entries
    else:
        for fname in sorted(os.listdir(folder)):
            path = os.path.join(folder, fname)
            if (fname.startswith(".") or is_suite_artifact(fname)
                    or not os.path.isfile(path)):
                continue
            entries.append(_entry(path, fname))
            if len(entries) >= SCAN_MAX_FILES:
                break
    return entries


def scan_paths(text: str):
    """Parse an individual-paths textarea. Returns (entries, bad_lines)."""
    entries, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        path = os.path.expanduser(line)
        if os.path.isfile(path):
            entries.append(_entry(path, os.path.basename(path)))
        else:
            bad.append(line)
        if len(entries) >= SCAN_MAX_FILES:
            break
    return entries, bad


# --- Batch File Downloader run ingest ---------------------------------------
def _header_map(ws):
    """Header text → 1-based column index for row 1 (batch-revise pattern:
    locate columns by name, never by position)."""
    return {str(c.value).strip(): i + 1
            for i, c in enumerate(ws[1]) if c.value is not None}


def load_downloader_meta(report_path: str) -> dict:
    """Saved Filename → record metadata from one per-structure report."""
    from openpyxl import load_workbook
    wb = load_workbook(report_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        cols = _header_map(ws)
        need = ("Saved Filename", "Article ID", "Title")
        if any(h not in cols for h in need):
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
    """Queue a downloader run. `folder` is either a DC_FileDownloads_* run
    folder or a folder containing one or more of them (newest run picked).
    Returns (entries, run_folder) — raises ValueError with a user-facing
    message when no run is found."""
    base = os.path.basename(os.path.normpath(folder))
    if base.startswith(DL_RUN_PREFIX):
        run = folder
    else:
        runs = sorted(glob.glob(os.path.join(folder, DL_RUN_PREFIX + "*")),
                      key=lambda p: os.path.basename(p), reverse=True)
        runs = [r for r in runs if os.path.isdir(r)]
        if not runs:
            raise ValueError(
                "No {}* run folder found in {} — point this at a Batch "
                "File Downloader run (or its parent folder).".format(
                    DL_RUN_PREFIX, folder))
        run = runs[0]
    entries = []
    for sub in sorted(os.listdir(run)):
        sub_path = os.path.join(run, sub)
        if not os.path.isdir(sub_path):
            continue
        meta_by_name = {}
        for rep in glob.glob(os.path.join(sub_path, DL_REPORT_GLOB)):
            try:
                meta_by_name.update(load_downloader_meta(rep))
            except Exception:
                pass  # a damaged report never blocks the file queue
        for fname in sorted(os.listdir(sub_path)):
            path = os.path.join(sub_path, fname)
            if (fname.startswith(".") or is_suite_artifact(fname)
                    or not os.path.isfile(path)):
                continue
            meta = dict(meta_by_name.get(fname, {}))
            meta["ctx"] = sub
            entries.append(_entry(path, os.path.join(sub, fname), meta))
            if len(entries) >= SCAN_MAX_FILES:
                return entries, run
    return entries, run


# ---------------------------------------------------------------------------
# Evaluation — automatable accessibility checks per format. Each evaluator
# returns a plain dict; process/report code reads only these dicts, so the
# evaluators are unit-testable against fixture files.
# ---------------------------------------------------------------------------
def eval_pdf(path: str) -> dict:
    import fitz
    import pikepdf
    info = {"pages": 0, "text_pages": 0, "images": 0, "tagged": False,
            "title": "", "lang": "", "encrypted": False}
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            info["encrypted"] = True
            return info
        info["pages"] = doc.page_count
        seen = set()
        for page in doc:
            if page.get_text("text").strip():
                info["text_pages"] += 1
            for img in page.get_images(full=True):
                seen.add(img[0])
        info["images"] = len(seen)
    finally:
        doc.close()
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        info["tagged"] = "/StructTreeRoot" in root
        try:
            info["lang"] = str(root.Lang) if "/Lang" in root else ""
        except Exception:
            info["lang"] = ""
        try:
            di = pdf.docinfo
            info["title"] = str(di.get("/Title", "")) if di else ""
        except Exception:
            info["title"] = ""
        if not info["title"]:
            try:
                with pdf.open_metadata() as meta:
                    info["title"] = str(meta.get("dc:title", "") or "")
            except Exception:
                pass
    return info


# Alt text equal to a bare image filename (what PowerPoint/export tools
# often auto-fill, and what python-pptx writes) is treated as MISSING —
# it reads "img0041.png" to a screen-reader user.
_FILENAME_ALT_RE = re.compile(
    r"^[\w,\s()-]+\.(png|jpe?g|tiff?|bmp|gif|wmf|emf|svg)$", re.I)


def alt_is_missing(alt) -> bool:
    a = (alt or "").strip()
    return not a or bool(_FILENAME_ALT_RE.match(a))


# python-docx / python-pptx XML namespaces used for alt-text access. The
# libraries expose no public alt-text API, so both audits go through the
# underlying elements — the wp:docPr / p:cNvPr @descr attribute is the alt
# text Word/PowerPoint read aloud.
_NS = {
    "wp": ("http://schemas.openxmlformats.org/drawingml/2006/"
           "wordprocessingDrawing"),
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": ("http://schemas.openxmlformats.org/officeDocument/2006/"
          "relationships"),
}


def iter_docx_images(document):
    """Yield (docPr_element, image_part_or_None) for every inline AND
    floating (anchored) drawing in the document body."""
    body = document.element.body
    for drawing in body.iter("{{{}}}inline".format(_NS["wp"])):
        yield _docx_pair(document, drawing)
    for drawing in body.iter("{{{}}}anchor".format(_NS["wp"])):
        yield _docx_pair(document, drawing)


def _docx_pair(document, drawing):
    doc_pr = drawing.find("{{{}}}docPr".format(_NS["wp"]))
    part = None
    blip = drawing.find(".//{{{}}}blip".format(_NS["a"]))
    if blip is not None:
        rid = blip.get("{{{}}}embed".format(_NS["r"]))
        if rid:
            try:
                part = document.part.related_parts[rid]
            except KeyError:
                part = None
    return doc_pr, part


def eval_docx(path: str) -> dict:
    import docx
    document = docx.Document(path)
    images = alt_missing = 0
    for doc_pr, _part in iter_docx_images(document):
        images += 1
        if doc_pr is None or alt_is_missing(doc_pr.get("descr")):
            alt_missing += 1
    headings = any(p.style is not None
                   and str(p.style.name or "").startswith("Heading")
                   for p in document.paragraphs)
    return {"images": images, "alt_missing": alt_missing,
            "headings": headings, "tables": len(document.tables),
            "title": document.core_properties.title or ""}


def iter_pptx_pictures(prs):
    """Yield (slide_index, cNvPr_element, image_or_None) for every picture,
    descending into group shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def walk(shapes, idx):
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for item in walk(shape.shapes, idx):
                    yield item
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                image = None
                try:
                    image = shape.image
                except Exception:
                    pass
                yield idx, shape._element._nvXxPr.cNvPr, image

    for idx, slide in enumerate(prs.slides):
        for item in walk(slide.shapes, idx):
            yield item


def eval_pptx(path: str) -> dict:
    import pptx
    prs = pptx.Presentation(path)
    images = alt_missing = 0
    for _idx, cnv, _image in iter_pptx_pictures(prs):
        images += 1
        if alt_is_missing(cnv.get("descr")):
            alt_missing += 1
    untitled = 0
    for slide in prs.slides:
        t = slide.shapes.title
        if t is None or not (t.text_frame.text or "").strip():
            untitled += 1
    return {"slides": len(prs.slides), "images": images,
            "alt_missing": alt_missing, "untitled": untitled,
            "title": prs.core_properties.title or ""}


def eval_image(path: str) -> dict:
    import fitz
    try:
        doc = fitz.open(path)
        try:
            page = doc[0]
            return {"width": int(page.rect.width),
                    "height": int(page.rect.height)}
        finally:
            doc.close()
    except Exception:
        return {"width": 0, "height": 0}


# ---------------------------------------------------------------------------
# Local OCR — ocrmypdf run as `python -m ocrmypdf` with THIS interpreter,
# so the pip-installed package is found regardless of PATH. Tesseract and
# Ghostscript must still be on PATH (ocrmypdf calls them itself).
# ---------------------------------------------------------------------------
class FileError(Exception):
    """Per-file failure with user-facing advice; never kills the run."""


def build_ocr_args(mode, langs, jobs, deskew, image_dpi=None):
    """ocrmypdf argument list (input/output appended by the caller).
    mode: "auto" → --skip-text, "force" → --force-ocr (old toolkit
    parity: force rasterizes and re-OCRs every page)."""
    args = [sys.executable, "-m", "ocrmypdf",
            "--output-type", "pdf", "--optimize", "0"]
    if deskew:
        args += ["--deskew", "--rotate-pages"]
    args.append("--force-ocr" if mode == "force" else "--skip-text")
    if langs:
        args += ["-l", langs]
    if jobs and jobs > 1:
        args += ["--jobs", str(jobs)]
    if image_dpi:
        args += ["--image-dpi", str(image_dpi)]
    return args


def run_ocrmypdf(src, dst, mode, langs, jobs, deskew, image_dpi=None):
    """Run ocrmypdf; returns the tail of its output. Raises FileError."""
    args = build_ocr_args(mode, langs, jobs, deskew, image_dpi) + [src, dst]
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=OCR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FileError("ocrmypdf timed out after {} min — file skipped "
                        "(split very large files and retry)".format(
                            OCR_TIMEOUT // 60))
    except OSError as e:
        raise FileError("Could not run ocrmypdf: {}".format(e))
    tail = (proc.stdout or b"").decode("utf-8", "replace").splitlines()[-8:]
    if proc.returncode != 0:
        raise FileError("ocrmypdf exit {}: {}".format(
            proc.returncode, " / ".join(t.strip() for t in tail if t.strip())
            or "no output"))
    return tail


def run_tesseract_text(src) -> str:
    """Plain-text OCR of one image via the tesseract CLI (sidecars)."""
    exe = shutil.which("tesseract")
    if not exe:
        raise FileError("tesseract is not installed / not on PATH")
    try:
        proc = subprocess.run([exe, src, "stdout"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise FileError("tesseract failed: {}".format(e))
    if proc.returncode != 0:
        raise FileError("tesseract exit {}".format(proc.returncode))
    return (proc.stdout or b"").decode("utf-8", "replace").strip()


def prettify_title(entry) -> str:
    """Document title: the record title when the downloader supplied one,
    else the filename stem with separators spaced (never case-mangled)."""
    if entry.get("title"):
        return entry["title"]
    stem = os.path.splitext(entry["name"])[0]
    return re.sub(r"[\s_-]+", " ", stem).strip() or stem


# ---------------------------------------------------------------------------
# PDF metadata + heuristic auto-tagging (pikepdf).
#
# Honest scope: wraps each text object (BT..ET) in a /P marked-content
# sequence and each image XObject drawn OUTSIDE text objects in /Figure,
# then builds StructTreeRoot → Document → [P|Figure], the ParentTree, and
# /StructParents per page. That satisfies machine "tagged PDF" checks and
# gives screen readers per-block text and per-image /Alt — but headings,
# lists, tables, and reading order are NOT inferred; the report always
# flags heuristically tagged files for a human semantics pass. Content
# inside Form XObjects and inline images (BI..EI) is left unwrapped
# (logged). A page whose BT/ET pairing looks unbalanced is left untouched.
# ---------------------------------------------------------------------------
def _image_xobject_names(page):
    import pikepdf
    names = set()
    try:
        xobjs = page.Resources.XObject
    except (AttributeError, KeyError):
        return names
    for name, ref in xobjs.items():
        try:
            if ref.Subtype == pikepdf.Name.Image:
                names.add(str(name))
        except (AttributeError, KeyError):
            pass
    return names


def _mc_open(tag, mcid):
    import pikepdf
    props = pikepdf.Dictionary(MCID=mcid)
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name(tag), props], pikepdf.Operator("BDC"))


def _mc_close():
    import pikepdf
    return pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))


def _rewrite_page_content(pdf, page):
    """Wrap text objects and image draws in marked content. Returns
    (new_instructions_or_None, elems) — elems = [(mcid, kind, xobj_name)];
    None means the page could not be safely rewritten."""
    import pikepdf
    try:
        instrs = pikepdf.parse_content_stream(page)
    except Exception:
        return None, []
    img_names = _image_xobject_names(page)
    new, elems = [], []
    mcid = 0
    in_text = False
    for inst in instrs:
        if isinstance(inst, pikepdf.ContentStreamInlineImage):
            new.append(inst)
            continue
        op = str(inst.operator)
        if op == "BT":
            if in_text:            # nested BT = malformed; bail out
                return None, []
            new.append(_mc_open("/P", mcid))
            new.append(inst)
            elems.append((mcid, "P", ""))
            mcid += 1
            in_text = True
        elif op == "ET":
            if not in_text:
                return None, []
            new.append(inst)
            new.append(_mc_close())
            in_text = False
        elif (op == "Do" and not in_text and len(inst.operands) == 1
                and str(inst.operands[0]) in img_names):
            new.append(_mc_open("/Figure", mcid))
            new.append(inst)
            new.append(_mc_close())
            elems.append((mcid, "Figure", str(inst.operands[0])))
            mcid += 1
        else:
            new.append(inst)
    if in_text:                    # unbalanced at end of stream
        return None, []
    return (new if elems else None), elems


def collect_pdf_image_bytes(path, max_images=AI_MAX_ALT_PER_FILE):
    """(page_index, xobject_name) → (bytes, mime) for AI alt drafting."""
    import fitz
    out = {}
    doc = fitz.open(path)
    try:
        for pno in range(doc.page_count):
            for img in doc[pno].get_images(full=True):
                xref, name = img[0], img[7]
                key = (pno, "/" + str(name).lstrip("/"))
                if key in out or len(out) >= max_images:
                    continue
                try:
                    ext = doc.extract_image(xref)
                    out[key] = (ext["image"],
                                guess_image_mime("." + ext.get("ext", "png")))
                except Exception:
                    pass
    finally:
        doc.close()
    return out


def tag_pdf(path, doc_lang, title, alt_fn=None):
    """Metadata + heuristic tagging, in place (path is rewritten only after
    the result verifies). alt_fn(page_index, xobject_name) → alt text or
    None. Returns a result dict for the report row."""
    import pikepdf
    res = {"tagged": False, "already": False, "p": 0, "figures": 0,
           "alts": 0, "title_set": False, "lang_set": False, "note": ""}
    tmp = path + ".tagging.tmp"
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        # --- metadata (applies even when tagging is skipped) ---
        try:
            existing = str(pdf.docinfo.get("/Title", "")) if pdf.docinfo \
                else ""
        except Exception:
            existing = ""
        if title and not existing.strip():
            pdf.docinfo["/Title"] = title
            try:
                with pdf.open_metadata() as meta:
                    meta["dc:title"] = title
            except Exception:
                pass
            res["title_set"] = True
        if doc_lang and "/Lang" not in root:
            root.Lang = pikepdf.String(doc_lang)
            res["lang_set"] = True
        if "/ViewerPreferences" not in root:
            root.ViewerPreferences = pikepdf.Dictionary()
        root.ViewerPreferences.DisplayDocTitle = True

        if "/StructTreeRoot" in root:
            res["already"] = True
            pdf.save(tmp)
        else:
            doc_elem = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.StructElem, S=pikepdf.Name.Document))
            kids, nums = [], []
            struct_root = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.StructTreeRoot))
            for pno, page in enumerate(pdf.pages):
                new, elems = _rewrite_page_content(pdf, page)
                if new is None:
                    continue
                page.Contents = pdf.make_stream(
                    pikepdf.unparse_content_stream(new))
                page.StructParents = pno
                page_refs = []
                for mcid, kind, xname in elems:
                    d = pikepdf.Dictionary(
                        Type=pikepdf.Name.StructElem,
                        S=pikepdf.Name("/" + kind),
                        Pg=page.obj, K=mcid, P=doc_elem)
                    if kind == "Figure":
                        alt = alt_fn(pno, xname) if alt_fn else None
                        if alt:
                            d.Alt = pikepdf.String(alt)
                            res["alts"] += 1
                        res["figures"] += 1
                    else:
                        res["p"] += 1
                    ref = pdf.make_indirect(d)
                    kids.append(ref)
                    page_refs.append(ref)
                nums.append(pno)
                nums.append(pdf.make_indirect(pikepdf.Array(page_refs)))
            if kids:
                doc_elem.P = struct_root
                doc_elem.K = pikepdf.Array(kids)
                struct_root.K = doc_elem
                struct_root.ParentTree = pdf.make_indirect(
                    pikepdf.Dictionary(Nums=pikepdf.Array(nums)))
                struct_root.ParentTreeNextKey = len(pdf.pages)
                root.StructTreeRoot = struct_root
                root.MarkInfo = pikepdf.Dictionary(Marked=True)
                res["tagged"] = True
            else:
                res["note"] = "no taggable content found"
            pdf.save(tmp)
    # --- verify before replacing the input ---
    try:
        import fitz
        check = fitz.open(tmp)
        n = check.page_count
        _ = check[0].get_text("text") if n else ""
        check.close()
        if n == 0:
            raise ValueError("empty result")
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise FileError("tagged output failed verification ({}) — file "
                        "left untagged".format(e.__class__.__name__))
    os.replace(tmp, path)
    return res


# ---------------------------------------------------------------------------
# docx / pptx processing — alt-text drafting + core title, saved via a
# temp file so overwrite mode is atomic. Both return report-facing dicts.
# ---------------------------------------------------------------------------
def process_docx(src, dst, title, ai_ctx, log_fn):
    import docx
    document = docx.Document(src)
    drafted = missing = images = 0
    for doc_pr, part in iter_docx_images(document):
        images += 1
        if doc_pr is None:
            missing += 1
            continue
        if not alt_is_missing(doc_pr.get("descr")):
            continue
        alt = _draft_alt(ai_ctx, part, log_fn)
        if alt is None:
            missing += 1
        else:
            doc_pr.set("descr", alt if alt else "")
            # An empty draft = model judged it decorative; Word treats
            # descr="" as present, matching decorative marking.
            drafted += 1
    title_set = False
    if title and not (document.core_properties.title or "").strip():
        document.core_properties.title = title
        title_set = True
    _atomic_save(document.save, dst)
    return {"images": images, "alt_missing": missing,
            "alt_drafted": drafted, "title_set": title_set}


def process_pptx(src, dst, title, ai_ctx, log_fn):
    import pptx
    prs = pptx.Presentation(src)
    drafted = missing = images = 0
    for _idx, cnv, image in iter_pptx_pictures(prs):
        images += 1
        if not alt_is_missing(cnv.get("descr")):
            continue
        part = image if image is not None else None
        alt = _draft_alt(ai_ctx, part, log_fn)
        if alt is None:
            missing += 1
        else:
            cnv.set("descr", alt if alt else "")
            drafted += 1
    untitled = 0
    for slide in prs.slides:
        t = slide.shapes.title
        if t is None or not (t.text_frame.text or "").strip():
            untitled += 1
    title_set = False
    if title and not (prs.core_properties.title or "").strip():
        prs.core_properties.title = title
        title_set = True
    _atomic_save(prs.save, dst)
    return {"images": images, "alt_missing": missing,
            "alt_drafted": drafted, "untitled": untitled,
            "title_set": title_set}


def _record_alt_length(ai_ctx, text):
    """Alt length is reported, never enforced — so it has to be recorded.

    A decorative reply is an empty string and is not a draft, so it does not
    belong in the length statistics."""
    if isinstance(ai_ctx, dict) and text:
        ai_ctx.setdefault("alt_chars", []).append(len(text))


def _draft_alt(ai_ctx, image_part, log_fn):
    """One alt draft. Returns text ("" = decorative) or None (not drafted).
    ai_ctx = {"ep": endpoint_or_None, "budget": [n], "retries": n,
              "usage": {...}, "alt_failed": n} — budget is mutable so the
    per-file cap spans docx/pptx/pdf paths alike, and usage rides the same
    channel for the same reason: the format processors calling this must
    stay ignorant of tokens."""
    if not ai_ctx or ai_ctx.get("ep") is None or image_part is None:
        return None
    if ai_ctx["budget"][0] <= 0:
        return None
    try:
        blob = image_part.blob
        mime = getattr(image_part, "content_type", "") or "image/png"
    except Exception:
        return None
    try:
        ai_ctx["budget"][0] -= 1
        drafted = ai_alt_text(ai_ctx["ep"], blob, mime,
                              retries=ai_ctx.get("retries",
                                                 AI_MAX_RETRIES),
                              sink=ai_ctx)
        _record_alt_length(ai_ctx, drafted)
        return drafted
    except AIError as e:
        # Counted, not just logged: without this Alt Drafted undercounts and
        # the report says nothing about why.
        ai_ctx["alt_failed"] = ai_ctx.get("alt_failed", 0) + 1
        log_fn("    WARNING: alt-text request failed: {}".format(e))
        return None


def _atomic_save(save_fn, dst):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".",
                               suffix=".tmp")
    os.close(fd)
    try:
        save_fn(tmp)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# AI OCR — plain-text sidecar from page rasters. AI engines cannot embed
# an invisible text layer, so this NEVER replaces the PDF; it writes
# <output stem>_ai_text.txt beside the output and the report says so.
# ---------------------------------------------------------------------------
AI_MAX_SEND_BYTES = 3500000     # re-encode a raster larger than this


class SystemicAIFailure(Exception):
    """The endpoint is failing as a whole, not this page.

    Raised past per-page isolation and past per-file isolation, because the
    remedy is neither: a dead key or an exhausted quota wants the run
    stopped, not fifty more requests sent to prove the point."""


class FailWindow:
    """Sliding window over recent AI COMPLETIONS, shared across the run.

    Deliberately not a consecutive counter, and strictly weaker than one:
    F F F F S F F F F S F F trips 8-of-12 with a longest run of four. That
    is intended. Counting over completions rather than over adjacency is
    also what lets one rule govern the serial and pooled paths alike — a
    pool delivers in completion order, so a consecutive counter would have
    had to be redefined the moment concurrency landed."""

    def __init__(self, size=AI_FAIL_WINDOW, limit=AI_FAIL_LIMIT):
        self.size = max(1, int(size))
        self.limit = max(1, int(limit))
        self.recent = []
        self.lock = threading.Lock()

    def record(self, failed) -> bool:
        """Log one completion; True when the window says this is systemic."""
        with self.lock:
            self.recent.append(bool(failed))
            if len(self.recent) > self.size:
                del self.recent[:-self.size]
            return sum(1 for f in self.recent if f) >= self.limit


def _clamp_scale(width, height):
    """Zoom factor for one page: DPI is the floor, AI_OCR_MAX_DIM the cap."""
    longest = max(float(width or 0), float(height or 0)) or 1.0
    scale = AI_OCR_DPI / 72.0
    if longest * scale > AI_OCR_MAX_DIM:
        scale = AI_OCR_MAX_DIM / longest
    return scale


def _raster_bytes(page):
    """One page as bytes ready to send, clamped to AI_OCR_MAX_DIM.

    PNG by preference: this is a transcription path, and lossy artefacts on
    small print cost more than the bandwidth saves. JPEG is a fallback for a
    raster that would otherwise be too large to send at all. The clamp is
    what saves tokens — providers price by tile, not by byte."""
    import fitz
    rect = page.rect
    scale = _clamp_scale(rect.width, rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    try:
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.colorspace is not None and pix.colorspace.n == 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
    except Exception:
        pass
    data = pix.tobytes("png")
    if len(data) > AI_MAX_SEND_BYTES:
        try:
            return pix.tobytes("jpeg", jpg_quality=88), "image/jpeg"
        except (TypeError, ValueError, RuntimeError):
            pass
    return data, "image/png"


def _image_send_bytes(img_path):
    """A standalone image, clamped the same way when it is oversized.

    A file already inside the ceiling is sent untouched — re-encoding a
    modest scan would only lose detail for nothing."""
    raw = Path(img_path).read_bytes()
    mime = guess_image_mime(os.path.splitext(img_path)[1])
    try:
        import fitz
        doc = fitz.open(img_path)
        try:
            page = doc[0]
            rect = page.rect
            if (max(rect.width, rect.height) * (AI_OCR_DPI / 72.0)
                    <= AI_OCR_MAX_DIM and len(raw) <= AI_MAX_SEND_BYTES):
                return raw, mime
            return _raster_bytes(page)
        finally:
            doc.close()
    except Exception:
        # No pymupdf, or a format it will not open: send what we have rather
        # than failing a page over an optimisation.
        return raw, mime


def _page_placeholder(reason):
    """The hole, named, in the transcript itself.

    A reader of the sidecar must see where the gap is and why. The report
    says the same thing in the same word (NO TRANSCRIPTION), so transcript
    and report cannot be read as describing different events."""
    return ("[NO TRANSCRIPTION — the model stopped for {!r}. This page "
            "was not transcribed; its content is missing from this file.]"
            .format(str(reason)))


def _ocr_one_page(ep, doc, doc_lock, pno, retries, log_fn, fails):
    """Transcribe one page. Returns (chunk_text, record).

    Isolates a refused page: the file keeps every other page. Raises only
    SystemicAIFailure (the endpoint is gone) and StopRequested."""
    label = "--- Page {} ---".format(pno + 1)
    try:
        with doc_lock:
            data, mime = _raster_bytes(doc[pno])
        text, usage, _tries, _stop = ai_ocr_page(ep, data, mime,
                                                 retries=retries)
    except AIError as err:
        reason = refusal_reason(err)
        status = getattr(err, "status", None)
        # A named content stop belongs to this page and is deterministic.
        # Anything else is transport, and transport failures are what the
        # systemic guards watch.
        if reason is None:
            if status in (401, 403):
                raise SystemicAIFailure(
                    "the endpoint rejected our credentials (HTTP {}) — "
                    "stopping the run rather than sending more requests: {}"
                    .format(status, err))
            if fails is not None and fails.record(True):
                raise SystemicAIFailure(
                    "{} of the last {} AI requests failed — this looks "
                    "like the endpoint rather than the pages, so the run was "
                    "stopped. Last error: {}".format(
                        fails.limit, fails.size, err))
            reason = "request failed"
        elif fails is not None:
            fails.record(False)
        log_fn("    WARNING: page {} not transcribed: {}".format(
            pno + 1, err))
        return ("{}\n{}".format(label, _page_placeholder(reason)),
                {"page": pno + 1, "chars": 0, "state": "absent",
                 "refused": reason, "usage": None})
    if fails is not None:
        fails.record(False)
    parsed = parse_ocr_reply(text)
    return ("{}\n{}".format(label, text),
            {"page": pno + 1, "chars": len(parsed["text"]),
             "state": parsed["state"] if text.strip() else "absent",
             "refused": None, "usage": usage})


def ai_ocr_pdf_sidecar(ep, pdf_path, sidecar_path, log_fn,
                       check_fn, retries=AI_MAX_RETRIES, sink=None,
                       concurrency=1):
    """Transcribe up to AI_MAX_OCR_PAGES pages into a text sidecar.

    The write happens in the `finally`, so a partial transcript survives a
    refused page, a systemic abort and a Stop alike — the same convention as
    the module's _PARTIAL report. Before v1.9 it sat after the loop, so one
    AIError on page 31 of 50 discarded thirty completed pages that had
    already been paid for."""
    import fitz
    doc = fitz.open(pdf_path)
    doc_lock = threading.Lock()
    chunks = {}
    records = []
    rec_lock = threading.Lock()
    tail = []
    try:
        pages = min(doc.page_count, AI_MAX_OCR_PAGES)
        if doc.page_count > pages:
            tail.append("--- {} further page(s) not sent (per-file AI "
                        "cap) ---".format(doc.page_count - pages))
            log_fn("    AI OCR capped at {} pages.".format(pages))

        def work(pno):
            check_fn()          # Stop lands before the request, not after
            chunk, record = _ocr_one_page(
                ep, doc, doc_lock, pno, retries, log_fn,
                sink.get("fails") if isinstance(sink, dict) else None)
            with rec_lock:
                chunks[pno] = chunk
                records.append(record)

        if concurrency > 1 and pages > 1:
            _run_page_pool(work, pages, concurrency, log_fn)
        else:
            for pno in range(pages):
                work(pno)
    finally:
        doc.close()
        # Sorted by page number, never by completion order: a pooled run
        # finishes out of order, and a scrambled transcript is worse than a
        # short one.
        ordered = [chunks[k] for k in sorted(chunks)] + tail
        Path(sidecar_path).write_text("\n\n".join(ordered),
                                      encoding="utf-8")
        _absorb_page_records(sink, records)
    return sidecar_path


def _run_page_pool(work, pages, concurrency, log_fn):
    """The page loop across a small pool. Concurrency 1 never comes here.

    Every future is drained before anything is re-raised, so a Stop still
    collects the pages already in flight instead of throwing them away."""
    import concurrent.futures
    workers = max(1, min(int(concurrency), MAX_AI_CONCURRENCY))
    log_fn("    Running {} AI requests at a time.".format(workers))
    stopped = False
    systemic = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers) as pool:
        futures = [pool.submit(work, pno) for pno in range(pages)]
        for fut in futures:
            try:
                fut.result()
            except StopRequested:
                stopped = True          # the rest refuse in turn
            except SystemicAIFailure as err:
                systemic.append(err)
            except Exception as err:
                # _ocr_one_page already isolates a page. Anything arriving
                # here is the pool itself, and must not take the file down
                # with pages still to write.
                log_fn("    WARNING: AI page worker failed: {}: {}".format(
                    err.__class__.__name__, err))
    if systemic:
        raise systemic[0]
    if stopped:
        raise StopRequested()


def _absorb_page_records(sink, records):
    """Fold per-page detail and usage into the file's ai_ctx sink."""
    if not isinstance(sink, dict) or not records:
        return
    pages = sink.setdefault("pages", [])
    for rec in sorted(records, key=lambda r: r["page"]):
        add_usage(sink, rec.pop("usage", None))
        pages.append(rec)


def ai_ocr_image_sidecar(ep, img_path, sidecar_path,
                         retries=AI_MAX_RETRIES, sink=None, log_fn=None):
    """One standalone image, transcribed to a text sidecar.

    Given the same treatment as a PDF page deliberately: before v1.9 a
    refusal here raised straight out and the image got no sidecar and no
    explanation, so a one-page image and page 31 of a PDF failed in two
    different ways and read differently in the report."""
    log = log_fn or (lambda m: None)
    data, mime = _image_send_bytes(img_path)
    fails = sink.get("fails") if isinstance(sink, dict) else None
    record = {"page": 1, "chars": 0, "state": "absent", "refused": None}
    try:
        text, usage, _tries, _stop = ai_ocr_page(ep, data, mime,
                                                 retries=retries)
    except AIError as err:
        reason = refusal_reason(err)
        status = getattr(err, "status", None)
        if reason is None:
            if status in (401, 403):
                raise SystemicAIFailure(
                    "the endpoint rejected our credentials (HTTP {}) — "
                    "stopping the run rather than sending more requests: {}"
                    .format(status, err))
            if fails is not None and fails.record(True):
                raise SystemicAIFailure(
                    "{} of the last {} AI requests failed — this looks "
                    "like the endpoint rather than the pages, so the run was "
                    "stopped. Last error: {}".format(
                        fails.limit, fails.size, err))
            reason = "request failed"
        elif fails is not None:
            fails.record(False)
        log("    WARNING: image not transcribed: {}".format(err))
        record["refused"] = reason
        Path(sidecar_path).write_text(_page_placeholder(reason),
                                      encoding="utf-8")
        _absorb_page_records(sink, [dict(record, usage=None)])
        return sidecar_path
    if fails is not None:
        fails.record(False)
    parsed = parse_ocr_reply(text)
    record["chars"] = len(parsed["text"])
    record["state"] = parsed["state"] if text.strip() else "absent"
    Path(sidecar_path).write_text(text, encoding="utf-8")
    _absorb_page_records(sink, [dict(record, usage=usage)])
    return sidecar_path


# ---------------------------------------------------------------------------
# Accessibility classification + next-step guidance. `manual` items need
# authoring work; `review` items only need verification. The universal
# never-automatable checks (contrast, reading order, link text, table
# header semantics) live on the report's Summary sheet, so "Meets
# automated checks" stays attainable per file.
# ---------------------------------------------------------------------------
STATUS_MEETS = "Meets automated checks"
STATUS_REVIEW = "Needs review"
STATUS_MANUAL = "Needs manual work"
STATUS_NA = "Not processed"
STATUS_FAILED = "Failed"


def classify_accessibility(manual_items, review_items):
    if manual_items:
        return STATUS_MANUAL
    if review_items:
        return STATUS_REVIEW
    return STATUS_MEETS


# ---------------------------------------------------------------------------
# Per-page triage. None of these is an error; all four are the difference
# between a report that says "Processed" over a silent hole and one that
# names the hole. They are per PAGE rather than per file because the
# escalation work re-runs pages, not documents.
#
# NO TRANSCRIPTION and INCOMPLETE REPLY are kept apart on purpose. A named
# stop is deterministic — re-running it returns the same refusal, so it wants
# a stronger model or a higher ceiling. A missing Uncertain: line means the
# provider claimed success and quietly stopped, which a plain re-run often
# fixes. Merged into one flag, an escalation pass cannot tell "don't bother
# re-running" from "re-run first".
#
# INCOMPLETE REPLY carries the image module's meaning deliberately, because
# both reports are read side by side and a name that means two things in two
# modules is the report-vocabulary equivalent of client drift. There it fires
# on `tchars and (not has_notes or uncertain_state == "absent")`; this module
# has no NOTES section to be missing — its reply is a transcription plus one
# final line — so the clause reduces to the second half and selects the same
# pages by the only route this reply format offers.
# ---------------------------------------------------------------------------
SHORT_PAGE_RATIO = 0.40        # under this share of the file's median
CLEAN_READ_MIN_CHARS = 1000    # long enough that "nothing uncertain" is a claim


def _median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def page_flags(pages):
    """(flag_text, counts) for one file's AI pages.

    The short-page threshold is relative to the median of THIS FILE, not of
    the run: a scanned volume of mostly-blank plates must not light up every
    page, while one truncated page in a dense volume must."""
    scored = [p for p in pages if not p.get("refused") and p.get("chars")]
    median = _median([p["chars"] for p in scored])
    groups = {"NO TRANSCRIPTION": [], "INCOMPLETE REPLY": [],
              "SHORT PAGE": [], "CLAIMS CLEAN READ": []}
    for page in sorted(pages, key=lambda p: p.get("page") or 0):
        num = page.get("page") or 0
        if page.get("refused"):
            groups["NO TRANSCRIPTION"].append(
                "p{} ({})".format(num, page["refused"]))
            continue
        chars = page.get("chars") or 0
        if not chars:
            # [NO TEXT], a blank plate: nothing was claimed, so nothing is
            # doubted. Fires none of the three, exactly as an alt-text-only
            # profile fires none in the image module.
            continue
        if page.get("state") == "absent":
            groups["INCOMPLETE REPLY"].append("p{}".format(num))
        if median and chars < median * SHORT_PAGE_RATIO:
            groups["SHORT PAGE"].append("p{}".format(num))
        if chars >= CLEAN_READ_MIN_CHARS and page.get("state") == "none":
            groups["CLAIMS CLEAN READ"].append("p{}".format(num))
    parts = []
    for label in ("NO TRANSCRIPTION", "INCOMPLETE REPLY", "SHORT PAGE",
                  "CLAIMS CLEAN READ"):
        if groups[label]:
            parts.append("{} {}".format(label, ", ".join(groups[label])))
    counts = dict((k, len(v)) for k, v in groups.items())
    counts["median_chars"] = median
    # Semicolons between flags, commas inside one flag's page list — so
    # "INCOMPLETE REPLY p7, p31" cannot be misread as two flags.
    return "; ".join(parts), counts


def new_ai_ctx(cfg, ep):
    """The per-file AI context: budget, retries, usage sink, page detail.

    Built whenever there is an endpoint, NOT only when alt drafting is on.
    Before v1.9 it was gated on cfg["ai_alt"] and constructed after the OCR
    branch had already run, so the AI OCR path had no channel to report
    usage through at all. Alt drafting is gated separately, at its own call
    site."""
    if ep is None:
        return None
    return {"ep": ep,
            "budget": [AI_MAX_ALT_PER_FILE],
            "retries": cfg.get("ai_retries", AI_MAX_RETRIES),
            "concurrency": cfg.get("ai_concurrency",
                                   DEFAULT_AI_CONCURRENCY),
            "fails": cfg.get("ai_fails"),
            "usage": new_usage(),
            "pages": [],
            "alt_chars": [],
            "alt_failed": 0}


def apply_ai_results(row, ai_ctx, manual, review):
    """Fold one file's AI context into its report row.

    A refused page goes to `manual`, never to `review` and never to Failed:
    the remedy is a person re-running or re-keying that page, but the OCR and
    tagging that did succeed on the other pages are real and must not be
    thrown away with it."""
    if not isinstance(ai_ctx, dict):
        return
    usage = ai_ctx.get("usage") or {}
    row["in_tok"] = usage.get("in", 0)
    row["out_tok"] = usage.get("out", 0)
    row["think_tok"] = usage.get("think", 0)
    row["cache_tok"] = usage.get("cache_read", 0)
    pages = ai_ctx.get("pages") or []
    row["ai_pages"] = len(pages)
    flags, counts = page_flags(pages)
    row["flags"] = flags
    row["flag_counts"] = counts
    alt_lengths = ai_ctx.get("alt_chars") or []
    if alt_lengths:
        row["alt_chars"] = max(alt_lengths)
        over = sum(1 for n in alt_lengths if n > AI_ALT_MAX_CHARS)
        row["alt_over"] = over or ""
        if over:
            review.append(
                "{} alt-text draft(s) run over {} characters — usually a "
                "sign the description belongs in the body text".format(
                    over, AI_ALT_MAX_CHARS))
    refused = counts.get("NO TRANSCRIPTION", 0)
    if refused:
        manual.append(
            "{} page(s) were not transcribed — the model stopped early; "
            "see the Flags column and the placeholders in the sidecar, and "
            "re-run those pages against a stronger model".format(refused))
    if counts.get("INCOMPLETE REPLY"):
        review.append(
            "{} page(s) returned no coverage line — the reply may have "
            "stopped mid-transcription; re-run those pages".format(
                counts["INCOMPLETE REPLY"]))
    if ai_ctx.get("alt_failed"):
        review.append(
            "{} alt-text request(s) failed and were not drafted".format(
                ai_ctx["alt_failed"]))


def join_steps(manual_items, review_items):
    return "; ".join(list(manual_items) + list(review_items))


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
    """Shared writer: header styling, zebra rows, hyperlinked columns."""
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
# NUMBER, and the Admin Record URL hyperlink must stay at 23.
REPORT_HEADERS = ["Order", "File", "Format", "Context ID", "Article ID",
                  "Record Title", "Record State", "Pages", "Text Before",
                  "Text After", "OCR Action", "Tagged", "Doc Title",
                  "Language", "Images", "Alt Missing", "Alt Drafted",
                  "Accessibility", "Next Steps", "Output File", "Status",
                  "Time", "Admin Record URL",
                  "In Tok", "Out Tok", "Think Tok", "Cached Tok",
                  "AI Pages", "Alt Max Chars", "Alt Over Limit", "Flags"]
REPORT_WIDTHS = [8, 44, 11, 26, 10, 36, 15, 8, 11, 11, 18, 14, 12, 10,
                 8, 11, 11, 24, 70, 52, 34, 9, 60,
                 10, 10, 11, 12, 10, 14, 14, 46]
REPORT_LINK_COLS = (23,)

# The checks no automated pass can make — one Summary block, not per-row
# noise (WCAG 2.1 AA).
MANUAL_CHECKS_NOTE = [
    "Checks that always need a human (WCAG 2.1 AA):",
    "· Color contrast of text and meaningful graphics (1.4.3 / 1.4.11)",
    "· Logical reading order and tab order (1.3.2 / 2.4.3)",
    "· Meaningful link text and heading hierarchy (2.4.4 / 1.3.1)",
    "· Table header/data-cell associations (1.3.1)",
    "· Accuracy of OCR text and AI-drafted alt text on critical content",
]


# Shown only when a run actually used an AI endpoint, so a local-tools run
# is not told about flags it cannot produce. The Out Tok / Think Tok
# sentence is the whole diagnosis of the defect this release exists to make
# visible, sitting where the next person will be looking.
AI_REVIEW_NOTE = [
    "AI-generated text in this run needs a human pass:",
    "· Out Tok includes Think Tok, the model's reasoning. Both count",
    "  against the reply limit, so a page refused for max_tokens may show",
    "  far less visible text than the limit would suggest.",
    "· NO TRANSCRIPTION means the model stopped and named a reason, so that",
    "  page is missing from the sidecar — a placeholder marks the gap.",
    "  Re-running it identically will refuse again: raise the reply limit",
    "  or send the page to a stronger model.",
    "· INCOMPLETE REPLY means the page came back with no coverage line, so",
    "  the reply may have stopped mid-transcription without saying so. A",
    "  plain re-run often succeeds; escalate only if it recurs.",
    "· SHORT PAGE means a page under 40% of the median for its own file —",
    "  usually a page the model stopped reading early.",
    "· CLAIMS CLEAN READ means a long transcription reported as wholly",
    "  legible. Check it against the page before trusting it.",
    "· Alt text over the character limit still works, but is usually a sign",
    "  the description belongs in the body text instead.",
    "· Token totals EXCLUDE refused pages: the request was billed but the",
    "  counts are lost with the error, and a refusal spends the whole",
    "  ceiling — so these totals are a floor, not a measurement.",
]


def write_report(path, rows, summary_pairs, header_rgb, extra_note=()):
    from openpyxl import Workbook
    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Files"
    _write_sheet(ws, REPORT_HEADERS, rows, REPORT_WIDTHS,
                 REPORT_LINK_COLS, st)
    ws2 = wb.create_sheet("Summary")
    body = list(summary_pairs)
    if extra_note:
        body += [("", "")] + [(line, "") for line in extra_note]
    body += [("", "")] + [(line, "") for line in MANUAL_CHECKS_NOTE]
    _write_sheet(ws2, ["Item", "Value"], body, [58, 60], (), st)
    wb.save(path)


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s. "files"
# carries a version counter; when it changes the page refetches
# GET /api/files to rebuild the file-selection list (report-consuming
# module pattern).
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",   # idle | starting | scanning | processing | writing | done | error
    "paused": False,
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "last_file": None,      # report workbook
    "run_dir": None,        # DC_OCR_Output_<stamp> folder (copy mode)
    "summary": "",
    "warnings": 0,
    "tools": {},            # local tool availability (filled at start/UI)
    "files": {"loaded": False, "source": "", "count": 0, "version": 0,
              "formats": {}},
}
LOCK = threading.Lock()
MODEL = None                # scanned file entries
STOP_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()

RUNNING_PHASES = ("starting", "scanning", "processing", "writing")


class StopRequested(Exception):
    """Raised inside a worker when the user clicks Stop."""


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary.
RUN_LOG_NAME = "DC_OCR_Log_{}.txt"
CLEAR_RESETS = {"last_file": None, "run_dir": None, "warnings": 0,
                "paused": False}


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
    # Console echo AFTER the state update, and encoding-safe (Windows
    # charmap consoles) — a print() crash must never kill a worker.
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
    """Install a new scanned-file model and bump the UI version counter."""
    global MODEL
    formats = {}
    for e in entries:
        formats[e["format"]] = formats.get(e["format"], 0) + 1
    with LOCK:
        MODEL = entries
        STATE["files"] = {
            "loaded": True, "source": source, "count": len(entries),
            "formats": formats,
            "version": STATE["files"]["version"] + 1,
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


def sleep_interruptible(seconds):
    """Wait without going deaf to Pause/Stop — used for AI retry backoff."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        check_pause_stop()
        time.sleep(min(0.2, max(0.0, end - time.time())))


def eta_text(done, total, t0):
    """mm:ss estimate from the average pace so far."""
    if not done:
        return "ETA --:--"
    remaining = int((time.time() - t0) / done * (total - done))
    return "ETA {:02d}:{:02d}".format(*divmod(remaining, 60))


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _primary_rgb(session) -> str:
    return session.get("branding", {}).get("colors", {}) \
                  .get("primary", FALLBACK_BRAND["primary"]).lstrip("#")


# ---------------------------------------------------------------------------
# Worker — one background thread per run; per-file failures are isolated
# (FileError / unexpected exceptions become a Failed row + warning, and
# the run continues — suite per-unit-failure pattern).
# ---------------------------------------------------------------------------
def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return "{}:{:02d}".format(m, s)


def _out_path(entry, cfg, run_dir, force_ext=None):
    """Destination for a processed copy of `entry` (copy mode mirrors the
    source's relative layout under the run folder)."""
    if cfg["save"] == "overwrite":
        base = entry["path"]
        if force_ext:
            stem = os.path.splitext(base)[0]
            return stem + (cfg["suffix"] or "_ocr") + force_ext
        return base
    rel = entry["rel"]
    stem, ext = os.path.splitext(rel)
    if force_ext:
        ext = force_ext
    dest = os.path.join(run_dir, stem + cfg["suffix"] + ext)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


def _pdf_alt_fn(out_path, ai_ctx, log_fn):
    """alt_fn for tag_pdf: lazily extracts image bytes from the PDF being
    tagged and drafts /Alt via the AI endpoint (per-file budget shared)."""
    if not ai_ctx or ai_ctx.get("ep") is None:
        return None, lambda: 0
    cache = collect_pdf_image_bytes(out_path)
    drafted = [0]

    def alt_fn(pno, xname):
        item = cache.get((pno, xname))
        if item is None or ai_ctx["budget"][0] <= 0:
            return None
        blob, mime = item
        try:
            ai_ctx["budget"][0] -= 1
            text = ai_alt_text(
                ai_ctx["ep"], blob, mime,
                retries=ai_ctx.get("retries", AI_MAX_RETRIES),
                sink=ai_ctx)
            drafted[0] += 1
            _record_alt_length(ai_ctx, text)
            return text or None       # decorative → no /Alt, stays counted
        except AIError as e:
            ai_ctx["alt_failed"] = ai_ctx.get("alt_failed", 0) + 1
            log_fn("    WARNING: alt-text request failed: {}".format(e))
            return None

    return alt_fn, lambda: drafted[0]


def _text_cov(info):
    return "{}/{}".format(info.get("text_pages", 0), info.get("pages", 0))


def process_pdf_entry(entry, cfg, ep, run_dir, row):
    src = entry["path"]
    before = eval_pdf(src)
    row["pages"] = before["pages"]
    row["text_before"] = _text_cov(before)
    row["images"] = before["images"]
    manual, review = [], []
    if before["encrypted"]:
        row["accessibility"] = STATUS_MANUAL
        row["next"] = ("Encrypted PDF — remove the password/restrictions, "
                       "then rerun")
        row["status"] = "Skipped (encrypted)"
        return row

    if cfg["mode"] == "evaluate":
        row["ocr"] = "Evaluate only"
        row["text_after"] = row["text_before"]
        row["tagged"] = "Yes" if before["tagged"] else "No"
        row["doc_title"] = "Present" if before["title"].strip() else "Missing"
        row["language"] = before["lang"] or "Missing"
        if before["text_pages"] < before["pages"]:
            manual.append("Run OCR ({} page(s) without text)".format(
                before["pages"] - before["text_pages"]))
        if not before["tagged"]:
            manual.append("Tag the document (rerun in a processing mode, "
                          "then review semantics)")
            if before["images"]:
                manual.append("Add alt text for {} image(s)".format(
                    before["images"]))
        if not before["title"].strip():
            manual.append("Set a descriptive document title")
        if not before["lang"]:
            manual.append("Set the document language")
        row["accessibility"] = classify_accessibility(manual, review)
        row["next"] = join_steps(manual, review)
        row["status"] = "Evaluated"
        return row

    # --- processing modes ---
    need_ocr = (cfg["mode"] == "force"
                or before["text_pages"] < before["pages"])
    out = _out_path(entry, cfg, run_dir)
    # Built here, above the OCR branch, and not gated on cfg["ai_alt"] —
    # otherwise the AI OCR path has no channel to report usage through.
    ai_ctx = new_ai_ctx(cfg, ep)
    if cfg["engine"] == "ai":
        if cfg["save"] == "copy":
            shutil.copy2(src, out)
        if need_ocr:
            sidecar = os.path.splitext(out)[0] + "_ai_text.txt"
            ai_ocr_pdf_sidecar(ep, out, sidecar, log,
                               check_pause_stop,
                               retries=cfg["ai_retries"], sink=ai_ctx,
                               concurrency=cfg.get(
                                   "ai_concurrency",
                                   DEFAULT_AI_CONCURRENCY))
            row["ocr"] = "AI text sidecar"
            review.append("AI OCR wrote a text sidecar ({}) — the PDF "
                          "itself has no new text layer; run the local "
                          "engine for a searchable PDF".format(
                              os.path.basename(sidecar)))
        else:
            row["ocr"] = "None needed"
    else:
        if need_ocr:
            tmp = out + ".ocr.tmp"
            tail = run_ocrmypdf(src, tmp, cfg["mode"], cfg["langs"],
                                cfg["jobs"], cfg["deskew"])
            os.replace(tmp, out)
            for t in tail[-2:]:
                if t.strip():
                    log("    ocrmypdf: " + t.strip())
            row["ocr"] = ("Force OCR" if cfg["mode"] == "force"
                          else "OCR (new text layer)")
            review.append("Confirm OCR text accuracy on critical content")
        else:
            if cfg["save"] == "copy":
                shutil.copy2(src, out)
            row["ocr"] = "None needed"

    # Metadata + heuristic tagging on the output file.
    title = prettify_title(entry) if cfg["set_title"] else ""
    tag_res = {"tagged": False, "already": before["tagged"], "alts": 0,
               "figures": 0, "title_set": False, "lang_set": False}
    if cfg["tag"] or cfg["set_title"] or cfg["doc_lang"]:
        alt_fn, drafted_count = (None, lambda: 0)
        # Alt drafting is gated separately from the context itself.
        alt_ctx = ai_ctx if (ai_ctx and cfg["ai_alt"]) else None
        try:
            if cfg["tag"] and alt_ctx:
                alt_fn, drafted_count = _pdf_alt_fn(out, alt_ctx, log)
            tag_res = tag_pdf(out, cfg["doc_lang"],
                              title, alt_fn if cfg["tag"] else None)
        except FileError as e:
            add_warning()
            log("    WARNING: {}".format(e))
            manual.append("Auto-tagging failed — tag in Acrobat")
        row["alt_drafted"] = tag_res.get("alts", 0)

    after = eval_pdf(out)
    row["text_after"] = _text_cov(after)
    row["output"] = out
    row["tagged"] = ("Yes" if tag_res.get("already") or before["tagged"]
                     else ("Yes (heuristic)" if tag_res.get("tagged")
                           else "No"))
    row["doc_title"] = ("Set" if tag_res.get("title_set")
                        else ("Present" if after["title"].strip()
                              else "Missing"))
    row["language"] = after["lang"] or "Missing"

    if after["text_pages"] < after["pages"]:
        manual.append("{} page(s) still have no text layer — inspect the "
                      "source scans".format(
                          after["pages"] - after["text_pages"]))
    if tag_res.get("tagged"):
        review.append("Heuristic tags only (paragraphs/figures) — review "
                      "headings, lists, tables, and reading order in "
                      "Acrobat")
        missing_alt = tag_res.get("figures", 0) - tag_res.get("alts", 0)
        if missing_alt > 0:
            manual.append("Add alt text for {} image(s) (no AI draft)"
                          .format(missing_alt))
        if tag_res.get("alts"):
            review.append("Review {} AI-drafted alt text(s) for accuracy"
                          .format(tag_res["alts"]))
    elif not (tag_res.get("already") or before["tagged"]):
        manual.append("Untagged — tag the document in Acrobat")
        if after["images"]:
            manual.append("Add alt text for {} image(s)".format(
                after["images"]))
    if row["doc_title"] == "Missing":
        manual.append("Set a descriptive document title")
    if row["language"] == "Missing":
        manual.append("Set the document language")

    apply_ai_results(row, ai_ctx, manual, review)
    row["accessibility"] = classify_accessibility(manual, review)
    row["next"] = join_steps(manual, review)
    row["status"] = "Processed"
    return row


def process_image_entry(entry, cfg, ep, run_dir, row):
    src = entry["path"]
    row["pages"] = 1
    row["images"] = 1
    row["text_before"] = "0/1"
    manual, review = [], []
    ai_ctx = new_ai_ctx(cfg, ep)
    if cfg["mode"] == "evaluate":
        row["ocr"] = "Evaluate only"
        row["text_after"] = "0/1"
        manual.append("Image file — produce a searchable PDF or text "
                      "equivalent, plus alt text where it is used")
        row["accessibility"] = STATUS_MANUAL
        row["next"] = join_steps(manual, review)
        row["status"] = "Evaluated"
        return row

    outputs = []
    if cfg["engine"] == "ai":
        sidecar = _out_path(entry, cfg, run_dir, force_ext="_ai_text.txt") \
            if cfg["save"] == "overwrite" else \
            os.path.splitext(_out_path(entry, cfg, run_dir))[0] + \
            "_ai_text.txt"
        if cfg["save"] == "copy":
            shutil.copy2(src, _out_path(entry, cfg, run_dir))
        ai_ocr_image_sidecar(ep, src, sidecar,
                             retries=cfg["ai_retries"], sink=ai_ctx,
                             log_fn=log)
        outputs.append(sidecar)
        row["ocr"] = "AI text sidecar"
        row["text_after"] = "1/1" if os.path.getsize(sidecar) else "0/1"
        review.append("AI OCR wrote a text sidecar — review it, and run "
                      "the local engine for a searchable PDF")
    else:
        made_text = False
        if cfg["image_out"] in ("pdf", "both"):
            pdf_out = _out_path(entry, cfg, run_dir, force_ext=".pdf")
            tmp = pdf_out + ".ocr.tmp"
            run_ocrmypdf(src, tmp, "auto", cfg["langs"], cfg["jobs"],
                         cfg["deskew"], image_dpi=300)
            os.replace(tmp, pdf_out)
            outputs.append(pdf_out)
            title = prettify_title(entry) if cfg["set_title"] else ""
            if cfg["tag"] or title or cfg["doc_lang"]:
                try:
                    tag_pdf(pdf_out, cfg["doc_lang"], title, None)
                except FileError as e:
                    add_warning()
                    log("    WARNING: {}".format(e))
            after = eval_pdf(pdf_out)
            made_text = after["text_pages"] >= 1
            row["ocr"] = "OCR to searchable PDF"
        if cfg["image_out"] in ("text", "both"):
            base = _out_path(entry, cfg, run_dir)
            sidecar = os.path.splitext(base)[0] + "_text.txt"
            text = run_tesseract_text(src)
            Path(sidecar).write_text(text, encoding="utf-8")
            outputs.append(sidecar)
            made_text = made_text or bool(text.strip())
            if cfg["image_out"] == "text":
                row["ocr"] = "OCR to text sidecar"
            if cfg["save"] == "copy" and cfg["image_out"] == "text":
                shutil.copy2(src, base)
        row["text_after"] = "1/1" if made_text else "0/1"
        if not made_text:
            manual.append("OCR found no text — if the image contains "
                          "text, review scan quality")
        review.append("Confirm OCR text accuracy on critical content")

    # Alt-text draft for the image itself (report + _alt.txt sidecar —
    # there is nowhere to embed alt inside a bare image file).
    if ai_ctx and cfg["ai_alt"]:
        try:
            alt = ai_alt_text(ep, Path(src).read_bytes(),
                              guess_image_mime(os.path.splitext(src)[1]),
                              retries=cfg["ai_retries"], sink=ai_ctx)
            _record_alt_length(ai_ctx, alt)
            if alt:
                base = outputs[0] if outputs else src
                alt_path = os.path.splitext(base)[0] + "_alt.txt"
                Path(alt_path).write_text(alt, encoding="utf-8")
                outputs.append(alt_path)
                row["alt_drafted"] = 1
                review.append("Review the AI-drafted alt text (_alt.txt) "
                              "and add it where the image is used")
        except AIError as e:
            add_warning()
            ai_ctx["alt_failed"] = ai_ctx.get("alt_failed", 0) + 1
            log("    WARNING: alt-text request failed: {}".format(e))
    if not row["alt_drafted"]:
        manual.append("Write alt text for wherever this image is used")

    row["output"] = "; ".join(outputs)
    apply_ai_results(row, ai_ctx, manual, review)
    row["accessibility"] = classify_accessibility(manual, review)
    row["next"] = join_steps(manual, review)
    row["status"] = "Processed"
    return row


def process_office_entry(entry, cfg, ep, run_dir, row):
    src = entry["path"]
    is_docx = entry["format"] == "docx"
    before = eval_docx(src) if is_docx else eval_pptx(src)
    row["pages"] = before.get("slides", "")
    row["images"] = before["images"]
    row["text_before"] = "—"
    row["text_after"] = "—"
    row["ocr"] = "—"
    manual, review = [], []
    ai_ctx = new_ai_ctx(cfg, ep) if cfg.get("ai_alt") else None

    def flag_structure(info, alt_missing, alt_drafted):
        if alt_missing:
            manual.append("Add alt text for {} image(s)".format(alt_missing))
        if alt_drafted:
            review.append("Review {} AI-drafted alt text(s) for accuracy"
                          .format(alt_drafted))
        if is_docx:
            if not info["headings"]:
                manual.append("Apply Heading styles to structure the "
                              "document")
            if info["tables"]:
                review.append("Verify header rows are marked on {} "
                              "table(s)".format(info["tables"]))
        else:
            if info["untitled"]:
                manual.append("Give {} slide(s) a unique title".format(
                    info["untitled"]))

    if cfg["mode"] == "evaluate":
        row["doc_title"] = ("Present" if before["title"].strip()
                            else "Missing")
        if row["doc_title"] == "Missing":
            manual.append("Set a document title (File > Info > Properties)")
        flag_structure(before, before["alt_missing"], 0)
        row["accessibility"] = classify_accessibility(manual, review)
        row["next"] = join_steps(manual, review)
        row["status"] = "Evaluated"
        return row

    out = _out_path(entry, cfg, run_dir)
    title = prettify_title(entry) if cfg["set_title"] else ""

    if is_docx:
        res = process_docx(src, out, title, ai_ctx, log)
    else:
        res = process_pptx(src, out, title, ai_ctx, log)
    row["alt_drafted"] = res["alt_drafted"]
    row["output"] = out
    row["doc_title"] = ("Set" if res["title_set"]
                        else ("Present" if before["title"].strip()
                              else "Missing"))
    if row["doc_title"] == "Missing":
        manual.append("Set a document title (File > Info > Properties)")
    after_info = dict(before)
    after_info["alt_missing"] = res["alt_missing"]
    if not is_docx:
        after_info["untitled"] = res["untitled"]
    flag_structure(after_info, res["alt_missing"], res["alt_drafted"])
    apply_ai_results(row, ai_ctx, manual, review)
    row["accessibility"] = classify_accessibility(manual, review)
    row["next"] = join_steps(manual, review)
    row["status"] = "Processed"
    return row


def process_one(entry, idx, cfg, ep, run_dir):
    """Returns a finished report-row dict; never raises for per-file
    problems (FileError → Failed row + warning)."""
    t_file = time.time()
    row = {"order": idx, "file": entry["rel"],
           "format": FORMAT_LABELS[entry["format"]],
           "ctx": entry["ctx"], "article": entry["article"],
           "title": entry["title"], "state": entry["state"],
           "pages": "", "text_before": "—", "text_after": "—",
           "ocr": "—", "tagged": "—", "doc_title": "—", "language": "—",
           "images": "", "alt_missing": "", "alt_drafted": 0,
           "accessibility": "", "next": "", "output": "", "status": "",
           "admin_url": entry["admin_url"],
           # Seeded zeroed so a Failed or Skipped row still sums cleanly.
           "in_tok": 0, "out_tok": 0, "think_tok": 0, "cache_tok": 0,
           "ai_pages": 0, "alt_chars": 0, "alt_over": "", "flags": "",
           "flag_counts": {}}
    try:
        if entry["format"] == "pdf":
            row = process_pdf_entry(entry, cfg, ep, run_dir, row)
        elif entry["format"] == "image":
            row = process_image_entry(entry, cfg, ep, run_dir, row)
        elif entry["format"] in ("docx", "pptx"):
            row = process_office_entry(entry, cfg, ep, run_dir, row)
        elif entry["format"] == "legacy":
            row["accessibility"] = STATUS_MANUAL
            row["next"] = LEGACY_ADVICE
            row["status"] = "Not processed (legacy format)"
        else:
            row["accessibility"] = STATUS_NA
            row["next"] = OTHER_ADVICE
            row["status"] = "Not processed"
    except (StopRequested, SystemicAIFailure):
        # Neither is this file's problem: Stop is the user, and a systemic
        # AI failure means the next forty-nine files would fail the same way.
        raise
    except FileError as e:
        add_warning()
        log("  WARNING: {}: {}".format(entry["rel"], e))
        row["accessibility"] = STATUS_FAILED
        row["status"] = "Failed"
        row["next"] = str(e)
    except Exception as e:
        add_warning()
        log("  WARNING: {}: unexpected {}: {}".format(
            entry["rel"], e.__class__.__name__, e))
        row["accessibility"] = STATUS_FAILED
        row["status"] = "Failed"
        row["next"] = "Unexpected {}: {}".format(e.__class__.__name__, e)
    # Fill the Alt Missing column from the next-steps text when set there.
    m = re.search(r"Add alt text for (\d+)", row["next"])
    if m:
        row["alt_missing"] = int(m.group(1))
    elif row["images"] != "":
        row["alt_missing"] = row.get("alt_missing") or 0
    row["time"] = _hms(time.time() - t_file)
    return row


MODE_LABELS = {"evaluate": "Evaluate only",
               "auto": "Process files that need work",
               "force": "Reprocess everything (force OCR)"}


def _row_values(row):
    return [row["order"], row["file"], row["format"], row["ctx"],
            row["article"], row["title"], row["state"], row["pages"],
            row["text_before"], row["text_after"], row["ocr"],
            row["tagged"], row["doc_title"], row["language"],
            row["images"], row["alt_missing"], row["alt_drafted"],
            row["accessibility"], row["next"], row["output"],
            row["status"], row["time"], row["admin_url"],
            # Zero renders blank: an unreported figure must not read as a
            # measured nought.
            row.get("in_tok") or "", row.get("out_tok") or "",
            row.get("think_tok") or "", row.get("cache_tok") or "",
            row.get("ai_pages") or "", row.get("alt_chars") or "",
            row.get("alt_over") or "", row.get("flags") or ""]


def process_worker(session, cfg, ep):
    header_rgb = _primary_rgb(session)
    rows = []
    t0 = time.time()
    entries = cfg["entries"]
    total = len(entries)
    run_dir = None
    report_dir = cfg["out_dir"]
    run_stamp = stamp()

    def finish_report(note=""):
        if not rows:
            return None
        suffix = "_PARTIAL" if note else ""
        counts = {}
        for r in rows:
            counts[r["accessibility"]] = counts.get(r["accessibility"],
                                                    0) + 1
        with LOCK:
            n_warn = STATE["warnings"]
        pairs = [("Run", time.strftime("%Y-%m-%d %H:%M")),
                 ("Module", "{} v{}".format(MANIFEST["name"],
                                            MANIFEST["version"])),
                 ("Source", cfg["source"]),
                 ("Mode", MODE_LABELS.get(cfg["mode"], cfg["mode"])),
                 ("Save mode", "Overwrite originals"
                  if cfg["save"] == "overwrite" else "New copies"),
                 ("OCR engine", "AI endpoint: " + ep["name"] if ep and
                  cfg["engine"] == "ai" else "Local (ocrmypdf/Tesseract)"),
                 ("AI alt text", "on ({})".format(ep["name"])
                  if ep and cfg["ai_alt"] else "off"),
                 ("Files in run", "{} of {} queued{}".format(
                     len(rows), total,
                     " — " + note if note else ""))]
        used_ai = bool(ep) and (cfg["engine"] == "ai" or cfg["ai_alt"])
        if used_ai:
            pairs.append(("Concurrent AI requests",
                          cfg.get("ai_concurrency",
                                  DEFAULT_AI_CONCURRENCY)))
            pairs.append(("Reply limit", "{} output tokens per page, {} "
                          "per alt draft".format(AI_OCR_MAX_TOKENS,
                                                 AI_ALT_MAX_TOKENS)))
            pairs.append(("Max AI raster dimension",
                          "{} px".format(AI_OCR_MAX_DIM)))
            tok_in = sum(r.get("in_tok") or 0 for r in rows)
            tok_out = sum(r.get("out_tok") or 0 for r in rows)
            tok_think = sum(r.get("think_tok") or 0 for r in rows)
            tok_cache = sum(r.get("cache_tok") or 0 for r in rows)
            ai_pages = sum(r.get("ai_pages") or 0 for r in rows)
            flag_totals = {}
            for r in rows:
                for key, val in (r.get("flag_counts") or {}).items():
                    if key != "median_chars":
                        flag_totals[key] = flag_totals.get(key, 0) + val
            pairs += [
                ("AI pages transcribed", ai_pages),
                ("Total input tokens", "{:,}".format(tok_in)),
                ("Total output tokens", "{:,}".format(tok_out)),
                # Zero here means "this endpoint publishes no separate
                # figure", not "the model did no thinking" — Anthropic folds
                # reasoning into output_tokens and reports nothing apart.
                ("  of which reasoning", "{:,}{}".format(
                    tok_think,
                    " — counted in the output total above, and against the "
                    "reply limit" if tok_think else
                    " (this endpoint reports no separate figure)")),
                ("Total cached input tokens", "{:,}".format(tok_cache)),
                ("Pages not transcribed",
                 "{} (model stopped and named a reason)".format(
                     flag_totals.get("NO TRANSCRIPTION", 0))),
                ("Pages with no coverage line",
                 "{} (reply may have stopped mid-transcription)".format(
                     flag_totals.get("INCOMPLETE REPLY", 0))),
                ("Unusually short pages",
                 "{} (under {:.0f}% of their own file's median)".format(
                     flag_totals.get("SHORT PAGE", 0),
                     SHORT_PAGE_RATIO * 100)),
                ("Pages claiming a clean read",
                 "{} (over {:,} characters, nothing uncertain)".format(
                     flag_totals.get("CLAIMS CLEAN READ", 0),
                     CLEAN_READ_MIN_CHARS)),
                ("Alt drafts over the limit",
                 sum(1 for r in rows if r.get("alt_over"))),
            ]
        pairs += [("Result: " + k, v) for k, v in sorted(counts.items())]
        pairs.append(("Warnings", n_warn))
        try:
            fname = "DC_OCR_Report_{}{}.xlsx".format(run_stamp, suffix)
            path = os.path.join(report_dir, fname)
            write_report(path, [_row_values(r) for r in rows], pairs,
                         header_rgb,
                         extra_note=AI_REVIEW_NOTE if used_ai else ())
            log("Report written: " + path)
            set_state(last_file=path)
            return path
        except Exception as e:
            log("ERROR writing report: {}: {}".format(
                e.__class__.__name__, e))
            return None

    try:
        set_state(phase="processing", summary="", warnings=0,
                  last_file=None, run_dir=None)
        # One window per RUN, not per file: a dead key shows itself across
        # files, and the guard exists to stop the run rather than each file
        # in turn.
        cfg["ai_fails"] = FailWindow()
        if cfg["save"] == "copy" and cfg["mode"] != "evaluate":
            # Evaluate-only runs touch nothing — the report goes straight
            # to the output folder, with no (empty) run folder.
            run_dir = os.path.join(cfg["out_dir"],
                                   "DC_OCR_Output_" + run_stamp)
            os.makedirs(run_dir, exist_ok=True)
            report_dir = run_dir
            set_state(run_dir=run_dir)
            log("Output run folder: " + run_dir)
        log("Processing {} file(s) — {} · {}.".format(
            total, MODE_LABELS.get(cfg["mode"], cfg["mode"]),
            "overwriting originals" if cfg["save"] == "overwrite"
            else "new copies"))
        for idx, entry in enumerate(entries, start=1):
            check_pause_stop()
            set_progress(idx - 1, total, "{} · {}".format(
                entry["rel"], eta_text(idx - 1, total, t0)))
            log("({}/{}) {}".format(idx, total, entry["rel"]))
            rows.append(process_one(entry, idx, cfg, ep, run_dir))
        set_state(phase="writing")
        set_progress(total, total, "Writing report…")
        path = finish_report()
        with LOCK:
            n_warn = STATE["warnings"]
        counts = {}
        for r in rows:
            counts[r["accessibility"]] = counts.get(r["accessibility"],
                                                    0) + 1
        # "Meets automated checks: 4 · Needs manual work: 14" — the status
        # as the report spells it, then its count. "4 meets automated
        # checks" read as a sentence and was not one (2026-10-02).
        summary = "{} file(s) — ".format(len(rows)) + " · ".join(
            "{}: {}".format(k, v) for k, v in sorted(counts.items()))
        if n_warn:
            summary += " · {} warning(s), see log".format(n_warn)
        if path:
            summary += " · report: " + os.path.basename(path)
        set_progress(total, total, "Done")
        set_state(phase="done", summary=summary, paused=False)
    except SystemicAIFailure as e:
        # Not "unexpected": the run was stopped deliberately, and the
        # partial report plus the reason is the whole point of stopping.
        log("AI endpoint failing systemically — {}".format(e))
        path = finish_report("stopped: AI endpoint failing")
        fail("Run stopped after {} of {} file(s) — {}{}".format(
            len(rows), total, e,
            " Partial report: " + os.path.basename(path) if path else ""))
    except StopRequested:
        log("Stop requested — {} of {} file(s) done.".format(
            len(rows), total))
        path = finish_report("stopped early")
        if path:
            set_state(phase="done", paused=False,
                      summary="Stopped early ({} of {} file(s)). Partial "
                              "report: {}".format(len(rows), total,
                                                  os.path.basename(path)))
        else:
            set_state(phase="idle", paused=False)
            set_progress(0, 0, "")
            log("Stopped before any file was processed.")
    except Exception as e:
        finish_report("unexpected error")
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))
    finally:
        # DC-LOG: the whole run's log, in its run folder, on every
        # way out of the run.
        save_run_log(run_dir, run_stamp)


# ---------------------------------------------------------------------------
# UI — branded page per suite conventions (WCAG 2.1 AA). Raw string + token
# replace (no str.format), so CSS/JS braces and backslashes stay untouched.
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
 main{max-width:48rem;margin:0 auto;padding:1.25rem}
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
 .row>div{flex:1;min-width:12rem}
 .controls{display:flex;gap:.6rem;flex-wrap:wrap}
 label{display:block;margin-bottom:.3rem}
 .inline{display:flex;gap:.45rem;align-items:baseline;margin:.35rem 0}
 .inline label{margin:0}
 .optgrid{display:flex;gap:2rem;flex-wrap:wrap}
 .optgrid>div{min-width:14rem;flex:1}
 .optgrid h3{font-size:.95rem;margin:.2rem 0 .1rem}
 .hint{font-size:.85rem;color:#54524c;margin:.35rem 0 0}
 .warnbox{font-size:.9rem;background:#f7efe2;border:1px solid #b98a2f;border-radius:6px;
   padding:.5rem .7rem;margin:.5rem 0 0}
 .sum{margin:.6rem 0 0;padding:.5rem .7rem;border-radius:6px;background:#eef1f5;
      border:1px solid #d7d4cc;font-size:.9rem}
 .sum.loaded{background:#e8f0e6;border-color:#4a6741}
 #list{border:1px solid #d7d4cc;border-radius:6px;max-height:16rem;overflow:auto;
       margin-top:.5rem;background:#fff}
 #list .item{display:flex;gap:.5rem;align-items:baseline;padding:.3rem .6rem;border-bottom:1px solid #efece6}
 #list .item:last-child{border-bottom:0}
 #list .meta{font-size:.82rem;color:#54524c}
 details{margin:.5rem 0;border:1px solid #e2ded5;border-radius:6px;padding:.4rem .7rem;background:#fbfaf7}
 details summary{cursor:pointer;font-weight:600;font-size:.92rem}
 details p,details ul{font-size:.88rem;margin:.4rem 0}
 details code{background:#eef1f5;padding:0 .25rem;border-radius:3px;font-size:.85em}
 table.eps{width:100%;border-collapse:collapse;margin:.5rem 0;font-size:.9rem}
 table.eps th,table.eps td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #e2ded5}
 table.eps .rowbtn{background:none;border:0;color:#1a5dc8;text-decoration:underline;
   cursor:pointer;font:inherit;padding:0}
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
 #modalback{position:fixed;inset:0;background:rgba(20,20,25,.45);display:none;
   align-items:flex-start;justify-content:center;padding:3rem 1rem;z-index:9}
 #modal{background:#fff;border-radius:8px;max-width:30rem;width:100%;padding:1rem 1.2rem}
 #modal h2{margin:.1rem 0 .6rem;font-size:1.05rem}
 #modal .foot{display:flex;gap:.6rem;justify-content:flex-end;margin-top:.9rem}
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


<fieldset><legend>1 · Files to process</legend>
 <div class="inline">
  <input type="radio" id="srcrun" name="src" value="run" checked>
  <label for="srcrun">A Batch File Downloader run — record details are read
   from its reports</label>
 </div>
 <div class="inline">
  <input type="radio" id="srcfolder" name="src" value="folder">
  <label for="srcfolder">A folder of files</label>
 </div>
 <div class="inline">
  <input type="radio" id="srcpaths" name="src" value="paths">
  <label for="srcpaths">Individual files (one full path per line)</label>
 </div>
 <div class="row" id="folderrow">
  <div>
   <label for="srcdir" id="srcdirlabel">Downloader run folder (or the folder
    that holds it)</label>
   <input type="text" id="srcdir" value="~/Desktop" autocomplete="off" spellcheck="false">
  </div>
  <div style="flex:0;min-width:auto" id="recwrap">
   <div class="inline"><input type="checkbox" id="recursive">
    <label for="recursive">Include subfolders</label></div>
  </div>
  <button type="button" id="scan" class="small">Scan</button>
 </div>
 <div id="pathswrap" hidden>
  <label for="pathbox">File paths</label>
  <textarea id="pathbox" spellcheck="false"
   placeholder="/Users/you/Desktop/scan1.pdf&#10;/Users/you/Desktop/thesis.docx"></textarea>
  <div class="row"><button type="button" id="scanpaths" class="small">Add files</button></div>
 </div>
 <div id="filesum" class="sum" role="status" aria-live="polite">No files queued yet.</div>
 <div id="pickwrap" hidden>
  <div class="row">
   <div><label for="filter">Filter queued files</label>
    <input type="text" id="filter" placeholder="type to filter…" autocomplete="off"></div>
   <button type="button" id="selall" class="small secondary">Check all</button>
   <button type="button" id="selnone" class="small secondary">Uncheck all</button>
  </div>
  <div id="list" role="group" aria-label="Queued files"></div>
  <p class="hint" id="selcount">0 files checked.</p>
 </div>
</fieldset>

<fieldset><legend>2 · Processing</legend>
 <div class="inline">
  <input type="radio" id="meval" name="mode" value="evaluate">
  <label for="meval">Evaluate only — audit every file, change nothing</label>
 </div>
 <div class="inline">
  <input type="radio" id="mauto" name="mode" value="auto" checked>
  <label for="mauto">Process files that need work — OCR only where text is
   missing (skips pages that already have text)</label>
 </div>
 <div class="inline">
  <input type="radio" id="mforce" name="mode" value="force">
  <label for="mforce">Reprocess everything — force OCR on all pages, even
   ones with existing text</label>
 </div>
 <div class="optgrid" style="margin-top:.6rem">
  <div>
   <h3 id="pdfh">PDF &amp; image options</h3>
   <div class="inline"><input type="checkbox" id="tag" checked>
    <label for="tag">Auto-tag untagged PDFs (heuristic — paragraphs and
     figures; semantics still need review)</label></div>
   <div class="inline"><input type="checkbox" id="deskew" checked>
    <label for="deskew">Deskew and auto-rotate pages during OCR</label></div>
   <div class="row">
    <div><label for="langs">OCR language(s) — Tesseract codes</label>
     <input type="text" id="langs" value="eng" autocomplete="off"></div>
    <div><label for="jobs">Parallel OCR jobs</label>
     <input type="number" id="jobs" min="1" max="16" value="1"></div>
   </div>
  </div>
  <div>
   <h3 id="metah">Document metadata</h3>
   <div class="inline"><input type="checkbox" id="settitle" checked>
    <label for="settitle">Set a document title where missing (record title
     from the downloader report, else the filename)</label></div>
   <label for="doclang" style="margin-top:.4rem">Document language tag</label>
   <input type="text" id="doclang" value="en-US" autocomplete="off">
   <h3 id="imgh" style="margin-top:.7rem">Image files become</h3>
   <div class="inline"><input type="radio" id="iopdf" name="imgout" value="pdf" checked>
    <label for="iopdf">A searchable PDF</label></div>
   <div class="inline"><input type="radio" id="iotext" name="imgout" value="text">
    <label for="iotext">A plain-text sidecar (_text.txt)</label></div>
   <div class="inline"><input type="radio" id="ioboth" name="imgout" value="both">
    <label for="ioboth">Both</label></div>
  </div>
 </div>
</fieldset>

<fieldset><legend>3 · AI assistance (optional)</legend>
 <div class="row">
  <div>
   <label for="endpoint">AI endpoint</label>
   <select id="endpoint"><option value="">None — local tools only</option></select>
  </div>
  <div style="flex:0 0 11rem">
   <label for="airetries">Retries on rate limit</label>
   <input type="number" id="airetries" min="0" max="10" value="4" disabled>
  </div>
  <div style="flex:0 0 11rem">
   <label for="aiconc">Concurrent AI requests</label>
   <input type="number" id="aiconc" min="1" max="4" value="1" disabled>
  </div>
  <button type="button" id="testep" class="small secondary" disabled>Test</button>
 </div>
 <p class="hint">Requests stream, so a long transcription cannot be cut off
  part-way by an idle connection. If a request is rate-limited or the
  connection drops, it is retried this many times with a doubling wait
  (10s, 20s, 40s…). Raise it for a busy or free-tier key; 0 disables
  retrying.</p>
 <p class="hint">Concurrent requests send that many pages of the SAME file at
  once. A 50-page AI OCR pass is otherwise strictly serial, at roughly nine
  seconds a page. Leave it at 1 unless you are waiting: 1 runs the original
  serial loop untouched, and it is the first thing to put back if the
  endpoint starts refusing. Files are always processed one at a time —
  running several at once would multiply Ghostscript and Tesseract against
  the same cores.</p>
 <div class="optgrid">
  <div>
   <h3 id="engh">OCR engine</h3>
   <div class="inline"><input type="radio" id="englocal" name="engine" value="local" checked>
    <label for="englocal">Local (ocrmypdf + Tesseract) — embeds a real text
     layer</label></div>
   <div class="inline"><input type="radio" id="engai" name="engine" value="ai" disabled>
    <label for="engai">AI endpoint — writes plain-text sidecars only (an AI
     pass cannot embed an invisible text layer)</label></div>
  </div>
  <div>
   <h3 id="alth">Alt text</h3>
   <div class="inline"><input type="checkbox" id="aialt" disabled>
    <label for="aialt">Draft alt text with the AI endpoint for images
     missing it (embedded in Word/PowerPoint and PDF tags; always flagged
     for review)</label></div>
  </div>
 </div>
 <div id="epstatus" class="hint" role="status" aria-live="polite"></div>
 <p class="hint">Endpoints are managed for the whole suite in
  <a href="__HUB__#/settings" target="dcAdminSuiteHub">Settings ›
  AI endpoints</a> — add or rotate a key there once and every module sees it,
  including this one, without relaunching. Use <strong>Refresh</strong> after
  adding one.</p>
 <div class="row">
  <button type="button" id="refreshep" class="small secondary">Refresh
   endpoint list</button>
 </div>
</fieldset>

<fieldset><legend>4 · Output</legend>
 <div class="inline">
  <input type="radio" id="scopy" name="save" value="copy" checked>
  <label for="scopy">Save new copies — originals untouched</label>
 </div>
 <div class="inline">
  <input type="radio" id="sover" name="save" value="overwrite">
  <label for="sover">Overwrite the original files in place</label>
 </div>
 <div class="row">
  <div>
   <label for="outdir" id="outdirlabel">Output folder (report always saves here)</label>
   <input type="text" id="outdir" value="~/Desktop" autocomplete="off" spellcheck="false">
  </div>
  <div id="sufwrap">
   <label for="suffix">Filename suffix for copies (optional)</label>
   <input type="text" id="suffix" value="" placeholder="e.g. _accessible" autocomplete="off">
  </div>
 </div>
 <p class="hint">New copies land in DC_OCR_Output_&lt;timestamp&gt;/ here,
 mirroring the source layout; the run report
 (DC_OCR_Report_&lt;timestamp&gt;.xlsx) is written beside them. In
 overwrite mode files are replaced atomically and sidecars are written
 next to the originals.</p>
 <div class="warnbox" id="overwarn" hidden>Overwrite mode replaces the
  original files. Evaluate-only runs never modify files, but a processing
  run in this mode cannot be undone — keep the downloader run folder (or a
  backup) if you may need the originals.</div>
</fieldset>

<fieldset><legend>5 · Local tools</legend>
 <div class="chips" id="toolchips" role="status" aria-live="polite"></div>
 <div class="row"><button type="button" id="recheck" class="small secondary">Re-check tools</button></div>
 <details><summary>Install the local OCR tools</summary>
  <p>Python packages (run with the same Python that launched the suite):</p>
  <p><code>python3 -m pip install ocrmypdf pikepdf pymupdf python-docx python-pptx openpyxl</code></p>
  <p>System tools (ocrmypdf calls these):</p>
  <ul>
   <li>Windows: <code>winget install -e --id tesseract.ocr</code> and
    <code>winget install -e --id ArtifexSoftware.Ghostscript</code></li>
   <li>macOS: <code>brew install tesseract ghostscript</code></li>
  </ul>
  <p>Then click Re-check tools — no restart needed.</p></details>
</fieldset>

<div class="controls">
 <button type="button" id="go" disabled>Start</button>
 <button type="button" id="pause" class="secondary" disabled>Pause</button>
 <button type="button" id="stop" class="secondary" disabled>Stop</button>
</div>

<div id="status" role="status" aria-live="polite">Idle — scan a source to queue files.</div>
<progress id="prog" max="1" value="0" hidden aria-label="Job progress"></progress>
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
const srcrun=$('srcrun'),srcfolder=$('srcfolder'),srcpaths=$('srcpaths'),
      srcdir=$('srcdir'),srcdirlabel=$('srcdirlabel'),recwrap=$('recwrap'),
      recursive=$('recursive'),scanBtn=$('scan'),pathswrap=$('pathswrap'),
      pathbox=$('pathbox'),scanpaths=$('scanpaths'),folderrow=$('folderrow'),
      filesum=$('filesum'),pickwrap=$('pickwrap'),filter=$('filter'),
      list=$('list'),selcount=$('selcount'),selall=$('selall'),selnone=$('selnone'),
      tag=$('tag'),deskew=$('deskew'),langs=$('langs'),jobs=$('jobs'),
      settitle=$('settitle'),doclang=$('doclang'),
      endpoint=$('endpoint'),testep=$('testep'),
      refreshep=$('refreshep'),epstatus=$('epstatus'),
      englocal=$('englocal'),engai=$('engai'),aialt=$('aialt'),
      airetries=$('airetries'),
      aiconc=$('aiconc'),
      outdir=$('outdir'),suffix=$('suffix'),sufwrap=$('sufwrap'),
      overwarn=$('overwarn'),toolchips=$('toolchips'),recheck=$('recheck'),
      go=$('go'),pauseBtn=$('pause'),stopBtn=$('stop'),
      status=$('status'),prog=$('prog'),logBox=$('log');
let paused=false,filesVersion=0,entries=[],checked=new Set(),
    aiCfg={endpoints:[],'default':''},tools={};
const RUNNING=['starting','scanning','processing','writing'];

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
async function api(path,body){
 const opts=body?{method:'POST',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify(body)}:{};
 const r=await fetch(path,opts);
 let data=null;
 try{data=await r.json()}catch(e){data={error:'HTTP '+r.status}}
 if(!r.ok&&!data.error)data.error='HTTP '+r.status;
 return data;
}

function srcMode(){return srcpaths.checked?'paths':(srcfolder.checked?'folder':'run');}
function syncSource(){
 const m=srcMode();
 pathswrap.hidden=m!=='paths';
 folderrow.style.display=m==='paths'?'none':'';
 recwrap.style.display=m==='folder'?'':'none';
 srcdirlabel.textContent=m==='run'
   ?'Downloader run folder (or the folder that holds it)':'Folder to scan';
}
[srcrun,srcfolder,srcpaths].forEach(r=>r.addEventListener('change',syncSource));

function fmtSize(n){
 if(n>=1048576)return (n/1048576).toFixed(1)+' MB';
 if(n>=1024)return Math.round(n/1024)+' KB';
 return n+' B';
}
function renderList(){
 const q=filter.value.trim().toLowerCase();
 list.textContent='';
 let shown=0;
 entries.forEach((e,i)=>{
  const hay=(e.rel+' '+e.format+' '+(e.title||'')).toLowerCase();
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
  meta.textContent=' '+e.format+' · '+fmtSize(e.size)+
   (e.title?' · '+e.title:'');
  div.append(cb,lab,meta);list.append(div);
 });
 if(shown>1500){const d=document.createElement('div');d.className='item meta';
  d.textContent='… list capped at 1500 rows — use the filter.';list.append(d);}
 updateCount();
}
function updateCount(){
 selcount.textContent=checked.size+' of '+entries.length+' file(s) checked.';
 syncGo();
}
filter.addEventListener('input',renderList);
selall.addEventListener('click',()=>{entries.forEach(e=>checked.add(e.path));renderList();});
selnone.addEventListener('click',()=>{checked.clear();renderList();});

async function refetchFiles(){
 const data=await api('/api/files');
 entries=data.entries||[];
 checked=new Set(entries.map(e=>e.path));
 pickwrap.hidden=entries.length===0;
 renderList();
}
async function doScan(){
 clearErr();   // a fixed problem takes its message down
 const m=srcMode();
 const body=m==='paths'?{mode:m,paths:pathbox.value}
   :{mode:m,folder:srcdir.value,recursive:recursive.checked};
 filesum.textContent='Scanning…';
 const data=await api('/api/scan',body);
 if(data.error){filesum.className='sum bad';filesum.textContent='Scan failed.';
  showErr(data.error);return;}
 if(data.bad&&data.bad.length)
  filesum.textContent='Note: '+data.bad.length+' path(s) not found.';
}
scanBtn.addEventListener('click',doScan);
scanpaths.addEventListener('click',doScan);

function syncSave(){
 const over=$('sover').checked;
 sufwrap.style.display=over?'none':'';
 overwarn.hidden=!over;
 $('outdirlabel').textContent=over
  ?'Report folder (files are replaced in place)'
  :'Output folder (report always saves here)';
}
$('scopy').addEventListener('change',syncSave);
$('sover').addEventListener('change',syncSave);

function syncAI(){
 const has=endpoint.value!=='';
 testep.disabled=!has;
 engai.disabled=!has;
 aialt.disabled=!has;
 airetries.disabled=!has;
 aiconc.disabled=!has;
 if(!has){englocal.checked=true;aialt.checked=false;}
}
endpoint.addEventListener('change',syncAI);

function renderEndpoints(){
 const cur=endpoint.value;
 endpoint.textContent='';
 const none=document.createElement('option');
 none.value='';none.textContent='None — local tools only';
 endpoint.append(none);
 aiCfg.endpoints.forEach(ep=>{
  const o=document.createElement('option');
  o.value=ep.name;o.textContent=ep.name+' ('+ep.kind+' · '+ep.model+')';
  endpoint.append(o);
 });
 endpoint.value=aiCfg.endpoints.some(e=>e.name===cur)?cur:
   (aiCfg['default']||'');
 syncAI();
}
/* Endpoints are owned by the shell now, so this module only re-reads them.
   The button exists because a key added in Settings while this page is open
   should not require a relaunch. */
refreshep.addEventListener('click',async()=>{
 const data=await api('/api/ai');
 if(data.error){showErr(data.error);return;}
 aiCfg=data.config;renderEndpoints();
 setMsg(epstatus,aiCfg.endpoints.length
  ?'Endpoint list refreshed.'
  :'Still no endpoints — add one in Settings › AI endpoints.',false);
});
testep.addEventListener('click',async()=>{
 epstatus.textContent='Testing '+endpoint.value+'…';
 const data=await api('/api/ai/test',{name:endpoint.value});
 if(data.error)setMsg(epstatus,'Test failed: '+data.error,true);
 else setMsg(epstatus,'Test OK — reply: '+data.reply,false,true);
});

function renderTools(t){
 tools=t||{};
 toolchips.textContent='';
 Object.keys(tools).forEach(k=>{
  const s=document.createElement('span');
  s.className='chip'+(tools[k]?'':' miss');
  s.textContent=k+(tools[k]?' ✓':' missing');
  toolchips.append(s);
 });
}
recheck.addEventListener('click',async()=>{
 const data=await api('/api/tools/check',{});
 if(data.tools)renderTools(data.tools);
});

function syncGo(){
 go.disabled=checked.size===0;
}
go.addEventListener('click',async()=>{
 clearErr();   // a fixed problem takes its message down
 const over=$('sover').checked;
 const mode=$('meval').checked?'evaluate':($('mforce').checked?'force':'auto');
 if(over&&mode!=='evaluate'&&
    !confirm('Overwrite mode: the checked files will be replaced in '+
             'place. Continue?'))return;
 const body={paths:Array.from(checked),mode:mode,
  save:over?'overwrite':'copy',out_dir:outdir.value,
  suffix:over?'':suffix.value.trim(),tag:tag.checked,deskew:deskew.checked,
  langs:langs.value.trim(),jobs:parseInt(jobs.value||'1',10),
  doc_lang:doclang.value.trim(),set_title:settitle.checked,
  image_out:$('iotext').checked?'text':($('ioboth').checked?'both':'pdf'),
  engine:engai.checked?'ai':'local',ai_alt:aialt.checked,
  ai_retries:parseInt(airetries.value||'4',10),
  ai_concurrency:parseInt(aiconc.value||'1',10),
  endpoint:endpoint.value,confirm_overwrite:over};
 const data=await api('/api/start',body);
 if(data.error)showErr(data.error);
});
pauseBtn.addEventListener('click',async()=>{
 await api('/api/pause',{paused:!paused});
});
stopBtn.addEventListener('click',async()=>{
 if(confirm('Stop the run? A partial report will be written.'))
  await api('/api/stop',{});
});

function esc(s){const d=document.createElement('span');d.textContent=s;return d.innerHTML;}
async function poll(){
 try{
  const s=await api('/api/state');
  const running=RUNNING.includes(s.phase);
  paused=!!s.paused;
  go.disabled=running||checked.size===0;
  pauseBtn.disabled=!running;
  stopBtn.disabled=!running;
  pauseBtn.textContent=paused?'Resume':'Pause';
  scanBtn.disabled=running;scanpaths.disabled=running;
  if(s.files&&s.files.version!==filesVersion){
   filesVersion=s.files.version;
   const f=s.files.formats||{};
   const parts=Object.keys(f).sort().map(k=>f[k]+' '+k);
   filesum.className='sum loaded';
   filesum.textContent=s.files.count+' file(s) queued from '+s.files.source+
    (parts.length?' — '+parts.join(', '):'');
   await refetchFiles();
  }
  if(s.tools&&Object.keys(s.tools).length&&!Object.keys(tools).length)
   renderTools(s.tools);
  status.className=s.phase==='done'?'done':(s.phase==='error'?'error':'');
  let msg='';
  if(s.phase==='idle')msg='Idle — scan a source to queue files.';
  else if(running){
   msg=(paused?'Paused — ':'')+
    (s.progress.msg||s.phase)+
    (s.progress.total?' ('+s.progress.current+'/'+s.progress.total+')':'');
  }
  else msg=s.summary||s.phase;
  status.innerHTML=esc(msg)+
   (s.phase==='done'&&s.last_file
    ?' — <a href="/download">Download the report</a>':'')+
   (s.phase==='error'&&s.last_file
    ?' — <a href="/download">Download the partial report</a>':'');
  if(s.progress.total&&running){
   prog.hidden=false;prog.max=s.progress.total;prog.value=s.progress.current;
  }else prog.hidden=true;
  renderLog(s.log); logStateSeen(s);
 }catch(e){/* transient poll errors are fine */}
 setTimeout(poll,1500);
}
(async function init(){
 syncSource();syncSave();
 const data=await api('/api/ai');
 if(data.config){aiCfg=data.config;renderEndpoints();}
 poll();
})();
</script>
</body></html>"""


def build_page(session: dict) -> bytes:
    brand = session.get("branding", {})
    colors = brand.get("colors", {})
    primary = colors.get("primary", FALLBACK_BRAND["primary"])
    html = (PAGE
            .replace("__NAME__", MANIFEST["name"])
            .replace("__SUITE__", brand.get("suite_name", "DC Admin Suite"))
            .replace("__INSTITUTION__", brand.get("institution", ""))
            .replace("__PRIMARY__", primary)
            .replace("__ACCENT__", colors.get("accent", FALLBACK_BRAND["accent"]))
            .replace("__ONPRIMARY__", contrast_text(primary))
            .replace("__HUB__", session.get("hub_url", "#"))
            .replace("__BASE_URL__", session.get("base_url", "")))
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# Local-request guard (v1.2 shell hardening) — the server binds to
# 127.0.0.1, but a malicious page could still reach it via DNS rebinding
# (bad Host) or a cross-site form/fetch (foreign Origin). Called FIRST in
# do_GET and do_POST.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# /api/start validation — pure function so it is unit-testable. Returns
# (error_message_or_"", cfg_dict_or_None, endpoint_or_None).
# ---------------------------------------------------------------------------
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]{0,24}$")
_LANGS_RE = re.compile(r"^[A-Za-z_+]{0,40}$")
_DOCLANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def validate_start(req, model, tools, ai_cfg):
    if model is None:
        return "Scan a source to queue files first.", None, None
    try:
        paths = [str(p) for p in req.get("paths", [])]
        cfg = {
            "mode": str(req.get("mode", "auto")),
            "save": str(req.get("save", "copy")),
            "out_dir": os.path.expanduser(str(req.get("out_dir",
                                                      "")).strip()),
            "suffix": str(req.get("suffix", "")).strip(),
            "tag": bool(req.get("tag", True)),
            "deskew": bool(req.get("deskew", True)),
            "langs": str(req.get("langs", "eng")).strip(),
            "jobs": int(req.get("jobs", 1)),
            "doc_lang": str(req.get("doc_lang", "en-US")).strip(),
            "set_title": bool(req.get("set_title", True)),
            "image_out": str(req.get("image_out", "pdf")),
            "engine": str(req.get("engine", "local")),
            "ai_alt": bool(req.get("ai_alt", False)),
            "ai_retries": int(req.get("ai_retries",
                                      AI_MAX_RETRIES)),
            "ai_concurrency": int(req.get("ai_concurrency",
                                          DEFAULT_AI_CONCURRENCY)),
            "endpoint": str(req.get("endpoint", "")).strip(),
            "confirm": bool(req.get("confirm_overwrite", False)),
        }
    except (TypeError, ValueError):
        return "Bad request.", None, None
    if cfg["mode"] not in ("evaluate", "auto", "force") \
            or cfg["save"] not in ("copy", "overwrite") \
            or cfg["image_out"] not in ("pdf", "text", "both") \
            or cfg["engine"] not in ("local", "ai"):
        return "Bad request.", None, None
    if not 0 <= cfg["ai_retries"] <= 10:
        return "AI retries must be between 0 and 10.", None, None
    if not 1 <= cfg["ai_concurrency"] <= MAX_AI_CONCURRENCY:
        return ("Concurrent AI requests must be between 1 and {}."
                .format(MAX_AI_CONCURRENCY)), None, None
    if not cfg["out_dir"] or not os.path.isdir(cfg["out_dir"]):
        return "Output folder does not exist: " + cfg["out_dir"], None, None
    if not _SUFFIX_RE.match(cfg["suffix"]):
        return ("Suffix may only use letters, digits, dot, dash, "
                "underscore (max 24)."), None, None
    if not _LANGS_RE.match(cfg["langs"]):
        return ("OCR languages must be Tesseract codes like eng or "
                "eng+deu."), None, None
    if cfg["doc_lang"] and not _DOCLANG_RE.match(cfg["doc_lang"]):
        return ("Document language must be a BCP-47 tag like en-US "
                "(or blank to skip)."), None, None
    if not 1 <= cfg["jobs"] <= 16:
        return "Parallel jobs must be between 1 and 16.", None, None
    by_path = {e["path"]: e for e in model}
    entries = [by_path[p] for p in paths if p in by_path]
    if not entries:
        return "Check at least one queued file.", None, None
    if len(entries) != len(paths):
        return ("The checked files no longer match the last scan — "
                "rescan and retry."), None, None
    missing = [e["path"] for e in entries if not os.path.isfile(e["path"])]
    if missing:
        return ("{} checked file(s) no longer exist on disk — rescan."
                .format(len(missing))), None, None
    cfg["entries"] = entries
    processing = cfg["mode"] != "evaluate"
    ep = None
    if cfg["endpoint"]:
        ep = find_endpoint(ai_cfg, cfg["endpoint"])
        if ep is None:
            return ("AI endpoint '{}' not found — save it under Manage "
                    "endpoints.".format(cfg["endpoint"])), None, None
        if not ep.get("api_key"):
            return ("AI endpoint '{}' has no API key.".format(
                cfg["endpoint"])), None, None
    if cfg["engine"] == "ai" or cfg["ai_alt"]:
        if ep is None:
            return ("Choose an AI endpoint to use the AI engine or alt-"
                    "text drafting."), None, None
    needs_local = processing and cfg["engine"] == "local" and any(
        e["format"] in ("pdf", "image") for e in entries)
    if needs_local and not local_ocr_ready(tools):
        miss = [k for k in ("ocrmypdf", "tesseract", "ghostscript")
                if not tools.get(k)]
        return ("Local OCR tools missing: {} — see section 5 for install "
                "commands (or switch to an AI endpoint / Evaluate only)."
                .format(", ".join(miss))), None, None
    if processing:
        office = any(e["format"] in ("docx", "pptx") for e in entries)
        if office and not (tools.get("python-docx")
                           and tools.get("python-pptx")):
            return ("python-docx / python-pptx are missing — see section "
                    "5 for the install command."), None, None
        if not tools.get("pikepdf") or not tools.get("pymupdf"):
            if any(e["format"] in ("pdf", "image") for e in entries):
                return ("pikepdf / pymupdf are missing — see section 5 "
                        "for the install command."), None, None
    if cfg["save"] == "overwrite" and processing and not cfg["confirm"]:
        return "Overwrite mode needs the confirmation prompt.", None, None
    if cfg["save"] == "overwrite":
        cfg["suffix"] = ""
    return "", cfg, ep


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
                return self._json({"error": "Forbidden (non-local request)."}, 403)
            if self.path == "/api/log":
                return send_run_log(self)
            if self.path == "/api/state":
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/api/files":
                with LOCK:
                    model = MODEL
                return self._json({"entries": model or []})
            if self.path == "/api/ai":
                return self._json({"config": load_ai_config()})
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
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
            if self.path == "/api/clear":
                with LOCK:
                    busy = STATE["phase"] in RUNNING_PHASES
                if busy:
                    return self._json({"error": CLEAR_REFUSED}, 409)
                clear_for_new_run()
                return self._json({"ok": True})
            if self.path == "/api/scan":
                if self._running():
                    return self._json({"error": "A job is running."}, 409)
                try:
                    req = self._body()
                    mode = str(req.get("mode", "run"))
                except (ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if mode not in ("run", "folder", "paths"):
                    return self._json({"error": "Bad request."}, 400)
                bad = []
                try:
                    if mode == "paths":
                        entries, bad = scan_paths(str(req.get("paths", "")))
                        source = "individual paths"
                        if not entries:
                            return self._json(
                                {"error": "No existing files in the "
                                          "pasted paths."}, 400)
                    else:
                        folder = os.path.expanduser(
                            str(req.get("folder", "")).strip())
                        if not folder or not os.path.isdir(folder):
                            return self._json(
                                {"error": "Folder does not exist: "
                                          + folder}, 400)
                        if mode == "run":
                            entries, run = scan_run_folder(folder)
                            source = os.path.basename(run)
                        else:
                            entries = scan_folder(
                                folder, bool(req.get("recursive", False)))
                            source = folder
                        if not entries:
                            return self._json(
                                {"error": "No files found in "
                                          + source + "."}, 400)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError as e:
                    return self._json({"error": "Scan failed: {}".format(
                        e)}, 400)
                set_model(entries, source)
                log("Queued {} file(s) from {}.".format(len(entries),
                                                        source))
                if len(entries) >= SCAN_MAX_FILES:
                    log("NOTE: scan capped at {} files.".format(
                        SCAN_MAX_FILES))
                return self._json({"ok": True, "count": len(entries),
                                   "bad": bad})

            if self.path == "/api/ai/test":
                try:
                    name = str(self._body().get("name", "")).strip()
                except (ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                ep = find_endpoint(load_ai_config(), name)
                if ep is None:
                    return self._json({"error": "Endpoint not found."}, 400)
                try:
                    reply, _usage = ai_generate(
                        ep, "Reply with the single word: OK", timeout=30,
                        stream=False)
                except AIError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "reply": reply[:80]})

            if self.path == "/api/tools/check":
                tools = tool_status()
                set_state(tools=tools)
                return self._json({"ok": True, "tools": tools})

            if self.path == "/api/start":
                if self._running():
                    return self._json({"error": "A job is already "
                                                "running."}, 409)
                try:
                    req = self._body()
                except (ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                with LOCK:
                    model = MODEL
                    tools = dict(STATE["tools"]) or tool_status()
                msg, cfg, ep = validate_start(req, model, tools,
                                              load_ai_config())
                if msg:
                    return self._json({"error": msg}, 400)
                with LOCK:
                    cfg["source"] = STATE["files"]["source"]
                if not start_run(process_worker, (session, cfg, ep),
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
    global CONFIG_PATH
    parser = argparse.ArgumentParser(description=MANIFEST["name"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--session", default=str(DEFAULT_SESSION))
    args = parser.parse_args()

    CONFIG_PATH = resolve_ai_config_path(
        load_session(Path(args.session)), args.session)
    session = load_session(Path(args.session))
    set_state(tools=tool_status())
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
