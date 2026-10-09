#!/usr/bin/env python3
"""Batch File Downloader v1.2 — DC Admin Suite module.

Downloads the files attached to the records of one or more Digital Commons
structures, scoped through the suite's hierarchy model (load a
DC_Hierarchy_Report_*.xlsx written by the Hierarchy Mapping Tool, or map
the hierarchy fresh; then all structures or checked parents + everything
beneath them — the same scoping as the Structure Regenerator and Batch
Revise Manager). Community structures hold no uploads and are excluded
automatically; chain-only intermediate parents (type "") likewise.

What can be downloaded, per the run options:
  · File kinds — primary files, native/original uploads, and/or
    supplemental ("Additional files")
  · Visibility — public files and/or hidden files (supplemental files whose
    "Show" box is unchecked, non-shown previous versions, and every file on
    a record that is not posted publicly)
  · Versions — current versions only, or ALL versions: every file-bearing
    revision on the record's "View revisions" page (window=pickvers),
    including originally-submitted native files (submitted .docx etc.) and
    journal submission cover letters
  · Records — posted records (Posted / Queued for update) and/or
    unposted records (Not yet posted / Rejected / Withdrawn)

Listings are read per record state directly (show_state=<state> /
showstate=<state>), so each record's state is known without heuristics.
Two listing flavors exist:
  · Gallery structures (Book, Gallery, i.e. ir_book / ir_gallery):
    /cgi/editor_gallery.cgi?context=CTX&show_state=S&search_field=abstract
    &search_op=eq&search_value=   — table#submission_list, "Next" paging.
  · Everything else (ETD, Journal, Series, Event) — the EdiKit editor:
    /cgi/editor.cgi?context=CTX&showstate=S&status_filter=all
    &x_field_1=abstract&x_op_1=eq&x_value_1=   — ID-headed table,
    "&gt;" (x_start) paging, plus an "Additional Files" count column that
    lets the module skip the additional_files page when there is nothing
    on it.
If a structure's type guessed the wrong flavor, the other one is tried
before giving up on that structure.

Item pages parsed (all under /cgi/editor.cgi, identical across types):
  · window=pickvers — table#revision: one row per revision, newest first;
    per-row PDF links (type=pdf&unstamped=yes&date=EPOCH&preview_mode=1),
    native links (type=native&…), cover-letter links (viewcoverletter.cgi),
    a checked "Select" radio marking the editor-selected (current) version,
    and a checked "Show" box marking previous versions that are public.
  · window=additional_files — table#uploaded-files: per-file link
    (viewcontent.cgi?filename=IDX&type=additional&preview_mode=1), original
    filename as the link text, checked show_N box = publicly shown.
The current primary file needs no page visit at all:
    /cgi/viewcontent.cgi?article=N&context=CTX&preview_mode=1
That URL asks for DC's DEFAULT content stream — the generated PDF. DC only
generates one from a Word, PowerPoint or PDF upload; a record holding
anything else (images, spreadsheets, datasets, audio, video, raw camera
files) has no derivative and answers it with a web page. So the fetch falls
back automatically to the native,
    /cgi/viewcontent.cgi?article=N&context=CTX&type=native&preview_mode=1
and reports the result as kind "native". No option needs checking for this;
the "Native / original files" option is for wanting the original AS WELL AS
the generated PDF. (DC also serves both through a path rewrite,
/context/CTX/article/N/type/native/viewcontent — same handler, same
response; content_link_type() reads either shape.)

Only ONE driver navigation is made per structure (its first listing page —
which also validates the login and refreshes cookies); every further
listing page, item page, and file is fetched with urllib using the
attached debug Chrome's cookies, so files land in the chosen folder (never
Chrome's download directory) and long runs stay fast.

Every downloaded file is renamed
    <context>_<article>_<kind>_<visibility>_<version>_<original-name>.<ext>
      kind        primary | native | supp<N> | coverletter
      visibility  public | hidden
      version     current | original | rev<N>   (chronological)
and sorted into one subfolder per structure beneath the run folder
DC_FileDownloads_<stamp>/. Each structure subfolder also receives a rich,
filterable download report (DC_FileDownload_Report_<context>_<stamp>.xlsx,
one row per download) and the run root receives a master log
(DC_FileDownload_MasterLog_<stamp>.xlsx, one row per structure). Stop or a
mid-run login redirect still writes the partial reports (_PARTIAL suffix).

Run standalone:  python3 modules/dc_file_downloader.py --port 8799 \
                     --session config/session.json
Or launch it from the Main Menu.

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
    "id": "file-downloader",
    "name": "Batch File Downloader",
    "description": "Download primary, native, supplemental, hidden, and versioned files for selected structures, with per-structure reports.",
    "version": "1.35.0",
    "requires": ["selenium", "beautifulsoup4", "openpyxl"],
}

import argparse
import glob
import json
import os
import random
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from email.utils import parsedate_to_datetime
from html import escape as _html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------
DEFAULT_SESSION = Path(__file__).resolve().parent.parent / "config" / "session.json"

HIER_GLOB = "DC_Hierarchy_Report_*.xlsx"     # written by the Hierarchy Mapping Tool

# Pacing. LISTING_SETTLE = after the one driver.get per structure.
# REQUEST_DELAY = polite gap between urllib requests (listing pages, item
# pages, files). Watch-item: bump if pages come back incomplete.
LISTING_SETTLE = 1.5
# Measured, not guessed. At 0.3s this module met a Digital Commons lockout
# after 26-36 records on four separate attempts against the same structure;
# at 5.0s it fetched all 40 rows with no pushback at all, 78 downloads in
# 687 seconds - one every 8.8s, which sits inside the 8.3-11.5s ceiling
# independently inferred from the four failures. The lockout is a rate
# limit, so the burst was the cause and not the volume.
#
# 5.0s is therefore the shipped default: an eleven-minute run that finishes
# beats a two-minute run that stalls for ten, and a staff member should
# never meet a lockout they have no way to understand. Anyone in a hurry
# lowers the field on the page for that run.
REQUEST_DELAY = 5.0                # default; the run may set its own
# Admin pages are paced separately from downloads, because Digital Commons
# counts downloads and does not appear to count admin pages.
#
# The evidence is the Hierarchy Mapping Tool, which sleeps 1.0s between
# admin page loads: in one measured run it made ~1,152 of them in 28 minutes with
# no lockout, and a download run started two minutes later fetched 37 of 38
# files unpenalised. Had admin traffic shared the download budget, that map
# would have exhausted it many times over.
#
# So 1.0s is not a guess — it is the figure a run of 1,152 consecutive
# requests already proved. It is a DEFAULT, not a constant, and it is a
# control on the page: this is one natural experiment, and it shows admin
# pages did not consume the download budget on that occasion rather than
# that they are free when interleaved with downloads. There may be a page
# rate limit nobody has reached because 1.0s was under it.
#
# What makes it safe to find out: since v1.10 a lockout is waited out and
# every pending request shares one expiry, and since v1.20/v1.25 a run
# records what it never reached and resumes from its own report. Being
# wrong costs ten minutes, not data.
PAGE_DELAY = 1.0                   # default gap before an ADMIN PAGE request
REQUEST_DELAY_MAX = 60.0           # ceiling on the per-run setting
PAGE_TIMEOUT = 60
# A listing asked for with x_showall=1 is not an ordinary admin page: the
# ETD collection answers with 3,383 rows in about 4.8 MB. 60s is right for
# a normal page and wrong for that one.
LISTING_TIMEOUT = 240
FILE_TIMEOUT = 300
# How long Selenium may wait on ONE chromedriver command. Selenium's own
# default is 120s and nothing was setting it, which is what ended Run E's
# first attempt: "ReadTimeoutError: HTTPConnectionPool(host='localhost',
# port=56920) ... read timeout=120" is chromedriver, not Digital Commons.
DRIVER_COMMAND_TIMEOUT = 300
MAX_LISTING_PAGES = 400            # hard cap per state crawl (safety)
LISTING_PAGE_SIZE = 25             # rows Digital Commons serves per page

# Rate limiting. Digital Commons throttles a sustained run: in one run
# a 38-record gallery ran clean for 25 records and then answered HTTP 429
# for the whole tail, and because a 429 fell through the native-fallback
# path those twelve records were reported as having no downloadable file.
# They have files. So 429 and 503 are retried with backoff, and a 429 that
# survives the retries is its own failure, never an absence verdict.
# DELAY_STEP is added to REQUEST_DELAY for the rest of the run each time a
# 429 is seen, so a throttled run slows down instead of failing its tail.
RATE_LIMIT_CODES = (429, 503)
# A 3xx that reaches us as an exception is urllib refusing to follow the
# server any further - a redirect loop. Digital Commons has been seen doing
# this to a sustained run at the same point where it otherwise answers 429,
# so it is the same pushback in different clothing and is retried the same
# way. It is emphatically not a statement that the file is missing.
REDIRECT_CODES = (301, 302, 303, 307, 308)
# The server ANSWERING that there is no such file. A 404 is not a failure
# and not pushback: it is a fact about the record, and the only reason it
# has not been usable until now is that the browser path cannot hear it.
# Measured on two records reported `Failed:` by two runs from outside the
# institution's network: both natives answered HTTP 404, both derivatives answered 200 with
# the PDF, over the same cookie and the same four minutes.
ABSENT_CODES = (404, 410)
# What the server ASKS for, via Retry-After, is honored up to this. Beyond
# it there is no point retrying: in one run DC asked for longer than the
# 20s backoff cap, the cap silently overrode it, and three retries were
# burned against a wall whose height the server had already stated. A wait
# longer than this fails the row immediately, naming the number, so the
# operator learns the real figure instead of a guess.
RATE_HONOR_CAP = 120.0
# A cooldown is a lockout with an ABSOLUTE expiry, which is what the
# measured evidence showed: ten consecutive refusals reported 600, 598,
# 595 ... 563 seconds, and `time + Retry-After` was the same instant to
# within a second in every one. The wall does not move, so retrying inside
# the window is provably futile - but every pending request shares the one
# expiry, so waiting it out ONCE clears the whole backlog. On that run a
# single 10-minute wait would have turned an incomplete 30-of-40 run into a
# complete one. Hence: wait it out, a bounded number of times.
COOLDOWN_MAX_WAITS = 3             # per run; 0 disables waiting entirely
COOLDOWN_HARD_MAX = 20             # no caller may ask for more than this
COOLDOWN_MAX_SECONDS = 900.0       # never sit out longer than this at once
COOLDOWN_TICK = 30.0               # seconds between "still waiting" notes
COOLDOWN_GRACE = 5.0               # resume a little past the stated expiry
RATE_RETRIES = 3                   # attempts after the first refusal
RATE_BACKOFF_BASE = 1.5            # seconds; doubles each attempt
RATE_BACKOFF_CAP = 20.0            # never sleep longer than this per wait
RATE_DELAY_STEP = 0.4              # permanent slowdown per refusal seen
RATE_DELAY_CAP = 3.0               # ceiling on the accumulated slowdown

# ---------------------------------------------------------------------------
# The fetch path, and the verification prompt
#
# Files are fetched by navigating the attached Chrome. Admin pages are not:
# they are never challenged, and the timeout and retry control that matters
# lives in _open(). This is a deliberate exception to "keep the browser out
# of the data path" — a rule written from a measurement, in which two runs
# died at exactly 120s driving ADMIN PAGES through DevTools. A download's
# bytes move through Chrome's own network stack rather than the DevTools
# channel, so that failure mode may not apply here. *May*: it has not been
# measured at collection scale, which is why FETCH_NETWORK still exists and
# is selectable on the page rather than requiring a source edit.
#
# The reason the browser path exists at all is that the repository is behind
# bot management that periodically asks the client to confirm a person is
# present. A browser shows that question to whoever is at it and they answer
# it in seconds; urllib cannot answer it at all and sees an indefinite
# refusal. Being a browser is not pretending to be one: nothing here spoofs
# an agent, forwards a clearance cookie, patches a driver or answers a
# challenge programmatically. When the question is asked, the run stops and
# a person answers it.
FETCH_BROWSER = "browser"
FETCH_NETWORK = "network"
FETCH_PATHS = (FETCH_BROWSER, FETCH_NETWORK)

# What the tab shows, and what it means. There are no status codes on this
# path — that is its real cost — so absence, refusal and challenge have to
# be told apart from the page itself. Matched against title + source,
# lowercased.
CHALLENGE_MARKERS = ("just a moment", "challenges.cloudflare.com",
                     "enable javascript and cookies", "verify you are human",
                     "checking your browser")
# A definite absence: the server answered, and the answer was that there is
# no file. Usually a record whose upload was never a PDF.
ABSENT_MARKERS = ("no pdf has been provided",
                  # v1.34 — captured from the live page by
                  # the native-absence probe, not paraphrased: a native
                  # URL with no file lands on the CGI form of itself,
                  # whose TITLE and BODY word it differently. Both
                  # paraphrases in the record had one of them right.
                  "sorry, that file does not exist",
                  "sorry, that file doesn't exist")

# A THIRD way the server was believed to say the same thing — and the
# belief was wrong. From v1.33.3 to v1.33.8 this module recorded
# that "a content URL with no file behind it redirects to the repository's
# own home page". It does not. That was never measured; it was a log line
# misread, twice, a week apart.
#
# What the module actually printed was `landed on: <the home page>`, which
# reports WHERE THE TAB IS, not where the request went. A navigation that
# never commits leaves the tab wherever it already was, and after the
# first such request that is always the home page. Probed directly
# against a record that had failed this way in two runs: the
# navigation timed out after 15s, no page rendered, the document identity
# was byte-for-byte unchanged, and nothing arrived. The server had in fact
# answered HTTP 404 — which the browser cannot hear at all.
#
# redirected_away() is KEPT, because a redirect is still a thing servers
# do and the guards around it are sound. It is no longer claimed as a
# measured behavior of this one, and it is no longer the answer to the
# case it was written for. That answer is ask_the_network(), below.
#
# The comparison is structural rather than by what the page says, because
# what it says is an institution's name, and an institution's name in
# source is what the identity gate exists to catch. Comparing the landed
# path to the requested one is true of any repository.
#
# Treated as a definite absence on the same rule v1.20 settled: a web page
# where a file was expected is the server ANSWERING. Two guards keep that
# honest — nothing may have begun arriving (a slow transfer is not an
# absence), and the page must not be a challenge or a login bounce, both
# of which are checked before this.

# How long to wait for a person, once the question has been asked. Five
# minutes, and editable. Thirty seconds was considered and rejected: the
# guarantee that matters is that the run stops and does not resume until
# somebody clears it, and while it waits it is idle and costs nothing. The
# timeout only governs when to tidy up and write the reports, so it is
# chosen for usability rather than for safety.
VERIFY_WINDOW = 900.0              # seconds; 0 means do not wait at all.
                                   # v1.34: was 300. Staff take required
                                   # 15-minute breaks, and a prompt that
                                   # arrives during one must not end the
                                   # run. The run is idle while it waits.
VERIFY_WINDOW_MAX = 3600.0         # no caller may ask for longer
VERIFY_POLL = 2.0                  # seconds between passive checks
VERIFY_TICK = 60.0                 # seconds between "still waiting" notes
                                   # (v1.34: was 30; at a 15-minute window
                                   # that was 30 lines a hold)
# Holds spent on ONE file before giving up on the run. A challenge that a
# present person clears and that returns immediately on the same file is
# not something to keep answering; per the standing rule, the answer to
# being challenged in a way a person cannot clear is the vendor
# conversation, not a better disguise. Bounded here so the retry loop
# cannot spin on a server that challenges every request.
VERIFY_MAX_HOLDS = 3

# Directory watching, which is how a download is known to have finished.
# Chrome writes <final>.crdownload while transferring and renames it on
# completion, so the TRANSITION is the signal. "A new file appeared" is not:
# a probe once counted a .DS_Store that Finder wrote as a 0.01 MB
# download taking 39.9 seconds. Dotfiles are excluded everywhere.
#
# Chrome's own download events may be reachable and would make completion
# authoritative rather than inferred, but only the *attribute* has been
# checked (Browser.setDownloadBehavior accepts eventsEnabled, and
# bidi_connection is present) — not a working listener. DownloadWatch is an
# interface so that proving it later replaces one object and touches
# neither worker. Until then this is what ships, and it is what ran 289
# files without a miscount.
BROWSER_START_WITHIN = 8.0         # nothing began arriving → it is not coming
# How often the tab is glanced at WHILE waiting for a file. The first live
# run (v1.33) met a verification prompt, the operator cleared it in
# about two seconds, the file arrived normally, and the run reported that
# no verification had been requested — because the tab was only ever read
# AFTER the wait gave up. A prompt answered quickly is still a prompt, and
# an audit trail that omits the ones handled well is worth very little.
# This is a title-only read: one cheap DevTools round trip.
BROWSER_OBSERVE = 0.5
# v1.34: there is NO total ceiling on a transfer. A fixed 300 s gave up on
# files that were still arriving — a 3.05 GB video took about three minutes
# in one run, and this module now has to expect 10 GB — and the partial
# was then thrown away, so the same row failed on every re-run. Patience is
# measured by PROGRESS instead: a transfer that keeps growing is waited
# for, and one that stops growing for BROWSER_STALL_LIMIT is given up.
# Stop is the ceiling, and the operator is present.
BROWSER_SETTLE_TIMEOUT = None
BROWSER_STALL_LIMIT = 120.0        # A GUESS, stated as one. Editable on the
                                   # page; revise from the stall lines the
                                   # log now records.
BROWSER_STALL_LIMIT_MAX = 3600.0
PROGRESS_TICK = 60.0               # one "still waiting" line a minute —
                                   # for holds and transfers alike
BROWSER_POLL = 0.3                 # seconds between folder reads
# How long a .crdownload must sit at the same size before it counts as
# stranded rather than in flight. Measured need (v1.33.4): a video was
# deleted mid-transfer because "is there a partial?" was the only question
# asked.
PARTIAL_STILL_FOR = 1.5
BROWSER_PAGE_TIMEOUT = 15          # a download completes no navigation
# v1.34.2. A NATIVE gets longer, because a missing native is the server's
# slowest answer and the module's commonest. MEASURED twice: a browser
# network capture (native URL -> 302 in 93 ms -> the CGI form ->
# 404 after 15,210 ms, load event at 15.58 s) and a curl
# (15.48 s). At 15.0 s chromedriver stopped the load just before the
# "Sorry" page arrived, so the tab never said anything about the request
# and every missing native ended "not confirmed" from outside the
# institution's network, ~28 s each.
# 30 s is about twice the measured answer. It costs a successful download
# nothing: a download completes no navigation and get() returns as the
# file starts (one measured run: downloads median 5-7 s a row against a
# 5.0 s gap). Only a page that RENDERS slowly waits for it.
BROWSER_NATIVE_PAGE_TIMEOUT = 30
BROWSER_PAGE_TIMEOUT_DEFAULT = 300 # Selenium's own default, restored on close
# Chrome downloads into one folder, so the module gives it a private one
# inside the run folder and moves each file to its real name from there.
# That keeps the user's own Downloads folder untouched, makes a stranded
# partial unambiguous (nothing else writes here), and makes the move a
# rename rather than a copy, since it shares a filesystem with the
# destination.
INCOMING_DIRNAME = "_incoming"

# Record states, keyed by the listing query value. Posted / Queued for
# update are live on the web; the rest are not.
POSTED_STATES = ("published", "queued_for_update")
UNPOSTED_STATES = ("pending", "rejected", "withdrawn")
STATE_LABELS = {
    "published": "Posted",
    "queued_for_update": "Queued for update",
    "pending": "Not yet posted",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}

# Structures that hold no uploads are excluded from every run.
# Label → state code, for reading a Record State back out of a report. The
# labels are what a human sees, so they are what the report carries; the
# codes are what the listing crawler and POSTED_STATES speak.
STATE_BY_LABEL = {label: code for (code, label) in STATE_LABELS.items()}

EXCLUDED_TYPE_LABELS = ("Community",)
# Types managed by the gallery editor (editor_gallery.cgi).
GALLERY_TYPE_LABELS = ("Book", "Gallery")


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


def edit_group_url(base_url: str, ctx: str) -> str:
    return "{0}/cgi/user_config.cgi?context={1}&window=edit_group".format(
        base_url, ctx)


def gallery_listing_url(base_url: str, ctx: str, state: str) -> str:
    """First page of a gallery structure's Manage Items listing."""
    return ("{0}/cgi/editor_gallery.cgi?context={1}&show_state={2}"
            "&search_field=abstract&search_op=eq&search_value="
            .format(base_url, ctx, state))


def edikit_listing_url(base_url: str, ctx: str, state: str,
                       showall: bool = True) -> str:
    """First page of an EdiKit structure's Manage Submissions listing.

    `x_showall=1` asks for every record on one page instead of 25 at a
    time. That matters enormously on a large structure: the ETD collection
    holds 3,383 records, which is ~136 paged requests before a single file
    is fetched - and at Digital Commons' rate (a ten-minute lockout every
    ~31 requests) the listing crawl alone would spend four lockouts and
    about forty-five minutes achieving nothing visible.

    UNVERIFIED against the live server as of 2026-09-08, and safe either
    way: if DC ignores the parameter the page comes back with 25 rows and
    its ">" link, and crawl_listing pages on exactly as before. The
    fallback is the current behavior, which is why this is worth trying
    without having proved it first. crawl_listing logs the page count so
    the answer shows up in the next run's log rather than being assumed.
    """
    return ("{0}/cgi/editor.cgi?context={1}&showstate={2}&status_filter=all"
            "{3}&x_field_1=abstract&x_op_1=eq&x_value_1="
            .format(base_url, ctx, state,
                    "&x_showall=1" if showall else ""))


def primary_file_url(base_url: str, ctx: str, article: str) -> str:
    """The record's current primary file (any record state; admin session).

    This asks for DC's DEFAULT content stream, which is the generated PDF
    derivative. DC only generates one from a Word, PowerPoint or PDF upload;
    for anything else (images, spreadsheets, CSV, ZIP, audio, video, raw
    camera files) there is no derivative and this URL answers with a web
    page. That is not an error — it means "ask for the native instead", which
    is what fetch_record_file() does with native_file_url() below.
    """
    return ("{0}/cgi/viewcontent.cgi?article={1}&context={2}&preview_mode=1"
            .format(base_url, article, ctx))


def native_file_url(base_url: str, ctx: str, article: str) -> str:
    """The record's current NATIVE file — the bytes as originally uploaded.

    THE SHAPE OF THIS URL IS THE OPERATIVE PART, and v1.2/v1.3 had it wrong.

    Until 2026-09-07 this built the query form,
        /cgi/viewcontent.cgi?article=N&context=CTX&type=native&preview_mode=1
    on the stated assumption that DC's path rewrite was "the same handler and
    the same response". It is not. Checked against a live record
    (photo-gallery25 article 1000, a native-only image):

        /context/CTX/article/N/type/native/viewcontent   -> the image bytes
        /cgi/viewcontent.cgi?...&type=native&preview_mode=1
                                                         -> "No PDF has been
                                                            provided." (HTML)
        /cgi/viewcontent.cgi?...&type=native             -> the same HTML

    The query form silently ignores `type=native` and answers as though the
    derivative had been asked for. So `preview_mode` was never the issue: the
    native is reachable ONLY through the path form. Every native-only record
    therefore failed the derivative fetch, failed the "fallback", and was
    reported as "no downloadable file (no derivative and no native)" — the
    exact symptom v1.2 was written to fix, with the fix pointed at a URL the
    server does not honor. A test asserted the broken shape, which is how it
    survived a green suite.
    """
    return _native_path_url(base_url, ctx, article)


def _native_path_url(base_url: str, ctx: str, article: str) -> str:
    """The one URL shape that serves a native, with preview_mode.

    `preview_mode=1` is what lets primary_file_url() reach a record in ANY
    state rather than only a posted one, so the native needs it for the same
    reason — an unposted record's native is otherwise as unreachable as its
    derivative was. The path form accepts the query parameter and still
    returns the bytes (checked live, 2026-09-07), so carrying it costs
    nothing and covers the states a run actually includes.
    """
    return ("{0}/context/{1}/article/{2}/type/native/viewcontent"
            "?preview_mode=1".format(base_url.rstrip("/"), ctx, article))


def native_variant(url: str) -> str:
    """The type=native form of an arbitrary DC content URL.

    Used for links parsed off a page, where the context/article are not
    separately in hand. Returns "" when the URL already names a type (there
    is nothing to fall back to) or does not look like a content URL.
    """
    parts = urlparse(url)
    if content_link_type(url):
        return ""
    if "viewcontent" not in parts.path:
        return ""
    origin = ("{0}://{1}".format(parts.scheme, parts.netloc)
              if parts.scheme and parts.netloc else "")
    if parts.path.endswith("/viewcontent"):      # already the path form
        stem = parts.path[:-len("/viewcontent")]
        return "{0}{1}/type/native/viewcontent?preview_mode=1".format(
            origin, stem)
    # Query form. Appending &type=native does NOT work: the CGI ignores it
    # and answers with the "No PDF has been provided" page (see
    # native_file_url). The native is only reachable through the path form,
    # so rebuild it from the article and context the query already carries.
    q = parse_qs(parts.query, keep_blank_values=True)
    article = (q.get("article") or [""])[0].strip()
    ctx = (q.get("context") or [""])[0].strip()
    if article and ctx:
        return _native_path_url(origin, ctx, article)
    # Without both, no path-form URL can be built — and returning the query
    # form would spend a request to be told "No PDF has been provided" and
    # then report a misleading failure. Say there is no fallback instead.
    return ""


def pickvers_url(base_url: str, ctx: str, article: str) -> str:
    return ("{0}/cgi/editor.cgi?article={1}&window=pickvers&context={2}"
            .format(base_url, article, ctx))


def additional_files_url(base_url: str, ctx: str, article: str) -> str:
    return ("{0}/cgi/editor.cgi?article={1}&window=additional_files"
            "&context={2}".format(base_url, article, ctx))


def admin_record_url(base_url: str, ctx: str, article: str,
                     gallery: bool) -> str:
    """The item's admin view (gallery details page / EdiKit abstract page)."""
    if gallery:
        return ("{0}/cgi/editor_gallery.cgi?display=submission&article={1}"
                "&context={2}".format(base_url, article, ctx))
    return ("{0}/cgi/editor.cgi?window=abstract&article={1}&context={2}"
            .format(base_url, article, ctx))


def preview_record_url(base_url: str, ctx: str, article: str) -> str:
    """Admin preview of the record's public view (works in every state)."""
    return ("{0}/cgi/preview_article.cgi?article={1}&context={2}"
            .format(base_url, article, ctx))


def root_label(base_url: str) -> str:
    """The platform root as it appears in Level1, e.g. the bare domain."""
    return urlparse(base_url).netloc or base_url


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

    # Selenium's client-side wait on a single chromedriver command defaults
    # to 120s, and nothing here was raising it. A slow page therefore ended
    # the run with a raw ReadTimeoutError naming a localhost port, which
    # reads like a broken driver rather than a big page. Set on the class
    # because that is the lever every Selenium 4 exposes; guarded because
    # it is not part of the documented API and an upgrade may move it.
    # Raise Selenium's client-side wait on ONE chromedriver command. Its
    # default is 120s and nothing here was raising it.
    #
    # v1.28 attempted this and wrapped it in a bare `except: pass`, so when
    # it did not take effect nothing said so — and two runs then
    # failed at exactly 120.0s, which is precisely the tell that the raise
    # had not happened. A guard that quietly does nothing turns a bug into
    # a mystery. It reports now, and the caller logs it.
    set_via = ""
    try:
        from selenium.webdriver.remote.remote_connection import (
            RemoteConnection)
        RemoteConnection.set_timeout(DRIVER_COMMAND_TIMEOUT)
        set_via = "RemoteConnection.set_timeout"
    except Exception as e:                # noqa: BLE001 - reported below
        # Not "FAILED": this is one of several levers, and the ones below
        # usually take. v1.34 — the word in front of a success was the
        # kind of message that gets acted on wrongly.
        set_via = "class-level lever unavailable ({})".format(
            e.__class__.__name__)

    driver = webdriver.Chrome(options=opts)

    # Belt and braces: whatever the class-level lever did, the live
    # connection object may carry its own timeout. Set that too, and
    # report what the driver actually ends up with rather than what we
    # asked for.
    # Selenium moves this lever between versions, so try every one that
    # exists on the live connection and then REPORT what it ends up at.
    #
    # v1.29 got this half right and the report proved it: on the operator's
    # Selenium the class-level setter raises
    #   AttributeError: 'NoneType' object has no attribute 'timeout'
    # because RemoteConnection._client_config is None until an instance
    # exists. v1.29 then set `_client_config.timeout` on the instance —
    # guarded on not-None, so it silently did nothing — and READ
    # `_timeout` without ever setting it. The connection duly went on
    # reporting 120s, which is what the log said, honestly.
    levers = []
    actual = None
    try:
        conn = driver.command_executor
        # The instance attribute urllib3 actually reads in Selenium 4.x.
        try:
            conn._timeout = DRIVER_COMMAND_TIMEOUT
            levers.append("_timeout")
        except Exception as e:            # noqa: BLE001
            levers.append("_timeout unavailable ({})".format(
                e.__class__.__name__))
        # Newer builds route it through a ClientConfig instead.
        cfg = getattr(conn, "_client_config", None)
        if cfg is not None:
            try:
                cfg.timeout = DRIVER_COMMAND_TIMEOUT
                levers.append("_client_config.timeout")
            except Exception as e:        # noqa: BLE001
                levers.append("_client_config unavailable ({})"
                              .format(e.__class__.__name__))
        if hasattr(conn, "set_timeout"):
            try:
                conn.set_timeout(DRIVER_COMMAND_TIMEOUT)
                levers.append("conn.set_timeout")
            except Exception as e:        # noqa: BLE001
                levers.append("conn.set_timeout unavailable ({})"
                              .format(e.__class__.__name__))
        # Read back whatever the connection now believes, preferring the
        # value a request would actually use.
        actual = getattr(conn, "_timeout", None)
        cfg = getattr(conn, "_client_config", None)
        if cfg is not None and getattr(cfg, "timeout", None) is not None:
            actual = cfg.timeout if actual is None else min(
                actual, cfg.timeout)
    except Exception as e:                # noqa: BLE001 - reported
        set_via += " | executor: {}: {}".format(e.__class__.__name__, e)
    if levers:
        set_via += " | instance: " + ", ".join(levers)

    try:
        driver.set_page_load_timeout(DRIVER_COMMAND_TIMEOUT - 30)
        page_set = True
    except Exception as e:                # noqa: BLE001 - reported
        page_set = "NOT set ({})".format(e.__class__.__name__)

    driver._dc_timeout_report = timeout_report(
        actual, DRIVER_COMMAND_TIMEOUT, set_via, page_set)
    return driver


def timeout_report(actual, asked, levers: str, page_set) -> str:
    """The Chrome bridge line, verdict first.

    Several levers are tried because Selenium moves them between versions,
    and on any one Selenium some are unavailable. That is normal and is not
    the verdict: the verdict is what the connection ends up reporting. So
    the line opens with whether the command timeout is what was asked, and
    lists the levers after it. 1.0.0 printed "asked 300s via FAILED (...)"
    in front of a connection that had its 300 s - the word in front of a
    success was the kind of message that gets acted on wrongly."""
    try:
        got = float(actual) if actual else None
    except (TypeError, ValueError):
        got = None
    if got is None:
        verdict = ("command timeout: could not be read back (asked {}s)"
                   .format(asked))
    elif got >= asked:
        verdict = "command timeout {:g}s, as asked".format(got)
    else:
        verdict = ("command timeout is {:g}s, NOT the {}s asked - a long "
                   "page may end the run at {:g}s".format(got, asked, got))
    page = ("page-load timeout set" if page_set is True
            else "page-load timeout " + str(page_set))
    return "{}; {} (levers: {})".format(verdict, page, levers or "none")


# ---------------------------------------------------------------------------
# HTML parsing (listing/edit_group helpers duplicated from the Hierarchy
# Mapping Tool by design — modules stay self-contained).
# ---------------------------------------------------------------------------
# DC writes content links in two interchangeable shapes. Everything that
# reads a `type` (or `filename`) off a link goes through the two helpers
# below, so a parser never cares which shape it was handed:
#     query form  /cgi/viewcontent.cgi?article=N&context=CTX&type=native
#     path form   /context/CTX/article/N/type/native/viewcontent
_TYPE_PATH_RE = re.compile(r"/type/([A-Za-z0-9_-]+)(?:/|$)")
_FILENAME_PATH_RE = re.compile(r"/filename/([A-Za-z0-9_.-]+)(?:/|$)")


def content_link_type(href: str) -> str:
    """The DC content type named by a viewcontent link, in either form.

    Returns "" for a link that names no type at all — a bare link to the
    row's current content. Callers treat that as the primary content and let
    the native fallback in fetch_record_file() work out what the server
    actually has; they must NOT discard it, which is what v1.1 did and is
    why native-only records downloaded nothing in all-versions mode.
    """
    parts = urlparse(href or "")
    from_query = (parse_qs(parts.query, keep_blank_values=True).get("type")
                  or [""])[0].strip().lower()
    if from_query:
        return from_query
    m = _TYPE_PATH_RE.search(parts.path)
    return m.group(1).lower() if m else ""


def content_link_filename(href: str) -> str:
    """The supplemental-file index named by a viewcontent link, either form.

    Returns "" when the link names none. The index is reported, never used
    to build a URL, so an empty one must not discard the file.
    """
    parts = urlparse(href or "")
    from_query = (parse_qs(parts.query, keep_blank_values=True)
                  .get("filename") or [""])[0].strip()
    if from_query:
        return from_query
    m = _FILENAME_PATH_RE.search(parts.path)
    return m.group(1) if m else ""


def _soup(html: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def find_cttypes_table(html: str):
    """Return the <table class="cttypes"> captioned 'Contents of this site'."""
    for tbl in _soup(html).find_all("table", class_="cttypes"):
        cap = tbl.find("caption")
        if cap and "contents of this site" in cap.get_text(strip=True).lower():
            return tbl
    return None


def session_still_open(driver, base_url, log=None):
    """Look at the site page both workers check before starting.

    Returns (html, bounced). ONE function, because two copies of a check
    drift and these two already had: both navigated to the site root and
    read it the same way, but only one said so in the log — and on
    2026-09-22 the missing line was the first sign that a re-run had not
    checked anything at all. It had; it just could not say so.

    **This cannot establish that the ADMIN session is alive.** The site
    root is the repository's public home page: an anonymous visitor is
    served it in full, so it reads identically logged in and logged out.
    It catches an installation that bounces everything to a login, and
    nothing subtler. The real detector is the listing crawl, which reads
    an admin page and raises LoginRequired — see crawl_listing.

    Said plainly here because the comment this replaces claimed "a login
    bounce still shows here", and a check believed to be load-bearing
    when it is not is worse than no check.
    """
    if log:
        log("Checking the site page (this catches a site-wide login "
            "bounce, not an expired admin session)…")
    t0 = time.time()
    driver.get(base_url + "/")
    time.sleep(LISTING_SETTLE)
    html = driver.page_source
    if log:
        log("Site page read in {:.1f}s.".format(time.time() - t0))
    return html, looks_like_login_page(html, driver.current_url)


def looks_like_login_page(html: str, current_url: str) -> bool:
    """Heuristic: did DC bounce us to a login/account page?"""
    if "myaccount.cgi" in current_url or "/login" in current_url:
        return True
    lowered = html.lower()
    return 'name="password"' in lowered and 'name="login"' in lowered


def extract_contexts(html: str):
    """Return {context: type_label} from the site-wide listing page, or None
    if the 'Contents of this site' table is missing."""
    table = find_cttypes_table(html)
    if table is None:
        return None

    found = {}
    for tr in table.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        code = th.get_text(strip=True)
        label = CODE_TO_LABEL.get(code, code)
        for a in td.find_all("a", href=True):
            qs = parse_qs(urlparse(a["href"]).query)
            for ctx in qs.get("context", []):
                ctx = ctx.strip()
                if ctx and "/" not in ctx and not ctx.startswith("http"):
                    found.setdefault(ctx, label)
    return found


def extract_parent(html: str, base_url: str):
    """Read <input name="x_group"> from an edit_group page → parent slug,
    "" for a direct child of the platform, None if the input is missing."""
    inp = _soup(html).find("input", attrs={"name": "x_group"})
    if inp is None:
        return None
    return normalize_parent(inp.get("value") or "", base_url)


def normalize_parent(value: str, base_url: str) -> str:
    """Reduce an x_group value (usually a full URL) to a context slug."""
    v = value.strip()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    domain = urlparse(base_url).netloc
    if domain and v.startswith(domain):
        v = v[len(domain):]
    return v.strip("/")


def build_chain(child: str, parents: dict, root: str):
    """Return the path [root, ..., parent, child] for one structure."""
    chain = []
    cur, seen = child, set()
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = parents.get(cur, "")
    chain.append(root)
    chain = list(dict.fromkeys(chain))   # dedupe, preserve order
    chain.reverse()                      # root → … → child
    return chain


# ---------------------------------------------------------------------------
# Manage-Items listing parsing — both flavors return the same shape:
#   {"rows": [{"article","title","last_event","submitted","af"}, ...],
#    "next": next-page href or None}
# or None when the page holds no recognizable listing table (wrong flavor,
# login page, or an unexpected layout). "af" is the Additional Files count
# (int) when the listing exposes one, else None.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Access state — metadata, not a record state, and not the same thing as
# visibility.
#
# `visibility` in this module answers "is this file shown on the site", and
# is derived from the record's posted state. That is not the same question
# as "may this file leave the institution". An ETD can be accepted, posted
# and perfectly visible while its Document Type and Release Option reads
# "Thesis (restricted to <institution>)" with an embargo date — 769 of the
# 3,383 records in one collection do. Until 2026-09-09 every one of those
# downloaded into a batch the report labelled `public`.
#
# The signal is in the listing's Type column, which the module already
# fetches, so reading it costs nothing.
#
# Two rules here, both deliberate:
#
#  * The match is on the RELEASE WORDING, never on an institution name.
#    A cell reading "restricted to <this institution>" is one instance's
#    phrasing of a platform-wide convention; matching the name would break
#    for every other institution AND put an identity marker in shared
#    source — which the --generic build rightly refuses. (It refused this
#    very comment on the first attempt.)
#  * An unrecognized cell is UNKNOWN, never open. Three cells in that same
#    collection read "" or "Dissertation/Thesis" with no parenthetical at
#    all, and guessing "open" for those is exactly the kind of cheerful
#    assumption this column exists to stop.
# ---------------------------------------------------------------------------
ACCESS_OPEN = "open"
ACCESS_RESTRICTED = "restricted"
ACCESS_UNKNOWN = "unknown"
# The listing does not express access at all — a different thing from
# "expresses it and I could not read it", and the distinction is the whole
# point. See resolve_listing_access() below.
ACCESS_NA = "n/a"

_ACCESS_RESTRICTED_RE = re.compile(
    r"restrict|embargo|closed\s+access|campus\s+only|not\s+available", re.I)
_ACCESS_OPEN_RE = re.compile(r"open\s+access|unrestricted", re.I)


def access_state(release: str) -> str:
    """Classify a listing Type / Release Option cell.

    Returns ACCESS_RESTRICTED, ACCESS_OPEN or ACCESS_UNKNOWN. Restricted is
    tested first: a value that somehow said both should be treated as the
    more careful of the two.
    """
    text = (release or "").strip()
    if not text:
        return ACCESS_UNKNOWN
    if _ACCESS_RESTRICTED_RE.search(text):
        return ACCESS_RESTRICTED
    if _ACCESS_OPEN_RE.search(text):
        return ACCESS_OPEN
    return ACCESS_UNKNOWN


def resolve_listing_access(items) -> str:
    """Decide, for a whole structure, whether its listing expresses access.

    Added 2026-09-09, immediately after the first version of this shipped and
    was wrong on the first real structure it met.

    "Type" is one column with two different meanings. On an ETD collection it
    is "Document Type and Release Option" and reads "Thesis (open access)" or
    "Thesis (restricted to ...)". On a faculty series it is a plain document
    type — "Newsletter", "Article" — carrying no access information at all.
    Classifying a cell on its own cannot tell those apart, so the first cut
    called 170 newsletters `unknown` and the run summary advised treating 176
    public files as restricted until checked. A warning that fires on every
    series and every gallery is how people learn to ignore warnings.

    Whether the listing expresses access is a property of the LISTING, not of
    a row, and it is answerable: if no row anywhere in the structure carries
    a recognizable release option, the column is not a release column, and
    every row is n/a. If some rows do, then a row that does not is genuinely
    unknown and worth flagging — in the measured ETD collection that is 3 of
    3,383, which is a number someone can actually act on.

    Mutates `items` in place and returns the verdict for logging.
    """
    speaks = any(it.get("access") in (ACCESS_OPEN, ACCESS_RESTRICTED)
                 for it in items)
    if speaks:
        return "release"
    for it in items:
        it["access"] = ACCESS_NA
    return "type-only"


def _header_map(tr):
    """Map lowercased header-cell text → cell index for a header row."""
    return {c.get_text(strip=True).lower(): i
            for i, c in enumerate(tr.find_all(["th", "td"]))}


def _find_listing_table(soup, required=("id", "title")):
    """Return (table, header_map) for the first table whose first row
    carries the required header names, else (None, None)."""
    for tbl in soup.find_all("table"):
        tr = tbl.find("tr")
        if tr is None:
            continue
        hmap = _header_map(tr)
        if all(name in hmap for name in required):
            return tbl, hmap
    return None, None


def _listing_rows(table, hmap):
    """Extract item rows from a listing table (both flavors)."""
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        texts = [c.get_text(strip=True) for c in cells]

        def col(name):
            i = hmap.get(name, -1)
            return texts[i] if 0 <= i < len(texts) else ""

        # The article id comes from the column the header map NAMES, not from
        # cell 0. Both real listings put "ID" first — checked 2026-09-08
        # against a gallery (`table#submission_list`) and an ETD
        # (`_find_listing_table`), so this changes nothing today — but the
        # id's position was an assumption the code made while holding the
        # map that answers it. A flavor with a select box or an icon in
        # column 0 would have returned ZERO rows for the whole structure,
        # silently, and cell 0 is not a thing worth betting a structure on.
        article = col("id")
        if not article.isdigit():
            continue                     # header, filter, or separator row
        if len(cells) < len(hmap) - 2:
            continue                     # malformed / colspan row

        af_text = col("additional files")
        # "type" is the Document Type and Release Option on an EdiKit
        # listing ("Thesis (open access)" / "Thesis (restricted to ...)").
        # Gallery listings have no such column, so this is "" there and
        # access_state() reports unknown, which is the honest answer.
        release = col("type")
        rows.append({
            "article": article,
            "title": col("title"),
            "last_event": col("last event"),
            "submitted": col("submitted"),
            "release": release,
            "access": access_state(release),
            "af": int(af_text) if af_text.isdigit() else
                  (0 if af_text == "-" else None),
        })
    return rows


def parse_gallery_listing(html: str):
    """Parse a gallery Manage Items page (table#submission_list)."""
    soup = _soup(html)
    table = soup.find("table", id="submission_list")
    if table is None:
        return None
    tr = table.find("tr")
    hmap = _header_map(tr) if tr is not None else {}
    if "id" not in hmap:
        return None
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() == "next":
            nxt = a["href"]
            break
    return {"rows": _listing_rows(table, hmap), "next": nxt}


def parse_edikit_listing(html: str):
    """Parse an EdiKit Manage Submissions page (ID-headed table)."""
    soup = _soup(html)
    table, hmap = _find_listing_table(soup)
    if table is None:
        return None
    nxt = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == ">":
            nxt = a["href"]
            break
    return {"rows": _listing_rows(table, hmap), "next": nxt}


# ---------------------------------------------------------------------------
# View-revisions parsing — table#revision on window=pickvers. Rows are
# NEWEST FIRST. Returns a list of revisions:
#   {"user","comment","date","current":bool,"show_prev":bool,
#    "files":[{"kind":"primary"|"native"|"coverletter","href","text"}]}
# (an empty list = table present but no revision rows), or None when the
# page has no #revision table (unexpected page / login bounce upstream).
# "current" = this row's editor-selected "Select" radio is checked.
# "show_prev" = this row's previous-version "Show" box is checked.
# ---------------------------------------------------------------------------
_SKIP_ROW_RE = re.compile(
    r"^(user$|editor selected|note:|pdf versions listed)", re.IGNORECASE)


# Links that must never be fetched, however they turn up. `force=yes`
# regenerates the record's PDF derivative on the server - a WRITE, dressed
# as a viewcontent link, and so indistinguishable from a download to the
# "viewcontent in href" test the parsers use. It lives on the submission
# details page today, which nothing parses, so this is a guard rather than a
# fix; it is here because open item 6 proposes parsing exactly that page,
# and the guard is worth more before that work than after it.
HARVEST_FORBIDDEN = ("force=yes",)


def harvestable(href: str) -> bool:
    low = (href or "").lower()
    return not any(bad in low for bad in HARVEST_FORBIDDEN)


def _own_selection(a):
    """The editor's Select radio that governs ONE file link, if it has one.

    v1.34.2. The real revisions table selects the current file PER TYPE:
    each link sits in its own `<p class="file">` beside a radio named
    `text.pdf` or `text.native`, and the editor can choose the current PDF
    from one revision and the current native from another. Checked against
    a captured revisions page (an ETD record, 2026-09-08): the native's
    radio and the PDF's radio are separate groups in separate cells.

    The module used to ask only "is ANY radio in this row checked?", so a
    revision whose native was selected was labelled current in full — and
    its PDF, an earlier PDF, was saved and reported as the current one.
    That is what a journal's article 1004 showed in a live run: two
    revisions both labelled `current`, with different PDFs.

    Returns (group, selected): the radio's name and whether it is checked,
    or ("", None) when the link's own container holds no radio — the older
    single-radio layout, where the row-level answer is the only one there
    is and is still used.
    """
    holder = a.find_parent("p") or a.find_parent("td")
    if holder is None:
        return "", None
    radio = holder.find("input", {"type": "radio"})
    if radio is None:
        return "", None
    return str(radio.get("name") or ""), radio.has_attr("checked")


def parse_pickvers(html: str):
    table = _soup(html).find("table", id="revision")
    if table is None:
        return None

    revs = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        user = cells[0].get_text(strip=True)
        if _SKIP_ROW_RE.match(user or ""):
            continue
        comment = cells[1].get_text(strip=True)
        date = cells[2].get_text(strip=True)

        files = []
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if "viewcoverletter" in href:
                files.append({"kind": "coverletter", "href": href,
                              "text": a.get_text(strip=True)})
                continue
            if "viewcontent" not in href or not harvestable(href):
                continue
            ftype = content_link_type(href)
            if ftype == "native":
                kind = "native"
            elif ftype in ("pdf", ""):
                # "" = a bare link to this row's content, with no type named.
                # Treat it as the primary; the native fallback sorts out a
                # record that has no derivative behind it.
                kind = "primary"
            else:
                kind = safe_token(ftype) or "file"
            group, selected = _own_selection(a)
            files.append({"kind": kind, "href": href,
                          "text": a.get_text(strip=True),
                          "group": group, "selected": selected})

        current = any(inp.has_attr("checked")
                      for inp in tr.find_all("input", {"type": "radio"}))
        show_prev = any(inp.has_attr("checked")
                        for inp in tr.find_all("input", {"type": "checkbox"}))
        if not (user or date or files):
            continue
        revs.append({"user": user, "comment": comment, "date": date,
                     "current": current, "show_prev": show_prev,
                     "files": files})
    return revs


def label_versions(revs):
    """Assign version labels IN PLACE and return revs.

    Two levels, because the page has two.

    Each REVISION gets `version` as before: chronological, the oldest
    file-bearing revision = "original", later ones = "rev2", "rev3", …, and
    the editor-selected revision = "current" (falling back to the newest
    file-bearing revision when no Select radio is checked). It also gets
    `chrono`, its chronological label whether or not it is current.

    Each FILE gets `version` and `is_current` (v1.34.2). Where the page
    selects per type — a radio beside each link, see _own_selection() — a
    file is current only when ITS radio is the checked one in its group (or,
    with none checked, it is the newest file of that group); every other
    file takes its revision's chronological label. A file with no radio of
    its own (a cover letter) is current when its revision holds the
    selected PDF — or, on a record with no PDF, the selected native. Where
    the page has only the older single radio per row, every file takes its
    revision's label, exactly as before.
    """
    file_revs = [r for r in revs if r["files"]]
    if file_revs and not any(r["current"] for r in file_revs):
        file_revs[0]["current"] = True           # newest row = current
    chrono = list(reversed(file_revs))           # oldest → newest
    for i, rev in enumerate(chrono, start=1):
        rev["chrono"] = "original" if i == 1 else "rev{}".format(i)
        rev["version"] = "current" if rev["current"] else rev["chrono"]
    for rev in revs:
        rev.setdefault("version", "")            # metadata-only revisions
        rev.setdefault("chrono", "")

    per_file = any(f.get("selected") is not None
                   for r in file_revs for f in r["files"])
    if not per_file:
        for rev in revs:
            for f in rev["files"]:
                f["version"] = rev["version"]
                f["is_current"] = rev["version"] == "current"
        return revs

    # The chosen file of each radio group. Rows are newest first, so the
    # first file met in a group is its newest.
    chosen, newest, lead_kind = {}, {}, {}
    for rev in file_revs:
        for f in rev["files"]:
            if f.get("selected") is None:
                continue
            g = f.get("group") or f["kind"]
            newest.setdefault(g, f)
            lead_kind.setdefault(g, f["kind"])
            if f["selected"] and g not in chosen:
                chosen[g] = f
    for g, f in newest.items():
        chosen.setdefault(g, f)
    chosen_ids = {id(f) for f in chosen.values()}
    lead = next((g for g, k in lead_kind.items() if k == "primary"),
                next(iter(chosen), None))
    lead_rev = None
    if lead is not None:
        for rev in file_revs:
            if any(f is chosen[lead] for f in rev["files"]):
                lead_rev = rev
                break
    for rev in revs:
        for f in rev["files"]:
            if f.get("selected") is None:
                cur = rev is lead_rev
            else:
                cur = id(f) in chosen_ids
            f["is_current"] = cur
            f["version"] = "current" if cur else (rev["chrono"]
                                                  or rev["version"])
    return revs


# ---------------------------------------------------------------------------
# Supplemental-content parsing — table#uploaded-files on
# window=additional_files. Returns
#   [{"idx","name","href","shown":bool}, ...]
# (empty list = table present but no files), or None when the table is
# missing entirely.
# ---------------------------------------------------------------------------
def parse_additional_files(html: str):
    table = _soup(html).find("table", id="uploaded-files")
    if table is None:
        return None

    out = []
    for tr in table.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            if "viewcontent" in a["href"]:
                link = a
                break
        if link is None:
            continue
        idx = content_link_filename(link["href"])
        show_box = tr.find("input", {"type": "checkbox"})
        out.append({"idx": idx,
                    "name": link.get_text(strip=True),
                    "href": link["href"],
                    "shown": bool(show_box is not None
                                  and show_box.has_attr("checked"))})
    return out


# ---------------------------------------------------------------------------
# Saved-file naming: <context>_<article>_<kind>_<visibility>_<version>_<orig>
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Admin record page — the metadata table only.
#
# This is deliberately NOT the submission-details parser of open item 7. It
# reads the page's label/value table and nothing else: it harvests no links
# at all, so `force=yes` (a write dressed as a download), Reviewers and
# Decision Letters cannot be reached through it even by accident. It exists
# for one field, Embargo Period, which is on this page and nowhere cheaper.
#
# It costs one request per record it is used on, which is why the run only
# uses it for records the listing has already flagged restricted, and only
# when the operator asks for it.
# ---------------------------------------------------------------------------
_EMPTY_VALUE_RE = re.compile(r"^-?\s*empty\s*-?$", re.I)


def parse_record_metadata(html: str):
    """Label → value for an admin record page's metadata table.

    Returns {} when the page has no such rows — never None, because "no
    metadata found" and "no embargo date" are the same outcome here and a
    caller that had to tell them apart would only guess.
    """
    soup = _soup(html)
    out = {}
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th is None or td is None:
            continue
        label = th.get_text(strip=True)
        if not label:
            continue
        value = td.get_text(" ", strip=True).replace("\xa0", " ").strip()
        if _EMPTY_VALUE_RE.match(value):
            value = ""
        out.setdefault(label, value)
    return out


def embargo_from_metadata(meta: dict) -> str:
    """The Embargo Period value, matched case-insensitively. "" when absent."""
    for label, value in (meta or {}).items():
        if label.strip().lower() == "embargo period":
            return value
    return ""


def safe_token(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", (name or "").strip()).strip("-_.")


# Content-Type → extension map, consulted only after the file's own bytes
# have failed to identify it. Deliberately has NO entry for
# application/octet-stream: DC serves many natives under it, and mapping it
# to anything would override a correct sniff with a guess.
_CTYPE_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument"
    ".spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument"
    ".presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/tiff": ".tif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/jp2": ".jp2",
    "image/x-icon": ".ico",
    "image/x-canon-cr2": ".cr2",
    "image/x-adobe-dng": ".dng",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}

# Placeholder extensions that carry no information and must be re-resolved
# from the bytes. ".bin" is on this list on purpose: a previous run's failed
# guess must not become a permanent one.
_PLACEHOLDER_EXT = ("", ".native", ".cgi", ".bin", ".unknown")


def _zip_family_ext(data: bytes) -> str:
    """Distinguish the OOXML formats inside a ZIP container by their first
    entry name. Falls back to .zip rather than guessing wrong."""
    head = data[:2048]
    if b"word/" in head:
        return ".docx"
    if b"xl/" in head:
        return ".xlsx"
    if b"ppt/" in head:
        return ".pptx"
    return ".zip"


def sniff_extension(data: bytes) -> str:
    """Identify a file from its leading bytes. Returns "" when unrecognized.

    Deliberately conservative: an unknown file returns "" so the caller can
    fall through to Content-Type rather than being handed a wrong answer.
    """
    if not data:
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        # TIFF container. Canon raw and Adobe DNG both live inside one and
        # would otherwise be mislabelled .tif — the Herbarium image records
        # are CR2 originals, so this branch is load-bearing, not theoretical.
        if data[8:10] == b"CR":
            return ".cr2"
        if b"DNG" in data[:512] or b"Adobe" in data[:512]:
            return ".dng"
        return ".tif"
    if data.startswith(b"BM"):
        return ".bmp"
    if len(data) >= 12 and data[0:4] == b"RIFF":
        if data[8:12] == b"WEBP":
            return ".webp"
        if data[8:12] == b"WAVE":
            return ".wav"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n"):
        return ".jp2"
    if data.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if data.startswith(b"PK\x03\x04"):
        return _zip_family_ext(data)
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".doc"            # OLE2 family — .doc/.xls/.ppt share it
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if data.lstrip()[:5].lower() in (b"<!doc", b"<html"):
        return ""                # an HTML error page is not a file
    try:
        data[:2048].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return ".txt"


FILENAME_MAX_CHARS = 120   # v1.34; a 120-character folder still fits 260


class SaveFailed(Exception):
    """The file came down and could not be written where it belongs.

    v1.34. Until then an `OSError` from `save_as` — a path too long for
    Windows, a FAT32 volume refusing a file over 4 GB, a full disk — was
    caught only by each worker's whole-run handler, so ONE file ended the
    RUN, and every re-run stopped at the same row. Now it fails that file,
    names what the operating system said, and the run goes on.
    """


def place_file(got, folder, fname):
    """Save a fetched file under `fname` in `folder`. Returns the path.

    One function both workers call, so the rule cannot reach one and not
    the other. The incoming copy is removed when it cannot be kept: a
    re-run fetches it again, and a stray full-size file in the private
    download folder is disk nobody is watching.
    """
    path = unique_path(folder, fname)
    try:
        return got.save_as(path)
    except OSError as e:
        leftover = getattr(got, "path", "")
        if leftover and os.path.exists(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise SaveFailed("could not be saved as {} ({} characters): "
                         "{}".format(path, len(path), describe_error(e)))


def build_filename(ctx, article, kind, visibility, version, orig_name,
                   content_type="", data=b""):
    """Build the renamed filename for one downloaded file.

    Extension precedence: a real filename from Content-Disposition wins (it
    preserves distinctions no sniffer can make), then the file's own bytes,
    then Content-Type, then .bin.

    The leading tokens of this name are parsed back out by the Image
    Description Generator (infer_meta) to relink a file to its record — the
    extension may change freely, the token order may not.
    """
    base, ext = os.path.splitext(orig_name or "")
    ext = ext.lower()
    if ext in _PLACEHOLDER_EXT:
        ext = (sniff_extension(data)
               or _CTYPE_EXT.get((content_type or "").split(";")[0].strip()
                                 .lower(), "")
               or ".bin")
    lead = "_".join(t for t in (safe_token(ctx), str(article), kind,
                                visibility, version) if t)
    tail = safe_token(base) or "file"
    # v1.34: a CAP. Measured on one complete structure: saved
    # names reached 168 characters (214 with natives on), and at a
    # plausible managed-workstation destination 2 of 1,133 paths passed
    # Windows' 260-character limit — each of which ended the whole run.
    # Only the original-name tail is shortened; the leading tokens are
    # what infer_meta parses back out, and the extension is kept.
    room = FILENAME_MAX_CHARS - len(lead) - 1 - len(ext)
    if len(tail) > room:
        tail = tail[:max(room, 8)].rstrip("-_.") or "file"
    return lead + "_" + tail + ext


def unique_path(folder: str, filename: str) -> str:
    """Return a collision-free path in `folder` for `filename`."""
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(filename)
    n = 2
    while True:
        path = os.path.join(folder, "{}_{}{}".format(base, n, ext))
        if not os.path.exists(path):
            return path
        n += 1


# ---------------------------------------------------------------------------
# Cookie-based fetching — pages and files are fetched with urllib using the
# attached debug Chrome's cookies, so downloads land in the user's chosen
# folder (never Chrome's download directory) and credentials are never
# touched. The driver must currently be on a page of the DC domain when the
# cookies are harvested.
# ---------------------------------------------------------------------------
def _is_driver_timeout(exc) -> bool:
    """A timeout talking to CHROMEDRIVER, not to Digital Commons.

    It surfaces as urllib3's ReadTimeoutError naming a localhost port, and
    it reads like a broken driver or a dead server. It is neither: it means
    the browser did not answer one command in time. Twice on 2026-09-09 it
    ended a run at exactly 120.0s — Selenium's client default — and the
    message named `localhost:57545`, which says nothing a person can act
    on.
    """
    name = exc.__class__.__name__
    text = str(exc)
    # 2026-09-30: "Timed out receiving message from renderer: 270.000" —
    # chromedriver waiting on the TAB, which names no address at all, so
    # the check above never matched it and the run ended as an
    # "Unexpected error" with a page of stack frames.
    return ("Timeout" in name
            and ("localhost" in text or "127.0.0.1" in text
                 or "receiving message from renderer" in text))


def unexpected_failure(exc) -> str:
    """What both workers say when a run ends on an error nobody named.

    One function since v1.34.1: the browser-timeout explanation lived in
    download_worker only, so the same Chrome timeout in a re-run read as
    a bare "Unexpected error". Name the browser, not Digital Commons, and
    give the remedy.
    """
    if _is_driver_timeout(exc):
        return ("Chrome did not answer within the timeout. This is the "
                "browser, not Digital Commons. Quit the debug Chrome window "
                "completely and start it again with the same command, then "
                "re-run; a wedged connection from an earlier run survives a "
                "module restart. The underlying error was: {}"
                .format(describe_error(exc)))
    return "Unexpected error: {}".format(describe_error(exc))


def mark_native_fallback(exc, native_url):
    """Say on the error that it came from the NATIVE fallback (v1.34.2).

    The row's File URL is the derivative's, so "not confirmed" beside it
    read as a claim about the derivative — which had answered, that it is
    absent. describe_error() puts the native's URL in front of the message.
    An attribute rather than a rewritten message, so the class (which the
    row's status is drawn from) and an HTTPError's code are untouched; one
    function, called by both fetch paths.
    """
    try:
        exc.native_fallback = native_url
    except Exception:                                # noqa: BLE001
        pass
    return exc


def describe_error(exc) -> str:
    """A failure message that names what actually went wrong.

    Bare class names cost a day of diagnosis. On 2026-09-08 a run reported
    `Failed: HTTPError` for every failing row; the status code was never
    shown, so "4xx on everything" was all anyone could report and the cause
    could not be narrowed. `HTTPError` already renders itself as
    "HTTP Error 403: Forbidden" — `str()` alone would have said it. The old
    code excluded it from an isinstance whitelist and fell through to
    `__class__.__name__`.

    So: never return a class name on its own. Our own exceptions carry
    written messages and are passed through unchanged; urllib's carry a
    status or a reason and are rendered explicitly.

    An error marked by mark_native_fallback() says first that it is the
    native's, and at which URL (v1.34.2).
    """
    # Read from the instance dict, never with getattr(): on Python 3.9 an
    # HTTPError with no body delegates unknown attributes to a file it
    # does not have and raises KeyError, which getattr's default does not
    # catch — found by the 3.9 run of the suite before it reached CI.
    fallback = (getattr(exc, "__dict__", None) or {}).get("native_fallback",
                                                          "")
    if fallback:
        return ("the derivative answered that it is absent; its native "
                "fallback, {}, did not say whether a file exists — {}".format(
                    fallback, _describe_error(exc)))
    return _describe_error(exc)


def _describe_error(exc) -> str:
    if isinstance(exc, (ValueError, NoFileAvailable, RateLimited)):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP {} {}".format(exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        return "network error: {}".format(exc.reason)
    # chromedriver appends "(Session info: …)" and a page of numbered
    # stack frames to every message. They are addresses inside a binary,
    # not something a person can act on, and on 2026-09-30 they were the
    # whole of what the page showed. The first line says what happened.
    text = str(exc).split("Stacktrace:")[0]
    text = re.sub(r"\(Session info:[^)]*\)", "", text)
    text = " ".join(text.replace("Message:", "").split()).strip()
    return ("{}: {}".format(exc.__class__.__name__, text) if text
            else exc.__class__.__name__)


class LoginRequired(Exception):
    """The response was Digital Commons' login page — session expired."""


class RateLimited(Exception):
    """The server refused with 429/503 and kept refusing after the retries.

    Distinct from every other failure on purpose. A rate limit says nothing
    about whether the file exists, so it must never be folded into a "no
    downloadable file" verdict — that is what turned twelve throttled
    records into twelve records reported as empty.
    """


def cookie_header(driver) -> str:
    return "; ".join("{}={}".format(c.get("name"), c.get("value"))
                     for c in driver.get_cookies() if c.get("name"))


def _retry_after_seconds(exc) -> float:
    """Seconds the server asked us to wait, or 0.0 if it did not say.

    Retry-After is either a count of seconds or an HTTP date; both are legal
    and DC has been seen sending neither.
    """
    raw = ""
    try:
        raw = (exc.headers.get("Retry-After") or "").strip()
    except Exception:
        return 0.0
    if not raw:
        return 0.0
    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when is None:
            return 0.0
        import datetime
        now = datetime.datetime.now(when.tzinfo) if when.tzinfo \
            else datetime.datetime.now()
        return max(0.0, (when - now).total_seconds())
    except Exception:
        return 0.0


def is_pushback(exc) -> bool:
    """The server declined to answer, rather than answering.

    429/503 say so outright. A redirect loop is the same thing arriving as
    a 3xx that urllib gave up following. Both are retried, and neither may
    ever become a claim about the record's contents.
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if exc.code in RATE_LIMIT_CODES:
        return True
    return exc.code in REDIRECT_CODES or "infinite loop" in str(exc)


def is_definite_absence(exc) -> bool:
    """True only when the server actually told us there is nothing here.

    This is the distinction v1.7 got wrong. A web page where a file was
    expected ("No PDF has been provided"), an empty body, or a 404/410 are
    answers: the file is not there. A throttle, a redirect loop, a timeout
    or a reset are the server declining to answer, and the file may well
    exist. Only two answers may be combined into "no downloadable file";
    anything else is reported as the transport failure it was, because a
    report that says a record is empty is a report someone acts on.
    """
    if isinstance(exc, ValueError):          # our own "web page"/"empty"
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (404, 410)
    return False


def _cooldown_refusal(asked: float, why: str) -> str:
    return ("server locked this run out for {:.0f}s (Retry-After) and {} — "
            "nothing was fetched. The file is not missing; re-run after "
            "that interval.".format(asked, why))


def _wait_out_cooldown(asked: float) -> bool:
    """Sit out a named lockout. False if Stop cut the wait short.

    Announced on the way in and every COOLDOWN_TICK seconds after, because
    a ten-minute silence is indistinguishable from a hung run - which is a
    mistake this module has already made once.
    """
    st = rate_state()
    _note("server has locked this run out for {:.0f}s — waiting it out "
          "(cooldown {} of {}). Every pending file shares this one expiry, "
          "so a single wait clears them all. Stop still works."
          .format(asked, st["cooldowns"], st["budget"]))
    left = asked + COOLDOWN_GRACE
    while left > 0:
        set_progress_msg(
            "server lockout — resuming in {:.0f}m {:02.0f}s".format(
                left // 60, left % 60))
        nap = min(COOLDOWN_TICK, left)
        if not _sleep(nap):
            _note("cooldown wait interrupted — stopping.")
            return False
        left -= nap
        if left > 0:
            _note("cooldown: {:.0f}s to go".format(left))
    _note("cooldown expired — resuming.")
    set_progress_msg("resuming after the lockout…")
    return True


def _open(url: str, cookie: str, timeout: int, kind: str = "file"):
    """GET a URL with the admin session cookie, retrying server pushback.

    Every request the module makes goes through here — listing pages, item
    pages and files — so the backoff covers the whole run rather than only
    the download step.
    """
    req = urllib.request.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": "DC-Admin-Suite BatchFileDownloader",
    })
    wait = RATE_BACKOFF_BASE
    last = None
    tries = 0
    # Defence in depth. Every path out of this loop is meant to be bounded -
    # `tries` for backoff, the cooldown budget for lockouts - but a loop
    # whose only bound lives in shared mutable state is one edit away from
    # spinning forever, with no timeout and no progress to show for it.
    # This bound depends on nothing but the constants.
    rounds, hard_stop = 0, RATE_RETRIES + 2 * COOLDOWN_HARD_MAX + 4
    while True:
        rounds += 1
        if rounds > hard_stop:
            raise RateLimited(
                "gave up after {} attempts against a server that kept "
                "refusing — nothing was fetched, and the file is not "
                "missing".format(hard_stop))
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if not is_pushback(e):
                raise
            last = e
            # Attribute it. A 429 used to be counted and not ascribed, so a
            # lockout said nothing about which pace provoked it — and with
            # admin pages and downloads now paced differently, that is the
            # only question worth asking of a lockout.
            _note_rate_limit(kind)
            asked = _retry_after_seconds(e)
            if asked > RATE_HONOR_CAP:
                # A named lockout, not a slow-down. Sit it out if the run
                # still has a wait to spend; a wait is deliberately NOT a
                # retry, because after it the request should simply work.
                if asked > COOLDOWN_MAX_SECONDS:
                    raise RateLimited(_cooldown_refusal(
                        asked, "that is longer than this run will ever "
                               "wait ({:.0f}s)".format(COOLDOWN_MAX_SECONDS)))
                if not _claim_cooldown_wait():
                    st = rate_state()
                    raise RateLimited(_cooldown_refusal(
                        asked,
                        "this run is set not to wait out cooldowns"
                        if st["budget"] == 0 else
                        "this run has already waited out {} cooldown(s), "
                        "its limit".format(st["cooldowns"])))
                if not _wait_out_cooldown(asked):
                    break
                continue
            tries += 1
            if tries > RATE_RETRIES:
                break
            if asked > 0:
                pause, why = asked, "it asked for {:.0f}s".format(asked)
            else:
                pause = min(wait * (1.0 + random.random() * 0.3),
                            RATE_BACKOFF_CAP)
                why = "no Retry-After, so backing off"
            # Say it out loud, and say whose number it is. A silent backoff
            # is why a working run looked hung on 2026-09-08, and a wait
            # that does not name its source is why the 20s cap silently
            # overrode what the server asked for.
            _note("HTTP {} from the server — {}; waiting {:.0f}s and "
                  "retrying ({} of {})".format(e.code, why, pause,
                                               tries, RATE_RETRIES))
            if not _sleep(pause):
                break                 # Stop pressed; do not keep retrying
            wait = min(wait * 2.0, RATE_BACKOFF_CAP)
    raise RateLimited(
        "server refused the request: HTTP {} {} after {} attempts — it "
        "is pushing back on this run, not saying the file is missing"
        .format(last.code, str(last.reason).splitlines()[0],
                RATE_RETRIES + 1))


def fetch_html(url: str, cookie: str, timeout: int = PAGE_TIMEOUT) -> str:
    """GET an admin page; raises LoginRequired on a login bounce.

    WHERE THE RESPONSE CAME FROM IS THE STRONGER SIGNAL, and until
    2026-09-22 nothing looked at it. urllib follows the bounce, so the
    body read here is the login page's — but this only ever searched
    that body for two field names, and on a real expired session it did
    not match. The request came back HTTP 200 with a perfectly readable
    page, the crawl called it "no records in this state", and a re-run
    reported a record that exists as gone.

    `resp.geturl()` is the URL the bytes actually came from, and a
    bounce lands on `/cgi/login.cgi`, which `looks_like_login_page`
    already recognizes — it was simply never shown it. Markup can be
    reskinned and field names renamed; the address the server sent us
    to cannot be mistaken for an admin page.

    The body check stays as well, for an installation that renders a
    login form without redirecting.
    """
    with _open(url, cookie, timeout, "page") as resp:
        html = resp.read().decode("utf-8", "replace")
        landed = ""
        try:
            landed = resp.geturl() or ""
        except Exception:
            landed = ""
    if looks_like_login_page(html, landed):
        raise LoginRequired()
    return html


def fetch_file(url: str, cookie: str, timeout: int = FILE_TIMEOUT):
    """GET a file; returns (bytes, filename-from-content-disposition,
    content-type). Raises LoginRequired on a login bounce and ValueError
    (user-facing message) when the response is an HTML page instead of a
    file."""
    with _open(url, cookie, timeout) as resp:
        data = resp.read()
        cd = resp.headers.get("Content-Disposition", "") or ""
        ctype = resp.headers.get("Content-Type", "") or ""
        # Same signal, same reason — see fetch_html. A file request that
        # lands on the login page is a session expiry, whatever the body
        # turns out to look like.
        try:
            landed = resp.geturl() or ""
        except Exception:
            landed = ""

    if not data:
        raise ValueError("empty response from the server")
    _refuse_a_page(data[:4096], landed)
    return data, _disposition_name(cd), ctype


def _refuse_a_page(head, landed):
    """A web page where a file was expected is not a file. One test, both
    the in-memory and the streamed fetch."""
    head = head.lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") \
            or b"<html" in head[:512]:
        if b'name="password"' in head and b'name="login"' in head:
            raise LoginRequired()
        if looks_like_login_page("", landed):
            raise LoginRequired()
        raise ValueError("server returned a web page instead of a file")


def _disposition_name(cd):
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd or "") \
        or re.search(r'filename="?([^";]+)"?', cd or "")
    return unquote(m.group(1)).strip() if m else ""


def fetch_file_to(url: str, cookie: str, timeout: int, folder: str):
    """GET a file STRAIGHT TO DISK; returns (path, name, content-type).

    v1.34. `fetch_file` reads the whole body into memory, which is fine
    for a thesis and not for the 10 GB files this module now has to
    expect. The body is streamed a megabyte at a time into a hidden
    `.net-*.part` file in `folder` — hidden, so the download watcher
    (which ignores dotfiles) can never mistake it for a browser
    download — and renamed when complete. A failure removes it: a
    partial file with a real record's name on it is worse than none.
    """
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".net-", suffix=".part", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out, _open(url, cookie, timeout) as resp:
            cd = resp.headers.get("Content-Disposition", "") or ""
            ctype = resp.headers.get("Content-Type", "") or ""
            try:
                landed = resp.geturl() or ""
            except Exception:
                landed = ""
            head = resp.read(4096)
            if not head:
                raise ValueError("empty response from the server")
            _refuse_a_page(head, landed)
            out.write(head)
            shutil.copyfileobj(resp, out, 1 << 20)
        final = tmp[:-len(".part")]
        os.replace(tmp, final)
        return final, _disposition_name(cd), ctype
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class NoFileAvailable(Exception):
    """Neither the derivative nor the native could be fetched for a record."""


class VerificationRequired(Exception):
    """The tab is showing a challenge: confirm a person is here.

    Not a failure and not an absence. It says nothing whatever about
    whether the file exists — only that the question has to be answered
    before anything else is handed over. Carries what was observed rather
    than a conclusion about it.
    """

    def __init__(self, observed="", url=""):
        self.observed = observed
        self.url = url
        Exception.__init__(
            self, "the site is asking for confirmation that a person is "
                  "here" + (" — tab: {!r}".format(observed)
                            if observed else ""))


class BrowserFetchFailed(Exception):
    """No file arrived and the page did not say why.

    The message is an OBSERVATION — what the tab's title was, where it
    landed, what the folder did — never a diagnosis. Twice in one afternoon
    a probe printed a confident cause that was wrong ("Chrome probably
    rendered it" for rows that were never file URLs, and again on a page
    whose entire message was "No PDF has been provided"). A diagnosis that
    sounds like a finding is worse than none, so this reports and lets the
    reader conclude.

    Deliberately NOT a definite absence: nothing here licenses the claim
    that the record has no file, and is_definite_absence() agrees by not
    knowing about it.
    """


UNCONFIRMED = "Not confirmed from this network"


class NotConfirmedHere(BrowserFetchFailed):
    """Neither path could answer from THIS network (v1.34).

    The browser observed nothing it could read, and the network layer —
    the only path that hears a 404 — was asked to verify that a person is
    present, which no program can answer honestly. Measured from outside
    the institution's network: 337 native rows, every one of which had
    been an absence or a 404 when the same collection was run from inside
    it.

    A failure, never an absence: nothing establishes that the file is not
    there. But not an ordinary failure either, because retrying it from
    the same network meets the same question. So it has a status of its
    own, re-runs skip it unless asked, and each structure reports how
    many there were in one line rather than one warning per row.
    """


def unconfirmed_line(ctx, n):
    """The one summary line both workers log for NotConfirmedHere rows."""
    return ("{}: {} file request(s) could not be confirmed from this "
            "network — the network layer was asked to verify that a person "
            "is present, which it cannot answer. They are neither "
            "downloaded nor absent. A re-run skips them unless asked; a run "
            "from a network the repository does not challenge settles "
            "them.".format(ctx, n))


def fetch_record_file(url: str, cookie: str, native_url: str = "",
                      timeout: int = FILE_TIMEOUT, folder: str = ""):
    """Fetch one record file, falling back to the native when DC has no
    derivative to serve. Returns (data, name, content_type, used_native).

    DC generates a PDF derivative only from Word, PowerPoint and PDF
    uploads. Every other upload — images, spreadsheets, CSV, ZIP, audio,
    video, raw camera files — exists only as the native, and the default
    content URL answers with a web page instead. So a "web page instead of a
    file" is the normal signal to ask for the native, not a failure.

    The second request is issued ONLY after the first has actually failed:
    this is the hot path for every file in a run.

    With `folder` (v1.34) each body is streamed to disk there, and the
    first element returned is that file's PATH rather than its bytes.
    """
    def get(u):
        if folder:
            return fetch_file_to(u, cookie, timeout, folder)
        return fetch_file(u, cookie, timeout)

    try:
        data, name, ctype = get(url)
        return data, name, ctype, False
    except RateLimited:
        # The server refused to answer. Whether a derivative exists is
        # unknown, so asking for the native next would only earn a second
        # refusal and let the pair be reported as an absence.
        #
        # Redundant today — RateLimited is not in the tuple below, so it
        # would propagate anyway — and kept deliberately: it is load-bearing
        # the moment anyone widens that tuple, which is how the second
        # handler swallowed it. Stated because a guard that quietly does
        # nothing is the kind of thing this file has been bitten by.
        raise
    except LoginRequired:
        # Never swallow a session expiry — the worker's mid-run handling
        # (expired_mid, _PARTIAL reports) depends on seeing this.
        raise
    except (ValueError, urllib.error.HTTPError, OSError) as first_error:
        if not native_url:
            raise
        first = first_error
    _throttle("file")
    try:
        data, name, ctype = get(native_url)
    except (LoginRequired, RateLimited):
        raise
    except Exception as exc:
        second = exc
        if is_definite_absence(first) and is_definite_absence(second):
            raise NoFileAvailable(
                "no downloadable file — derivative: {} | native: {}"
                .format(describe_error(first), describe_error(second)))
        # At least one attempt never got an answer, so nothing here
        # licenses a claim that the record has no file. Report the failure
        # that was not an answer - that is the one a human can act on.
        if is_definite_absence(first) and not is_definite_absence(second):
            mark_native_fallback(second, native_url)
        raise (second if not is_definite_absence(second) else first)
    return data, name, ctype, True


# ---------------------------------------------------------------------------
# Hierarchy model — identical semantics to the Structure Regenerator's, so
# the modules scope structures the same way.
# ---------------------------------------------------------------------------
def build_model(rows, source: str) -> dict:
    if not rows:
        raise ValueError("The hierarchy contains no structures.")

    root = rows[0][2][0]
    types, levels, chains = {}, {}, []
    for ctx, type_label, chain in rows:
        chains.append(chain)
        types[ctx] = type_label
        for i, node in enumerate(chain):
            if i == 0:
                continue                      # skip the root
            levels[node] = max(levels.get(node, 0), i + 1)
            types.setdefault(node, "")

    desc_sets = {}
    for chain in chains:
        for i in range(1, len(chain)):
            bucket = desc_sets.setdefault(chain[i], set())
            for j in range(i + 1, len(chain)):
                bucket.add(chain[j])
    desc_count = {ctx: len(desc_sets.get(ctx, ())) for ctx in levels}

    return {"root": root, "chains": chains, "levels": levels,
            "types": types, "desc_count": desc_count, "source": source}


def resolve_targets(model: dict, mode: str, parents) -> set:
    """mode "all" → every node; mode "selected" → each chosen parent plus
    every structure beneath it in any chain (chain membership)."""
    if mode == "all":
        return set(model["levels"])
    chosen = {p for p in parents if p in model["levels"]}
    targets = set()
    for chain in model["chains"]:
        for i in range(1, len(chain)):
            if chain[i] in chosen:
                targets.update(chain[i:])
                break
    return targets


def split_targets(model: dict, targets):
    """Split scope into (download, excluded_communities, excluded_unknown).
    Communities hold no uploads; chain-only intermediates (type "", never
    seen on the showall listing) are almost certainly communities too."""
    download, comm, unknown = [], [], []
    for ctx in sorted(targets):
        label = model["types"].get(ctx, "")
        if label in EXCLUDED_TYPE_LABELS:
            comm.append(ctx)
        elif not label:
            unknown.append(ctx)
        else:
            download.append(ctx)
    return download, comm, unknown


# ---------------------------------------------------------------------------
# Hierarchy report parsing (input) — same format as the Hierarchy Mapping
# Tool / Structure Regenerator: one sheet, Context ID | Type | Level1…LevelN.
# ---------------------------------------------------------------------------
def parse_report(path: str):
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise ValueError("Couldn't open the workbook ({})."
                         .format(e.__class__.__name__))
    try:
        ws = wb["Hierarchy"] if "Hierarchy" in wb.sheetnames else wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if (not header or len(header) < 3
                or not str(header[0] or "").lower().startswith("context")
                or not str(header[2] or "").lower().startswith("level")):
            raise ValueError(
                "That workbook doesn't look like a hierarchy report "
                "(expected headers: Context ID | Type | Level1…).")

        rows = []
        for raw in rows_iter:
            if not raw:
                continue
            ctx = str(raw[0] or "").strip()
            if not ctx:
                continue
            type_label = str(raw[1] or "").strip() if len(raw) > 1 else ""
            chain = [str(c).strip() for c in raw[2:]
                     if c is not None and str(c).strip()]
            if len(chain) < 2:                 # need at least root + itself
                continue
            rows.append((ctx, type_label, chain))
    finally:
        wb.close()

    if not rows:
        raise ValueError("No structure rows found in that report.")
    return rows


def scan_reports(folder: str, pattern: str):
    """List matching workbooks in `folder`, newest first."""
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


def write_hierarchy_workbook(path, rows, max_levels, header_rgb):
    """Same layout as the Hierarchy Mapping Tool's report — interchangeable."""
    from openpyxl import Workbook

    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Hierarchy"
    headers = ["Context ID", "Type"] + \
              ["Level{}".format(i + 1) for i in range(max_levels)]
    body = [[ctx, type_label] + chain + [""] * (max_levels - len(chain))
            for ctx, type_label, chain in sorted(rows,
                                                 key=lambda r: (r[2], r[0]))]
    _write_sheet(ws, headers, body, [32, 22] + [32] * max_levels, (), st)
    wb.save(path)


# Per-structure download report — one row per download attempt.
# Columns are located by NAME downstream (dc_image_describer._header_map),
# so appending is safe and reordering is not. Every report_rows.append()
# tuple must stay the same arity as this list.
REPORT_HEADERS = ["Order", "Article ID", "Title", "Record State",
                  "File Kind", "Visibility", "Version", "Version Date",
                  "Original Filename", "Saved Filename", "Size", "Status",
                  "File URL", "Public Record URL", "Admin Record URL",
                  "Time", "Content Type",
                  # Appended 2026-09-09. "Visibility" says whether a file is
                  # shown on the site; "Access" says whether it may leave the
                  # institution, which is a different question and was
                  # previously not asked at all. "Plan" is filled in only on
                  # a "Not attempted" row and tells the re-run what the
                  # original run would have fetched for that record.
                  "Access", "Type / Release Option", "Embargo", "Plan",
                  # Appended v1.34.1. How the bytes of a downloaded file
                  # actually arrived: "Chrome", "network layer", or
                  # "network layer — Chrome delivered nothing". Blank on
                  # every row that downloaded nothing. On 2026-09-30 the one
                  # file that came the second way said only "Downloaded".
                  "Fetched via"]
REPORT_WIDTHS = [8, 12, 44, 18, 12, 11, 11, 26, 34, 52, 12, 30,
                 72, 60, 60, 10, 28,
                 12, 38, 14, 44, 30]


def fetched_via(got, fetcher) -> str:
    """The "Fetched via" cell for a file that came down."""
    if getattr(got, "via_network", False):
        return "network layer — Chrome delivered nothing"
    if getattr(fetcher, "path", "") == FETCH_BROWSER:
        return "Chrome"
    return "network layer"
REPORT_LINK_COLS = (13, 14, 15)
# Derived, never written by hand: the stop handler counts finished rows out
# of a partial report and needs these two columns. A hand-typed 11 goes
# wrong the moment a column is inserted, and it goes wrong quietly - it
# would just count nothing and report a structure as having downloaded no
# files. _verify_file_downloader.py asserts both against the header names.
COL_STATUS = REPORT_HEADERS.index("Status")
COL_VISIBILITY = REPORT_HEADERS.index("Visibility")
COL_ACCESS = REPORT_HEADERS.index("Access")


def _check_arity(kind, headers, rows):
    """Refuse to write a sheet whose rows do not match its headers.

    Appending a column means touching every append site, and a missed one
    does not raise — openpyxl writes the short row happily and every value
    after the gap lands under the wrong heading. That is a silent, plausible
    report, which is the worst kind of wrong output this module can produce.
    So the writers check, and say which row and by how much.
    """
    want = len(headers)
    for i, row in enumerate(rows, start=1):
        if len(row) != want:
            raise ValueError(
                "{} row {} has {} value(s) but there are {} columns — a "
                "column was added or removed without updating every row "
                "that is built. Row begins: {!r}".format(
                    kind, i, len(row), want, tuple(row)[:4]))


def write_structure_report(path, rows, header_rgb):
    from openpyxl import Workbook

    _check_arity("download report", REPORT_HEADERS, rows)
    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Downloads"
    _write_sheet(ws, REPORT_HEADERS, rows, REPORT_WIDTHS,
                 REPORT_LINK_COLS, st)
    wb.save(path)


# Master run log — one row per structure.
MASTER_HEADERS = ["Order", "Context ID", "Type", "Records", "Files",
                  "Hidden Files", "Warnings", "Status", "Time", "Report"]
MASTER_WIDTHS = [8, 32, 12, 10, 10, 12, 10, 44, 10, 54]


PLAN_SHEET = "Job"

# The verification audit trail. One row per prompt, in the master log's
# second sheet. "Files since previous" is the number that makes the record
# useful: it is the work that ran unattended between one confirmation and
# the next, which is the question anyone auditing the arrangement would
# ask first.
VERIFY_SHEET = "Verifications"
VERIFY_HEADERS = ["#", "When", "Structure", "At file request",
                  "Files since previous prompt or run start",
                  "Cleared in (s)", "Outcome",
                  "Observed"]
VERIFY_WIDTHS = [5, 21, 32, 16, 20, 15, 22, 48]

# The checks made before any request (v1.34.1). 2026-09-30: the preflight's
# result was in the run log and nowhere in the master log, so the one
# record that outlives the page could not show it had run. Each row says
# what was OBSERVED, including when the check passed.
CHECKS_SHEET = "Checks"
CHECKS_HEADERS = ["Check", "Result", "Observed", "When"]
CHECKS_WIDTHS = [22, 10, 96, 21]

# ---------------------------------------------------------------------------
# SESSION LIMITS
#
# What the server was believed to be rationing was NOT KNOWN. Three days of
# measured runs from inside one institution's network:
#
#     day 1   379 files    322 MB   across four runs, never blocked
#     day 2   158 files    766 MB   blocked
#     day 3   167 files    442 MB   blocked
#
# A file count did not predict it: 176 files went through in one run on
# day 1 and 158 blocked on day 2. Megabytes fit these three better — 322
# through, 442 and 766 blocked — but three points and one address is not a
# model, and a run from outside that network blocked at 99 files / 200 MB,
# lower in both units. Something about the address mattered too.
#
# So the module does not pick a theory. It carries BOTH limits, stops a
# session on whichever binds first, and lets the operator change either one
# in the plan. That is the operator's design (v1.32) and it has three
# properties worth naming:
#
#   - it ships without the question being settled;
#   - a limit the operator edits is an EXPERIMENT, and the plan workbook is
#     its record, so a test is a file to open rather than a dozen controls
#     to re-enter by hand;
#   - a third variable later is a row in this tuple, not a new column
#     anywhere.
#
# Defaults are 85% of the LOWEST observed block in each unit — 85% because
# a session sized at the ceiling ends on a wall of refusals every time,
# lowest because being early costs a session and being late costs an
# allowance.
# v1.34: BOTH DEFAULTS ARE 0 — no limit. The numbers below them were 85%
# of the lowest block observed in each unit, sized for a download ceiling
# that turned out not to exist (2026-09-14: it was a verification prompt,
# which now paces the work by itself). They are kept as history, and as
# the values to type in if a limit is ever wanted again.
SESSION_LIMIT_FILES = 0            # was 134: 85% of 158 (day 2 above)
SESSION_LIMIT_MB = 0               # was 375: 85% of 442 MB (day 3 above)

SESSION_LIMITS = (
    # key         label                     unit    default
    ("max_files", "Files per session",      "files", SESSION_LIMIT_FILES),
    ("max_mb",    "Megabytes per session",  "MB",    SESSION_LIMIT_MB),
)

_LIMIT_BASIS = {
    # History, not advice: see SESSION_LIMIT_FILES. 0 means no limit.
    "max_files": "0 = no limit (the default since v1.34). Formerly 134: 85% of 158 — the fewest files served before a block "
                 "in one measured run. Not known to be the unit the "
                 "server counts: 176 files went through unblocked in another.",
    "max_mb": "0 = no limit (the default since v1.34). Formerly 375: 85% of 442 MB — the least volume served before a block "
              "in one measured run. Fits those runs better than a "
              "file count does, but is not proven to be the unit either.",
}


def session_limits(observed=None, chosen=None):
    """The caps for the next session, with where each number came from.

    Three sources, narrowest first: the measured defaults above, whatever
    this install has since seen the server do, and whatever the operator
    typed into the plan. Returns a list of dicts so the plan sheet, the
    page and the worker all read the same list and a fourth limit is one
    more entry in SESSION_LIMITS.

    `observed` is {key: value} from a run that actually met a block — the
    self-calibrating half of the agreed architecture. `chosen` is {key:
    value} from the operator and wins outright, including 0, which means
    "do not limit on this".
    """
    observed = observed or {}
    chosen = chosen or {}
    out = []
    for key, label, unit, default in SESSION_LIMITS:
        value, basis = default, _LIMIT_BASIS.get(key, "")
        if observed.get(key):
            value = max(1, int(observed[key] * 0.85))
            basis = ("85% of the {} {} served before this install last saw "
                     "the server start refusing".format(observed[key], unit))
        if key in chosen and chosen[key] is not None:
            value = chosen[key]
            basis = "set in the plan" + (
                " — no limit on this" if not value else "")
        out.append({"key": key, "label": label, "unit": unit,
                    "value": value, "basis": basis})
    return out


def limit_values(limits):
    """{key: value} from the list session_limits() returns."""
    return {d["key"]: d["value"] for d in (limits or [])}


class SessionBudget:
    """Spends a session's allowance, in both units, as files arrive.

    The plan-time split is an ESTIMATE — at plan time no row knows its own
    size, because knowing would mean fetching it. This is the half that is
    not an estimate: it counts what actually came down and ends the session
    when either limit is reached, so a plan built on an average that turned
    out wrong costs a short session rather than a block.
    """

    def __init__(self, max_files=0, max_mb=0):
        self.max_files = max_files or 0
        self.max_mb = max_mb or 0
        self.files = 0
        self.bytes = 0

    @property
    def mb(self):
        return self.bytes / 1048576.0

    def spend(self, nbytes):
        self.files += 1
        self.bytes += max(0, int(nbytes or 0))

    def exhausted(self):
        """The key of the limit that is spent, or "" while both have room."""
        if self.max_files and self.files >= self.max_files:
            return "max_files"
        if self.max_mb and self.mb >= self.max_mb:
            return "max_mb"
        return ""

    def reason(self):
        key = self.exhausted()
        if key == "max_files":
            return ("this session's limit of {} file(s) is reached ({:.0f} "
                    "MB fetched)".format(self.max_files, self.mb))
        if key == "max_mb":
            return ("this session's limit of {} MB is reached ({} file(s) "
                    "fetched, {:.1f} MB)".format(
                        self.max_mb, self.files, self.mb))
        return ""


def plan_rows(spec, counts):
    """The Job sheet: what was asked for, and what the inventory found.

    Two columns, label and value, because this sheet is read by a person
    deciding how many nights the work will take — not parsed by anything.
    The one thing the code reads back is the job spec string, which is the
    same `Plan` token every un-reached record row already carries.
    """
    out = [("Job spec", plan_spec(spec)),
           ("Taken", time.strftime("%Y-%m-%d %H:%M:%S"))]
    for label, key in (("Scope", "scope"), ("Structures", "structures")):
        if spec.get(key):
            out.append((label, str(spec[key])))
    out += [
        ("File kinds", ", ".join(k for k in ("primary", "supp", "native")
                                 if spec.get(k)) or "none"),
        ("Versions", str(spec.get("versions", "current"))),
        ("Visibility", ", ".join(k for k in ("public", "hidden")
                                 if spec.get(k)) or "none"),
        ("Record states", ", ".join(k for k in ("published", "unpublished")
                                    if spec.get(k)) or "none"),
        ("Embargo lookup", "yes" if spec.get("embargo") else "no"),
        ("", ""),
        ("Records", counts.get("records", 0)),
        ("Files found", counts.get("files", 0)),
        ("Access-restricted", counts.get("restricted", 0)),
        ("Access unknown", counts.get("unknown", 0)),
        ("", ""),
    ]
    # The limits table. Editable: a session reads these back, so changing a
    # number here changes the next session and records that it was changed.
    out.append(("SESSION LIMITS — edit either value", ""))
    for d in counts.get("limits") or session_limits():
        out.append(("{} ({})".format(d["label"], d["unit"]),
                    d["value"] if d["value"] else "no limit"))
        out.append(("    why", d["basis"]))
    out += [
        ("", ""),
        ("Sessions needed", counts.get("sessions", 0)),
        ("Average file size seen", counts.get("avg_mb") and
         "{:.2f} MB".format(counts["avg_mb"]) or "not yet measured"),
        ("How the split was sized", counts.get("basis", "")),
    ]
    return [(str(a), b) for a, b in out]


def suggest_session_size(n_files, limits=None, avg_mb=0.0):
    """How many files a session takes, and how the number was arrived at.

    Both limits apply and the SMALLER wins, because the module does not
    know which unit the server counts — see SESSION_LIMITS. The megabyte
    limit can only be turned into a row count through an average file
    size, so when no average is known that limit cannot bind at plan time
    and the basis says so rather than implying the split honoured it.
    SessionBudget enforces it during the run either way.

    Returns (size, sessions, basis). size 0 means no limit binds: one
    session takes everything, which is what a structure smaller than an
    allowance wants.
    """
    vals = limit_values(limits or session_limits())
    max_files = vals.get("max_files") or 0
    max_mb = vals.get("max_mb") or 0

    candidates = []
    if max_files:
        candidates.append((max_files,
                           "the limit of {} file(s) per session"
                           .format(max_files)))
    if max_mb and avg_mb > 0:
        by_mb = max(1, int(max_mb / avg_mb))
        candidates.append((by_mb,
                           "the limit of {} MB per session, at the {:.2f} MB "
                           "average file size measured for this job "
                           "({} file(s))".format(max_mb, avg_mb, by_mb)))
    if not candidates:
        note = ("no limit binds at plan time, so this is one session")
        if max_mb and avg_mb <= 0:
            note = ("the {} MB limit cannot be turned into a row count until "
                    "a run has measured an average file size, so the split "
                    "is one session; the limit still ends the session during "
                    "the run".format(max_mb))
        return 0, 1, note

    size, why = min(candidates, key=lambda c: c[0])
    sessions = max(1, -(-int(n_files or 0) // size)) if n_files else 1
    if len(candidates) > 1:
        why += " — the smaller of the two limits, because which unit the "\
               "server counts is not known"
    return size, sessions, why


def average_file_mb(rows):
    """Mean size of the rows that actually downloaded, in MB. 0.0 if none.

    Taken from evidence rather than assumed: the ETD collection averages
    ~2.6 MB a file and a 2017 faculty series ~0.85, so a single figure
    baked into the module would be wrong for one of them by 3x.
    """
    sizes = []
    for r in rows or []:
        try:
            n = float(r.get("size") if isinstance(r, dict) else r)
        except (TypeError, ValueError):
            continue
        if n > 0:
            sizes.append(n)
    if not sizes:
        return 0.0
    return (sum(sizes) / len(sizes)) / 1048576.0


def write_plan_workbook(path, rows, spec, counts, header_rgb):
    """The inventory and the job that produced it, in one file.

    One file because the spec must travel with the evidence it describes:
    a session that re-enters the controls by hand can quietly become a
    different job from the one that was planned, and nothing would notice.
    """
    from openpyxl import Workbook

    _check_arity("download report", REPORT_HEADERS, rows)
    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    _write_sheet(ws, REPORT_HEADERS, rows, REPORT_WIDTHS,
                 REPORT_LINK_COLS, st)
    job = wb.create_sheet(PLAN_SHEET)
    _write_sheet(job, ["Item", "Value"], plan_rows(spec, counts),
                 [34, 64], (), st)
    wb.save(path)


def write_master_workbook(path, rows, header_rgb, verify_rows=None,
                          verify_note="", check_rows=None):
    """The run log, and — when the site asked — the record of every prompt.

    The second sheet is the audit trail, and it is the point of the
    feature rather than a by-product of it: it shows that every prompt
    reached a person and how long each took, from the module's own output,
    without reference to anything that cannot be reproduced here.

    The sheet is written whenever there is a note to make, including when
    the note is that nothing was asked — because "no prompts" and "never
    checked" are different claims and a blank sheet reads as the first.
    """
    from openpyxl import Workbook

    _check_arity("master log", MASTER_HEADERS, rows)
    verify_rows = list(verify_rows or ())
    _check_arity("verification log", VERIFY_HEADERS, verify_rows)
    st = _styles(header_rgb)
    wb = Workbook()
    ws = wb.active
    ws.title = "Run Log"
    _write_sheet(ws, MASTER_HEADERS, rows, MASTER_WIDTHS, (), st)
    if verify_rows or verify_note:
        vs = wb.create_sheet(VERIFY_SHEET)
        _write_sheet(vs, VERIFY_HEADERS, verify_rows, VERIFY_WIDTHS, (), st)
        if verify_note:
            vs.cell(row=len(verify_rows) + 3, column=1, value=verify_note)
    if check_rows is not None:
        check_rows = list(check_rows)
        _check_arity("checks", CHECKS_HEADERS, check_rows)
        cs = wb.create_sheet(CHECKS_SHEET)
        _write_sheet(cs, CHECKS_HEADERS, check_rows, CHECKS_WIDTHS, (), st)
        if not check_rows:
            cs.cell(row=3, column=1,
                    value="No check was recorded before this run.")
    wb.save(path)


# ---------------------------------------------------------------------------
# State machine — the frontend polls GET /api/state every 1.5 s. "hier"
# carries a version counter; when it changes the page refetches
# GET /api/hierarchy to rebuild the parent-selection list.
# ---------------------------------------------------------------------------
STATE = {
    "phase": "idle",   # idle | starting | mapping | downloading | writing | done | error
    "paused": False,
    "progress": {"current": 0, "total": 0, "msg": ""},
    "log": [],
    "last_file": None,      # master log workbook
    "report_file": None,    # hierarchy report written by a fresh map
    "run_dir": None,        # DC_FileDownloads_<stamp> folder
    "summary": "",
    "hier": {"loaded": False, "source": "", "count": 0,
             "max_level": 0, "version": 0},
    # A verification hold in progress. Separate from "paused", which is
    # the operator's own pause: this one is the run waiting to be told a
    # person is here, and it must read as that and not as an error.
    "verify": {"holding": False, "left": 0, "prompts": 0},
    # Claimed between the endpoint accepting a run and the worker thread
    # actually setting a phase. Without it there is a window in which no
    # phase says "running" and a second request sails straight through —
    # see _claim_run() for what that cost.
    "claimed": False,
    # Which process this is, for the cross-process run lock: a copy is
    # believed to hold a run only if it answers with this token.
    "run_lock_token": "",
    # Bumped by clear_for_new_run(), so every open copy of the page wipes
    # its own view of the log rather than only the tab that asked.
    "cleared": 0,
    # The page's form, as the page last sent it (v1.34.2) — see
    # validate_form_state(). A reopened tab is filled from this.
    "form": None,
}
LOCK = threading.Lock()
MODEL = None           # hierarchy model
DRIVER = None          # one driver for the module's lifetime
DRIVER_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()

RUNNING_PHASES = ("starting", "mapping", "downloading", "writing")


# ---------------------------------------------------------------------------
# One run per Chrome, across processes (v1.34.1).
#
# v1.34.1: the module's tab was closed during a run, and opening
# the module again from the shell started a SECOND copy on the next port
# instead of returning to the first. The one-run claim above lives in each
# process's memory, so the idle copy would have accepted Start — and both
# copies drive the same Chrome and each sets its download folder, so the
# second run would have taken the first one's downloads. The 2026-09-09
# double run, across processes rather than threads.
#
# So a run is also claimed in a small file named for the Chrome debugging
# address, created atomically (O_CREAT | O_EXCL) and removed when the run
# ends. A file left behind by a process that died is judged stale by ASKING
# ITS HOLDER: the lock records the holder's port and a per-process token,
# and only a copy that answers /api/state with that token and a running
# phase is still running. That works on Windows as well as POSIX, where
# signalling a process id to test it does not.
#
# The residual window, said out loud: two copies that find the SAME stale
# file within the same few milliseconds could both remove it. The content
# is re-read immediately before removal to narrow that; it is not closed.
# ---------------------------------------------------------------------------
RUN_LOCK_DIR = None          # None = the system temp folder; tests set it
RUN_LOCK_FRESH_SECONDS = 5.0 # an unreadable lock this new is being written
RUN_LOCK_TOKEN = os.urandom(16).hex()
STATE["run_lock_token"] = RUN_LOCK_TOKEN


def run_lock_path(debugger_address: str) -> str:
    """The lock file for runs driving the Chrome at this address."""
    safe = re.sub(r"[^A-Za-z0-9.-]", "_", debugger_address or "chrome")
    return os.path.join(RUN_LOCK_DIR or tempfile.gettempdir(),
                        "dc-admin-suite-run-{}.lock".format(safe))


def _read_run_lock(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def holder_is_running(info: dict, timeout: float = 2.0) -> bool:
    """Is the copy that wrote this lock still running a job?

    Asked of the holder itself, over its own local page, and believed only
    when it answers with the token the lock carries AND a running phase. An
    idle copy that happens to own the port now, a program that is not this
    module, or nothing at all: all mean the lock is stale.
    """
    try:
        port = int(info.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    token = info.get("token") or ""
    if not port or not token:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:{}/api/state".format(port),
                         timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
    except Exception:                                # noqa: BLE001
        return False
    if not isinstance(data, dict) or data.get("run_lock_token") != token:
        return False
    return bool(data.get("phase") in RUNNING_PHASES or data.get("claimed"))


def claim_run_lock(debugger_address: str, port: int,
                   token: str = None, holder_check=None):
    """Claim the run for this Chrome. Returns (True, {}) or (False, holder).

    `holder` is what the lock says about the copy that has it — its port
    and start time — so the refusal can say where it is.
    """
    token = token or RUN_LOCK_TOKEN
    holder_check = holder_check or holder_is_running
    path = run_lock_path(debugger_address)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = _read_run_lock(path)
            if not info:
                try:
                    age = time.time() - os.path.getmtime(path)
                except OSError:
                    continue                 # gone already; try again
                if age < RUN_LOCK_FRESH_SECONDS:
                    return False, {}         # another copy is writing it
            elif holder_check(info):
                return False, info
            if _read_run_lock(path) != info:
                return False, _read_run_lock(path)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                return False, info
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"port": port, "token": token, "pid": os.getpid(),
                       "started": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
        return True, {}
    return False, _read_run_lock(path)


def release_run_lock(debugger_address: str, token: str = None) -> None:
    """Remove the lock if, and only if, this copy holds it."""
    token = token or RUN_LOCK_TOKEN
    path = run_lock_path(debugger_address)
    if _read_run_lock(path).get("token") == token:
        try:
            os.remove(path)
        except OSError:
            pass


def run_lock_refusal(holder: dict) -> str:
    """What to tell the operator when another copy has the run."""
    port = holder.get("port")
    where = ("Open it at http://127.0.0.1:{}".format(port) if port
             else "It is starting up")
    since = (" (started {})".format(holder["started"])
             if holder.get("started") else "")
    return ("Another copy of this module is already running a job on this "
            "Chrome{}. {} — a second run would take over its downloads."
            .format(since, where))


class StopRequested(Exception):
    """Raised inside a worker when the user clicks Stop."""


class SessionFull(StopRequested):
    """This session has spent the allowance the plan gave it. Not an error.

    Subclasses StopRequested for the same reason ServerRefusing does: every
    existing handler flushes the in-flight rows, writes the un-reached
    records with their plans and lands a _PARTIAL report, so the next
    session resumes from it. Only the wording differs, and it has to — a
    session that finished its share exactly as planned must not be reported
    as one that was interrupted.

    The distinction from ServerRefusing matters to whoever reads the log:
    this one means the plan worked and the next session can start when the
    allowance refreshes. ServerRefusing means the plan was too big.
    """


class NotVerified(StopRequested):
    """The run stopped because nobody confirmed a person was present.

    The fourth stop reason, beside Stop, ServerRefusing and SessionFull,
    and it exists so that this outcome never reads as a failure. Nothing
    went wrong: the site asked whether somebody is here, the run stopped
    and waited, and the window passed without an answer. The files were
    not requested, so nothing is known to be wrong with them.

    Subclasses StopRequested for the reason the other two do — every
    existing handler already flushes the in-flight rows, records the
    records never reached with their plans, lands a _PARTIAL report and
    leaves the job resumable. Only the wording differs, and it has to.

    It is also the compliance guarantee, stated in code: the run does not
    resume until a person clears the prompt, and if none does, it ends.
    """


class ServerRefusing(StopRequested):
    """The server is refusing every file — stop rather than grind.

    Subclasses StopRequested deliberately, so every existing handler does
    the right thing without being taught about it: the in-flight rows are
    flushed, the records never reached are written with their plans, the
    _PARTIAL report lands, and the run is resumable. Only the summary needs
    to know the difference.

    Measured (v1.29). Digital Commons imposes a ceiling on file
    downloads per account or address — 99 from outside one institution's
    network, 156 from inside it — that
    a fresh login does not reset, that ~35 minutes of quiet does not
    reset, and that pacing cannot avoid. After it, every file request is
    refused with HTTP 403. That run then spent 24 more minutes and 285
    more requests being told no; on an overnight run it would be hours.
    """



HEARTBEAT_SECONDS = 300.0   # v1.34: a quiet log looks like a stall


# This module's half of DC-LOG (below): where a run's whole log is saved,
# and what Clear puts back besides the log, status and summary. The
# .txt goes beside the master log (save_run_log, both workers).
RUN_LOG_NAME = "DC_FileDownload_Log_{}.txt"
CLEAR_RESETS = {"last_file": None, "report_file": None, "run_dir": None,
                "paused": False,
                "verify": {"holding": False, "left": 0, "prompts": 0}}


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


def log(msg: str):
    line = time.strftime("[%H:%M:%S] ") + msg
    _LAST_LOG[0] = time.time()
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


# The page's form fields, by element id (v1.34.2). Kept by the module so a
# reopened tab is filled in as it was left. 2026-10-01: the tab was closed
# and reopened during a run, the shell returned to the running module —
# and the page came back with "Save reports to" blank and the mode back on
# Start, so the next run would have written its reports somewhere else
# without anything saying so. A silent change of destination.
FORM_FIELDS = (
    "jobnew", "jobresume", "repdir", "modeall", "modesel",
    "kprimary", "ksupp", "knative", "kinventory", "kembargo",
    "vpublic", "vhidden", "vercur", "verall", "rpub", "runpub",
    "fetchbrowser", "fetchnetwork", "verifywin", "stalllimit",
    "sessioncap", "sessionmb", "dldir", "reqdelay", "stopblocked",
    "pagedelay", "cooldowns", "rptdir", "retryunconfirmed", "reconcile",
    "retryrpt",
)
# The page's other controls, which are NOT kept, and why: the reports found by a scan, the hierarchy filter, the log copy box.
# Every control on the page is in one list or the other (_verify_pages.py).
FORM_NOT_KEPT = (
    "reports", "filter", "logfull",
)
def set_state(**kw):
    with LOCK:
        STATE.update(kw)


_LAST_LOG = [0.0]


def set_progress(current, total, msg):
    with LOCK:
        STATE["progress"] = {"current": current, "total": total, "msg": msg}
        _LAST_PROGRESS[:] = [current, total]
    # The heartbeat lives HERE because both workers report every record
    # through this one function (v1.34). A long stretch of quiet absences
    # otherwise reads as a frozen module — "nothing in the downloads yet".
    if _LAST_LOG[0] and time.time() - _LAST_LOG[0] >= HEARTBEAT_SECONDS:
        log("Still working — {}".format(msg))


def set_progress_msg(msg):
    """Change the progress TEXT, keeping the counts already showing.

    A ten-minute cooldown happens deep inside the fetch, so the status line
    kept whatever the item loop last set - which once left the operator
    watching a stale "item 36/38 · ETA --:--" for ten minutes with the
    countdown only in the log. The counts are still true; the sentence is
    what needs to change.
    """
    with LOCK:
        cur, tot = _LAST_PROGRESS
        STATE["progress"] = {"current": cur, "total": tot, "msg": msg}


_LAST_PROGRESS = [0, 0]


def fail(msg: str):
    log("ERROR: " + msg)
    set_state(phase="error", summary=msg, paused=False)


def set_model(model):
    """Install a new hierarchy model and bump the UI's version counter."""
    global MODEL
    with LOCK:
        MODEL = model
        STATE["hier"] = {
            "loaded": True,
            "source": model["source"],
            "count": len(model["levels"]),
            "max_level": max(model["levels"].values()),
            "version": STATE["hier"]["version"] + 1,
        }


def show_verify_hold(holding, left=0.0, prompts=0):
    """Publish a verification hold to the page. Both workers pass this.

    A function rather than a lambda in each worker, because "both workers
    need this" has nine times meant "one worker got it".
    """
    set_state(verify={"holding": bool(holding), "left": int(left),
                      "prompts": int(prompts)})


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


def run_position(structures_done, item_index, item_count):
    """Where the whole run is, as a fraction of the structure count.

    Pulled out as a function because the version inline in the item loop
    passed the structure index as the position, so a single-structure run -
    which is every test run made on 2026-09-08 - showed a full progress bar
    from its first record and stayed there. Four lines inside
    download_worker are unreachable without Chrome and a loaded hierarchy;
    a function is not.
    """
    if item_count <= 0:
        return float(structures_done)
    fraction = min(1.0, max(0.0, item_index / float(item_count)))
    return structures_done + fraction


def eta_text(done, total, t0):
    """Estimate from the average pace so far.

    Renders hours past sixty minutes. The first run long enough to need it
    displayed "ETA 1940:39" on 2026-09-08 - correct, and unreadable.
    """
    if not done:
        return "ETA --:--"
    remaining = int((time.time() - t0) / done * (total - done))
    hours, rest = divmod(remaining, 3600)
    if hours:
        return "ETA {}h {:02d}m".format(hours, rest // 60)
    return "ETA {:02d}:{:02d}".format(*divmod(remaining, 60))


def _attach_or_fail(session):
    """Shared preamble for all workers. Returns a driver or None (failed)."""
    set_progress(0, 0, "Attaching to Chrome…")
    log("Attaching to debug Chrome at {}…".format(
        session["chrome"]["debugger_address"]))
    try:
        driver = get_driver(session)
    except Exception as e:
        # The underlying exception names both versions and the remedy when
        # chromedriver is stale. Reporting only the class name sent the
        # operator to restart a Chrome that was already correct.
        fail("Could not attach to Chrome. {}\n\nIf that names a ChromeDriver "
             "version mismatch, the driver on PATH is stale — Chrome updates "
             "itself and a hand-installed driver does not. Otherwise: start "
             "Chrome in debug mode from the suite Splash/Settings page, log "
             "into Digital Commons, then try again."
             .format(describe_error(e)))
        return None
    log("Attached.")
    return driver


def _primary_rgb(session) -> str:
    return session.get("branding", {}).get("colors", {}) \
                  .get("primary", FALLBACK_BRAND["primary"]).lstrip("#")


# Accumulated slowdown, in seconds, added to REQUEST_DELAY after the server
# has throttled us. Process-global because _open is; reset_rate_state() is
# called once at the top of every run so one throttled run does not hand its
# penalty to the next.
# Initial value is replaced wholesale by reset_rate_state() at the top of
# every run; _fresh_rate_state() below is the single definition of both.
# Pushback, attributed. A 429 was counted but never ascribed
# to what provoked it, so a lockout mid-run said nothing
# about which pace caused it — which is the whole question
# the split gap asks. Counting them apart is what turns
# running faster into an experiment.
_RATE_STATE = {}
_RATE_LOCK = threading.Lock()

_RATE_LOCK = threading.Lock()

# Set by a worker so the low-level fetch can say what it is waiting for.
# Module-level for the same reason _open is; cleared with the rest.
_NOTE_HOOK = [None]


_STOP_HOOK = [None]


def set_note_hook(fn) -> None:
    _NOTE_HOOK[0] = fn


def set_stop_hook(fn) -> None:
    """Lets a long backoff notice that Stop was pressed.

    Without it a 120s wait makes Stop look broken for two minutes. The
    fetch does not raise StopRequested itself - that would be caught by
    the item loop's handler and written out as a failed row - it just
    stops waiting, and the loop's own check_pause_stop() ends the run
    cleanly on the next pass.
    """
    _STOP_HOOK[0] = fn


def _stopping() -> bool:
    fn = _STOP_HOOK[0]
    try:
        return bool(fn()) if fn is not None else False
    except Exception:
        return False


def _sleep(seconds: float) -> bool:
    """Sleep in slices. Returns False if a stop cut the wait short."""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return True
        if _stopping():
            return False
        time.sleep(min(1.0, left))


def _note(text: str) -> None:
    fn = _NOTE_HOOK[0]
    if fn is not None:
        try:
            fn(text)
        except Exception:
            pass                      # a log must never break a download


def _fresh_rate_state() -> dict:
    """The rate state a run starts from.

    One definition, used both to create the state and to reset it. Written
    this way after the attributed-pushback counters were added and
    reset_rate_state() was not updated: the per-kind counts then carried
    across runs inside one process, so the second run of a session would
    report the first run's lockouts as its own. Since the whole point of
    attributing pushback is to read one run's numbers, a counter that
    survives the run is worse than no counter. A hand-maintained reset is
    how that happened, so there is no longer a hand-maintained reset.
    """
    return {"extra": 0.0, "hits": 0, "cooldowns": 0,
            "budget": COOLDOWN_MAX_WAITS, "base": REQUEST_DELAY,
            "page_base": PAGE_DELAY, "hits_file": 0, "hits_page": 0}


def reset_rate_state() -> None:
    with _RATE_LOCK:
        _RATE_STATE.clear()
        _RATE_STATE.update(_fresh_rate_state())


_RATE_STATE.update(_fresh_rate_state())


def set_request_delay(seconds: float) -> None:
    """The gap between requests for this run.

    A run-scoped setting rather than a constant because the pacing question
    is open and unresolved: DC's lockout tripped at records 26, 33, 28, 30
    and 36 on 2026-09-08, all at 0.3s, which does not distinguish a fixed
    request budget from a bucket that refills while idle. Settling it means
    running the same structure at a different pace, and answering it by
    hand-editing a constant would leave the resulting report traceable to no
    commit at all - which is the one thing a test report must not be.
    """
    with _RATE_LOCK:
        _RATE_STATE["base"] = max(0.0, min(REQUEST_DELAY_MAX, float(seconds)))


def request_delay() -> float:
    with _RATE_LOCK:
        return _RATE_STATE["base"] + _RATE_STATE["extra"]


def set_page_delay(seconds: float) -> None:
    """The gap before an admin page request, for this run."""
    with _RATE_LOCK:
        _RATE_STATE["page_base"] = max(
            0.0, min(REQUEST_DELAY_MAX, float(seconds)))


def page_delay() -> float:
    """Admin pages carry the same accumulated penalty as downloads.

    If the server has pushed back, it has pushed back — slowing files while
    hammering pages would be reading the penalty as a statement about one
    request kind, which is exactly the thing this run is trying to find out
    rather than assume.
    """
    with _RATE_LOCK:
        return _RATE_STATE["page_base"] + _RATE_STATE["extra"]


def set_cooldown_budget(n: int) -> None:
    """How many server lockouts this run may sit out. 0 never waits.

    Clamped at both ends. The upper clamp is not decoration: the budget is
    what ends _open's retry loop, so an unbounded budget against a server
    that never yields is an unbounded loop. Found by planting exactly that
    violation, which hung the verification suite instead of failing it.
    """
    with _RATE_LOCK:
        _RATE_STATE["budget"] = max(0, min(COOLDOWN_HARD_MAX, int(n)))


def _claim_cooldown_wait() -> bool:
    """Spend one of the run's cooldown waits, if any are left."""
    with _RATE_LOCK:
        if _RATE_STATE["cooldowns"] >= _RATE_STATE["budget"]:
            return False
        _RATE_STATE["cooldowns"] += 1
        return True


def _note_rate_limit(kind: str = "file") -> None:
    """Record pushback, and what kind of request drew it.

    The accumulated slowdown stays global: a lockout is a statement about
    the whole session, not about one request kind, and every pending
    request shares its expiry.
    """
    with _RATE_LOCK:
        _RATE_STATE["hits"] += 1
        _RATE_STATE["hits_page" if kind == "page" else "hits_file"] += 1
        _RATE_STATE["extra"] = min(_RATE_STATE["extra"] + RATE_DELAY_STEP,
                                   RATE_DELAY_CAP)


def rate_state() -> dict:
    with _RATE_LOCK:
        return dict(_RATE_STATE)


def _throttle(kind: str = "file"):
    """Wait before a request. `kind` is "file" for a download, "page" for
    admin HTML — a listing, a revisions table, a supplemental page."""
    delay = page_delay() if kind == "page" else request_delay()
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# The fetch path
#
# One file, however it arrived. The two paths differ in a way that reaches
# the caller: urllib hands over bytes in memory, the browser hands over a
# file already on disk. Wrapping both in one object is what lets a single
# success branch serve both workers — and the standing rule in this module
# is that logic two workers need becomes one object they both hold, because
# nine separate defects have come from fixing something in download_worker
# and not in retry_worker.
#
# `head` rather than the whole file: build_filename() sniffs an extension
# from the leading bytes, and reading a 200 MB thesis into memory to look
# at its first four kilobytes is the kind of thing the browser path exists
# to avoid.
# ---------------------------------------------------------------------------
class Fetched:
    """One file that came down. Either bytes in hand, or a file on disk."""

    def __init__(self, data=None, path="", name="", ctype="",
                 used_native=False, prompt=None):
        if data is None and not path:
            # Not a defensive nicety. A helper that returns something empty
            # rather than raising is how a fetch that quietly failed gets
            # written to disk as a zero-byte file with a real record's name
            # on it.
            raise ValueError("a Fetched must carry either bytes or a path")
        self.data = data
        self.path = path
        self.name = name
        self.ctype = ctype
        self.used_native = used_native
        # A verification prompt that appeared during THIS fetch and was
        # answered before it could stop anything. Carried rather than
        # discarded: the run still has to record that it happened, and
        # the first live run proved that a prompt handled well is
        # exactly the kind that goes unrecorded.
        self.prompt = prompt

    @property
    def nbytes(self):
        if self.data is not None:
            return len(self.data)
        return os.path.getsize(self.path)

    def head(self, n=4096):
        """The leading bytes, for sniffing an extension."""
        if self.data is not None:
            return self.data[:n]
        with open(self.path, "rb") as fh:
            return fh.read(n)

    def save_as(self, dest):
        """Put the file at `dest`. Returns the path it ended up at."""
        if self.data is not None:
            Path(dest).write_bytes(self.data)
            return dest
        try:
            os.replace(self.path, dest)
        except OSError:
            # Different filesystem — the incoming folder normally shares
            # one with the destination, but a destination on another
            # volume is exactly the overnight arrangement (downloads to an
            # external disk), so this is a real path, not a theoretical
            # one.
            shutil.move(self.path, dest)
        self.path = dest
        return dest


class Ticker:
    """When a long wait should say something — ONE object, for holds and
    transfers alike (v1.34), so the two cannot drift into different
    cadences or different ideas of "long"."""

    def __init__(self, every=PROGRESS_TICK):
        self.every = every
        self.last = 0.0

    def due(self, elapsed):
        if self.every and elapsed - self.last >= self.every:
            self.last = elapsed
            return True
        return False


def human_size(n):
    """Bytes as a person reads them. Observed sizes only — never totals."""
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("{:.0f} {}" if unit == "bytes" else "{:.2f} {}").format(
                n, unit)
        n /= 1024.0


class DownloadWatch:
    """How the module learns that a browser download has finished.

    An interface with one implementation, because open item 2 is unproven:
    Chrome's own download events would make completion authoritative
    rather than inferred, and only the attribute has been checked, not a
    working listener. When somebody proves a listener delivers, a second
    implementation slots in here and neither worker changes.
    """

    def begin(self):
        """Called immediately before the navigation that starts a file."""
        raise NotImplementedError

    def settle(self, start_within, timeout):
        """Wait for one download. Returns (name, observation).

        `name` is "" when nothing completed, and `observation` then says
        what was seen — never why.
        """
        raise NotImplementedError

    def partials(self):
        """Names of transfers currently in flight or stranded."""
        raise NotImplementedError

    def clear_partials(self):
        """Remove stranded partials. Returns the names removed."""
        raise NotImplementedError


class DirectoryDownloadWatch(DownloadWatch):
    """Completion is a .crdownload → final-name transition.

    Not "a new file appeared", which is what a probe did on 2026-09-14
    before it reported a .DS_Store as a 0.01 MB download that took 39.9
    seconds. Dotfiles are excluded on every read.

    The folder is the module's own: nothing else writes to it, one file is
    fetched at a time, and it is emptied between fetches. That is what
    makes "no .crdownload remains" a sound completion test rather than a
    guess about whose partial it is.
    """

    def __init__(self, folder):
        self.folder = folder
        self._before = {}

    def _snapshot(self):
        out = {}
        try:
            names = os.listdir(self.folder)
        except OSError:
            return out
        for name in names:
            if name.startswith("."):
                continue
            try:
                out[name] = os.path.getsize(os.path.join(self.folder, name))
            except OSError:
                pass
        return out

    def begin(self):
        self._before = self._snapshot()

    def partials(self):
        return sorted(n for n in self._snapshot()
                      if n.endswith(".crdownload"))

    def anything_started(self):
        """Has anything at all appeared since begin()?

        Asked before concluding, from a rendered page, that no file is
        coming. A page and a download are not mutually exclusive, and
        the absence branch must never overrule bytes already on disk.
        """
        return any(n not in self._before for n in self._snapshot())

    def clear_partials(self, settle=PARTIAL_STILL_FOR):
        """Remove STRANDED partials. Returns (removed, still_growing).

        **A partial that is still growing is a live transfer, not a
        stranded one.** On 2026-09-15 this deleted a video that was
        downloading — 1,084 records' worth of run stopped because the
        module threw away its own progress and asked the site again.

        So it looks twice, `settle` seconds apart, and removes only what
        did not change size in between. A file that grew is left alone
        and named, so the caller can wait for it instead.
        """
        first = {n: s for n, s in self._snapshot().items()
                 if n.endswith(".crdownload")}
        if not first:
            return [], []
        time.sleep(settle)
        second = self._snapshot()
        gone, growing = [], []
        for name, size in sorted(first.items()):
            now = second.get(name)
            if now is None:
                # It finished, or Chrome renamed it. Either way it is not
                # ours to delete.
                continue
            if now != size:
                growing.append(name)
                continue
            try:
                os.remove(os.path.join(self.folder, name))
                gone.append(name)
            except OSError:
                pass
        return gone, growing

    def settle(self, start_within=BROWSER_START_WITHIN,
               timeout=BROWSER_SETTLE_TIMEOUT, observe=None,
               stall_limit=BROWSER_STALL_LIMIT, progress=None,
               tick=PROGRESS_TICK):
        """Wait for one download, glancing at the tab while waiting.

        `observe` is asked, about twice a second, whether the tab is
        showing a verification prompt, and that is the whole reason it
        exists — see BROWSER_OBSERVE. Two facts come back on the
        object: `challenge_at`, when one was first seen, and
        `challenge_gone_at`, when it stopped showing. Both are None
        when none appeared, which is the ordinary case.

        v1.34: patience is by PROGRESS. `timeout` None means no total
        ceiling; a transfer that stops growing for `stall_limit` is
        given up. `progress`, when given, is told once a `tick` what has
        arrived so far, and once when growth stops — observed sizes and
        rates only, because Chrome's partial does not carry the total
        and a guessed total is a claim. Stop ends the wait at once.
        """
        t0 = time.time()
        started = ""
        looked = -1.0
        self.challenge_at = None
        self.challenge_gone_at = None
        grown_at, size, last_tick_size = 0.0, -1, 0
        stall_said = False
        ticker = Ticker(tick)
        while True:
            now = self._snapshot()
            new = [n for n in now if n not in self._before]
            for name in new:
                if name.endswith(".crdownload"):
                    started = name
            done = sorted(n for n in new if not n.endswith(".crdownload"))
            if done and not [n for n in now if n.endswith(".crdownload")]:
                extra = ("; {} other file(s) also arrived: {}".format(
                    len(done) - 1, ", ".join(done[1:])) if len(done) > 1
                    else "")
                return done[0], extra
            waited = time.time() - t0
            if started:
                cur = now.get(started, size)
                if cur != size:
                    if size >= 0 and stall_said and progress:
                        progress("{} is growing again — {} so far".format(
                            started[:-len(".crdownload")], human_size(cur)))
                    size, grown_at, stall_said = cur, waited, False
                idle = waited - grown_at
                if progress and ticker.due(waited):
                    per_min = (size - last_tick_size) * 60.0 / ticker.every \
                        if ticker.every else 0
                    last_tick_size = size
                    progress("Still downloading {} — {} so far, {} over the "
                             "last minute (total size not known)".format(
                                 started[:-len(".crdownload")],
                                 human_size(size), human_size(per_min)))
                if stall_limit and progress and not stall_said \
                        and idle >= min(PROGRESS_TICK, stall_limit / 2.0):
                    stall_said = True
                    progress("No growth for {:.0f}s on {} ({} so far) — it "
                             "is given up after {:.0f}s without growth"
                             .format(idle, started[:-len(".crdownload")],
                                     human_size(size), stall_limit))
                if stall_limit and idle >= stall_limit:
                    return "", ("no growth for {:.0f}s: {} ({} had "
                                "arrived)".format(idle, started,
                                                  human_size(size)))
            if _stopping():
                raise StopRequested(
                    "stopped while waiting for a download{}".format(
                        " ({} had arrived)".format(human_size(size))
                        if started else ""))
            if observe is not None and waited - looked >= BROWSER_OBSERVE:
                looked = waited
                if observe():
                    if self.challenge_at is None:
                        self.challenge_at = waited
                    self.challenge_gone_at = None
                    if not started:
                        # Do not sit out the patience below waiting for
                        # a file that cannot arrive until somebody
                        # answers a question. Hand back at once, so the
                        # hold starts while the prompt is still up.
                        return "", "a verification prompt is showing"
                elif self.challenge_at is not None \
                        and self.challenge_gone_at is None:
                    self.challenge_gone_at = waited
            if not started and waited > start_within:
                return "", "nothing began arriving within {:.0f}s".format(
                    start_within)
            if timeout is not None and waited >= timeout:
                break
            time.sleep(BROWSER_POLL)
        return "", ("still in flight when the wait ran out after {:.0f}s: "
                    "{}".format(timeout, started) if started else
                    "nothing completed within {:.0f}s".format(timeout))


class NetworkFetcher:
    """Files over urllib, with the admin session's cookie.

    The original path, kept and selectable. It cannot answer a
    verification prompt — no non-browser client can — so when it meets one
    it says so and the run stops, rather than reading the refusal as a
    missing file. That is the whole reason it is no longer the default.
    """

    path = FETCH_NETWORK
    label = "the network layer"

    def __init__(self, cookie="", incoming_dir=""):
        self.cookie = cookie
        # Where bodies are streamed (v1.34). Empty keeps the old in-memory
        # behaviour, which the suite's older tests rely on.
        self.incoming = incoming_dir

    def open(self, driver=None):
        return self

    def close(self):
        return []

    def use_cookie(self, cookie):
        """The worker re-harvests the session cookie once per structure.

        A method on both fetchers rather than an attribute the worker
        knows about on one of them, so the loop does not have to ask which
        path it is on — the browser's copy does nothing, and correctly so:
        it is navigating the session, not borrowing its cookie.
        """
        self.cookie = cookie

    def fetch(self, url, native_url=""):
        got, name, ctype, used_native = fetch_record_file(
            url, self.cookie, native_url, folder=self.incoming)
        # Decided by what came back, not by whether a folder was offered:
        # a path when the body was streamed, bytes when it was not.
        if isinstance(got, str):
            return Fetched(path=got, name=name, ctype=ctype,
                           used_native=used_native)
        return Fetched(data=got, name=name, ctype=ctype,
                       used_native=used_native)


def _response_is_a_challenge(exc) -> bool:
    """Is this refusal the site asking whether a person is here?

    Two signals, in order of strength. Cloudflare names its own
    mechanism in a response header — `cf-mitigated: challenge` — which
    is the vendor stating it outright and cannot be confused with an
    ordinary refusal. Failing that, the interstitial's own words, which
    are the same ones the tab is read for.

    Asked only of a refusal, never of an answer. A 403 that carries
    neither signal is a plain refusal and stays one.
    """
    try:
        mitigated = (exc.headers.get("cf-mitigated") or "").strip().lower()
    except Exception:
        mitigated = ""
    if "challenge" in mitigated:
        return True
    try:
        body = exc.read(8192).decode("utf-8", "replace").lower()
    except Exception:
        return False
    return any(m in body for m in CHALLENGE_MARKERS)


def ask_the_network(url, cookie, observed="", timeout=FILE_TIMEOUT,
                    folder=""):
    """The browser observed nothing. Ask the path that hears status codes.

    **This is the browser path's one structural blind spot, and it has
    an exit that costs one request.** Driving a real browser buys the
    ability to answer a verification prompt, and pays for it in status
    codes: over urllib a 404 and a 403 are different facts, while
    through Chrome there is no status at all. For most outcomes the tab
    substitutes — a challenge marker, an absence marker, a file landing.
    For a 404 it does not substitute at all: measured (v1.33.9), the
    navigation simply never commits, so there is no page to read, no
    new document, and nothing to distinguish "no such file" from "the
    request went nowhere". Seven days of `Failed:` rows were that.

    So when the browser has observed NOTHING, this asks the other path
    the same question, once, and reports what the server said:

        HTTP 404 / 410        NoFileAvailable        a definite absence
        200 with bytes        Fetched                the file, in hand
        anything else         BrowserFetchFailed     as before, plus
                                                     what urllib got

    A refusal that names a challenge is in that last row, deliberately
    and expensively — see the comment where it is raised. Only the
    browser can be asked to verify, so a challenge met here is reported
    as a second opinion that could not be given, never as a hold.

    Nothing here is a disguise. It is the module's own long-standing
    file path, the operator's own session cookie, the same request the
    browser just made, and the same standing rule applies: a refusal
    this cannot interpret is reported, never worked around.

    Only reached after the browser has failed, so the cost is bounded
    by failures rather than by files — and with natives requested for
    every record, nearly all of those failures are 404s that the run
    currently spends a page-load timeout and a file-arrival wait on
    before writing them down wrongly.

    The bytes are kept when they come. Recording "this file exists but
    was not fetched" would be a row every re-run reads and retries
    through the same blind spot, which is a loop rather than a report.
    """
    if not cookie:
        # Said out loud rather than skipped. A guard that quietly does
        # nothing turns a bug into a mystery, and the way this feature
        # dies in production is a fetcher that was never given the
        # session cookie — which is invisible unless the message says so.
        raise BrowserFetchFailed(
            "{} — and there was no session cookie to ask the network layer "
            "with, so nothing here establishes whether the file exists"
            .format(observed))
    try:
        if folder:
            # Streamed: a file the second opinion finds may be 10 GB.
            path, name, ctype = fetch_file_to(url, cookie, timeout, folder)
            data = None
        else:
            data, name, ctype = fetch_file(url, cookie, timeout)
            path = ""
    except urllib.error.HTTPError as e:
        if e.code in ABSENT_CODES:
            raise NoFileAvailable(
                "the server answered HTTP {} for this file — an answer the "
                "browser cannot hear, read over the network layer"
                .format(e.code))
        if _response_is_a_challenge(e):
            # NOT a hold, and v1.33.9 got this wrong in a way that cost a
            # live run within two hours of shipping.
            #
            # **Nobody can answer this one.** A hold exists so a person
            # can clear a prompt the BROWSER is showing, and the hold
            # watches the tab to know when they have. A challenge served
            # to urllib appears nowhere on the tab, and clearing one in
            # Chrome does not clear it for urllib — the only thing that
            # would is forwarding Chrome's clearance cookie, which is
            # exactly the disguise this project will not build.
            #
            # So raising VerificationRequired here manufactured a hold
            # against the wrong witness: is_challenge() read a tab that
            # had never been challenged and returned False on the first
            # poll, so every such prompt "cleared in 2s" without anyone
            # doing anything, the retry met the same 403, and three of
            # them in a row spent the per-file bound and ended the run as
            # NotVerified. Measured from outside the institution's
            # network (v1.33.10): three prompts
            # in three file requests, no files, 23s to raise each and 2s
            # to "clear" it.
            #
            # What it actually establishes is only that the second
            # opinion could not be given. Reported as that, and nothing
            # more. A challenge the browser IS showing still reaches the
            # hold, from _await, before this function is ever called.
            raise NotConfirmedHere(
                "{} — and the network layer was refused with HTTP {} "
                "carrying a verification challenge, which it cannot "
                "answer, so nothing here establishes whether the file "
                "exists".format(observed, e.code))
        raise BrowserFetchFailed(
            "{} — and the network layer got {} for the same URL"
            .format(observed, describe_error(e)))
    except (LoginRequired, RateLimited, VerificationRequired):
        # Real conditions with real handling elsewhere: a session expiry
        # reaches the worker's _PARTIAL path, and pushback must never be
        # flattened into "the browser could not tell".
        raise
    except Exception as e:                       # noqa: BLE001
        raise BrowserFetchFailed(
            "{} — and the network layer got {} for the same URL"
            .format(observed, describe_error(e)))
    got = (Fetched(path=path, name=name, ctype=ctype) if path
           else Fetched(data=data, name=name, ctype=ctype))
    got.via_network = True
    return got


def is_native_url(url) -> bool:
    """Is this a request for a record's ORIGINAL upload (a native)?"""
    return "/type/native/" in (url or "")


def native_status(url, cookie, timeout=FILE_TIMEOUT):
    """Ask whether a native exists — headers only, the body is never read.

    Returns (answer, why):

        ("absent",  "HTTP 404")   the server said no file — believed
        ("present", "")           a file is there; fetch it through Chrome
        ("",        why)          anything else; ask the browser as before

    **Why this exists (v1.34).** A native with no file behind it is the
    commonest answer on a collection pass, and the browser cannot hear
    it: measured from inside the institution's network, the navigation
    never commits, so
    the page budget, the wait for a file and then the second opinion
    were all spent before the 404 was read — about 44 seconds a row.
    Asking first costs one request and is answered in about one.

    Nothing is fetched here. A file that exists still comes down through
    the browser, exactly as before; this only decides whether to send
    the browser at all. An HTML answer is NOT taken as "present" — a
    page where a file was expected is a question for the browser to
    read, not a file.

    From outside that network the network layer is challenged, and then
    the answer is
    "" and the browser path runs as it always has. Nothing is disguised
    and nothing is held for: see ask_the_network().
    """
    if not cookie:
        return "", "no session cookie to ask with"
    try:
        with _open(url, cookie, timeout) as resp:
            try:
                landed = resp.geturl() or ""
            except Exception:
                landed = ""
            if looks_like_login_page("", landed):
                raise LoginRequired()
            ctype = (resp.headers.get("Content-Type", "") or "").lower()
            if "text/html" in ctype:
                return "", "answered with a web page"
            return "present", ""
    except urllib.error.HTTPError as e:
        if e.code in ABSENT_CODES:
            return "absent", "HTTP {}".format(e.code)
        if _response_is_a_challenge(e):
            return "challenged", ("refused with HTTP {} carrying a "
                                  "verification challenge, which it cannot "
                                  "answer".format(e.code))
        return "", "answered {}".format(describe_error(e))
    except (LoginRequired, RateLimited):
        raise
    except Exception as e:                       # noqa: BLE001
        return "", "unable to answer ({})".format(describe_error(e))


class BrowserFetcher:
    """Files by navigating the attached Chrome.

    It genuinely is a browser: the person's own authenticated session, the
    window in front of them, and when the site asks whether somebody is
    present, the question reaches a person who answers it. Nothing here
    disguises anything.

    What it loses is status codes, and that is the real cost. Over urllib
    a 404 and a 403 are different facts; here there is no status at all,
    so the three outcomes have to be told apart from what the tab shows:

        a challenge marker  →  VerificationRequired   (pause, ask a person)
        an absence marker   →  NoFileAvailable        (definitely no file)
        a file lands        →  Fetched                (success)

    and anything else WAS BrowserFetchFailed carrying what was observed.
    Since v1.33.9 "anything else" first asks the network layer the same
    question — see ask_the_network() — because the one outcome the tab
    cannot express at all is a 404, and that turned out to be the most
    common answer of the lot.
    """

    path = FETCH_BROWSER
    label = "Chrome"

    def __init__(self, incoming_dir, watch=None, page_timeout=None):
        self.incoming = incoming_dir
        self.watch = watch or DirectoryDownloadWatch(incoming_dir)
        self.page_timeout = (BROWSER_PAGE_TIMEOUT if page_timeout is None
                             else page_timeout)
        self.native_page_timeout = BROWSER_NATIVE_PAGE_TIMEOUT
        # Attributes rather than constants read at the call site, so a
        # test can wait a fraction of a second instead of the module's
        # real patience. The defaults ARE the constants.
        self.start_within = BROWSER_START_WITHIN
        self.settle_timeout = BROWSER_SETTLE_TIMEOUT
        self.stall_limit = BROWSER_STALL_LIMIT
        # Where a long transfer reports itself. Set by fetch_one — the
        # one function both workers fetch through — so neither worker can
        # forget to wire it.
        self.say = None
        self.driver = None
        self._behavior_set = False
        self._last_title = ""
        self._requested = ""
        # Not for fetching. The one thing this path cannot do is hear a
        # status code, so it keeps the session's cookie to ask the
        # network layer what the server said when the tab says nothing.
        # Empty is a working state — it degrades to the old behavior and
        # the message says that it did.
        self.cookie = ""
        self.file_timeout = FILE_TIMEOUT
        # v1.34: natives are asked about over the network first, headers
        # only. Counted, because every one is a request the site sees.
        self.status_first = True
        self.status_asks = 0
        self.network_deliveries = 0
        self._network_in_a_row = 0
        self._asked_first = ""
        self._asked_first_challenged = False
        self.restore_page_timeout = BROWSER_PAGE_TIMEOUT_DEFAULT

    # -- lifecycle ---------------------------------------------------------
    def open(self, driver):
        """Point Chrome's downloads at the private folder.

        The matching close() is not optional and not merely tidy: module
        contract 5 says a browser-driven download sets the download folder
        back, because the alternative is that the person's next manual
        download silently lands in a run folder.
        """
        os.makedirs(self.incoming, exist_ok=True)
        self.driver = driver
        # Put back what was actually there, not a constant. The Chrome
        # bridge sets its own page-load timeout at attach time and says
        # so in the log; restoring a hard-coded number after every
        # navigation would quietly overrule whatever it chose. The
        # constant is the fallback for a driver that will not say.
        try:
            was = driver.timeouts.page_load
            self.restore_page_timeout = (float(was) / 1000.0 if was > 1000
                                         else float(was))
        except Exception:
            self.restore_page_timeout = BROWSER_PAGE_TIMEOUT_DEFAULT
        # Recorded BEFORE the command, not after it. On 2026-09-30 this
        # command waited 270 s and raised; whether Chrome had applied it
        # could not be known, and because the flag was set only on
        # success, close() did not try to put the folder back and said
        # nothing. A restore that is attempted needlessly costs nothing;
        # one that is skipped silently leaves the person's next download
        # in a run folder.
        self._behavior_set = True
        driver.execute_cdp_cmd(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": self.incoming,
             "eventsEnabled": True})
        return self

    def use_cookie(self, cookie):
        """Keep the session cookie for the second opinion, not for fetching.

        Until v1.33.9 this did nothing, and the reason given was sound as
        far as it went: this path IS the session rather than borrowing
        its cookie, and it navigates rather than requesting. What that
        reasoning missed is that navigating loses the status code, and a
        404 has no other expression — so the cookie is kept in order to
        ASK, once, after the browser has already failed. Files still come
        down through Chrome.

        Both workers already call this once per structure, before any
        fetch, which is why nothing else had to change.
        """
        self.cookie = cookie or ""

    def close(self):
        """Give the browser back. Returns notes worth logging, not raising.

        Called from a `finally`, where raising would replace whatever
        actually ended the run with a tidy-up error.
        """
        notes = []
        # settle=0: the run is over, so nothing can still be arriving and
        # there is no reason to wait to find out. Everything left is
        # stranded by definition.
        stranded, _growing = self.watch.clear_partials(settle=0)
        # Reported from here because both workers already log close()'s
        # notes in their `finally` — one place, reached by both, rather
        # than two summary lines that could drift apart.
        if self.network_deliveries:
            notes.append(
                "{} file(s) came over the network layer because Chrome "
                "delivered nothing for them".format(self.network_deliveries))
        if self.status_asks:
            notes.append(
                "{} native file(s) were asked about over the network layer "
                "before the browser was sent — headers only, no file "
                "fetched that way".format(self.status_asks))
        if stranded:
            notes.append("cleared {} unfinished transfer(s) from the "
                         "incoming folder: {}".format(len(stranded),
                                                      ", ".join(stranded)))
        if self.driver is not None:
            if self._behavior_set:
                try:
                    self.driver.execute_cdp_cmd(
                        "Browser.setDownloadBehavior", {"behavior": "default"})
                    self._behavior_set = False
                    notes.append("Chrome's download folder has been set "
                                 "back to its own default.")
                except Exception as e:
                    # Said loudly. The failure mode is the person's next
                    # manual download landing in a run folder, which they
                    # would discover by not finding it.
                    notes.append(
                        "COULD NOT restore Chrome's download folder ({}) — "
                        "check Settings → Downloads before downloading "
                        "anything by hand.".format(describe_error(e)))
        return notes

    def page_timeout_for(self, url):
        """The page-load allowance for one FILE navigation (v1.34.2).

        A native — either URL form — gets BROWSER_NATIVE_PAGE_TIMEOUT,
        never less than the fetcher's own allowance. Everything else, and
        the site-root navigation normalize_tab() makes, keeps
        self.page_timeout.
        """
        if is_native_url(url) or content_link_type(url) == "native":
            return max(self.page_timeout, self.native_page_timeout)
        return self.page_timeout

    def _set_page_timeout(self, seconds):
        try:
            self.driver.set_page_load_timeout(seconds)
        except Exception:
            # Not fatal either way: too long merely makes a successful
            # download wait for its own timeout, and too short is what the
            # success path already expects.
            pass

    # -- reading the tab ---------------------------------------------------
    def tab_state(self):
        """What the tab is showing: (state, title, url).

        state is "challenge", "absent", "unreadable" or "other".
        """
        try:
            title = self.driver.title or ""
            source = self.driver.page_source or ""
            url = self.driver.current_url or ""
        except Exception as e:
            return "unreadable", describe_error(e), ""
        self._last_title = title
        body = (title + " " + source).lower()
        if any(m in body for m in CHALLENGE_MARKERS):
            return "challenge", title, url
        if any(m in body for m in ABSENT_MARKERS):
            return "absent", title, url
        return "other", title, url

    def is_challenge(self):
        """Passive: is the tab STILL showing the question?

        Used by the hold, which must not navigate while somebody is trying
        to click the checkbox.
        """
        return self.tab_state()[0] == "challenge"

    def peek(self):
        """(title, url) as cheaply as possible, for a before/after check.

        **The tab is only an honest witness when it moves.** A download
        never navigates it — Chrome hands the bytes to its own network
        stack and the page stays exactly where it was — so "the tab is
        showing X" says nothing about the request just made. Only "the
        tab CHANGED to X" does.

        Measured in v1.33.4, and expensively: after a real challenge the
        tab was left on Cloudflare's own page, and every one of the next
        145 downloads read that frozen page as a fresh prompt. The audit
        trail — the artifact this whole feature exists to produce —
        recorded 145 events that never happened.
        """
        try:
            return (self.driver.title or "", self.driver.current_url or "")
        except Exception:
            return ("", "")

    def doc_id(self):
        """Which document is on screen, as an opaque string.

        Answers the one question the tab's appearance cannot: **did THIS
        navigation put a new page there?** A download completes no
        navigation and leaves the document untouched; a page the server
        served is a new document with a new time origin, even when it
        looks exactly like the one before.

        `moved()` was the older approximation of that question, and it
        is genuinely weaker: a served page is a new document even when
        it looks identical to the last one, which appearance cannot see.

        It is worth being exact about what this did NOT fix, because
        v1.33.7 shipped claiming it did. The 51 rows of a run from outside
        the institution's network were not redirects that `moved()` failed
        to notice. Probed directly: the navigation never committed, so there
        was no new document for this to compare and the tab stayed on
        the page it was already showing. This returns the same string
        before and after, correctly, and `served()` correctly says no.
        The server's answer to those requests was HTTP 404, and no
        reading of the tab can recover it — see ask_the_network().

        Returns "" when the browser will not say. The caller then falls
        back to the appearance test, which is weaker, rather than to a
        claim built on nothing.
        """
        try:
            got = self.driver.execute_script(
                "return String(performance.timeOrigin) + '|' + "
                "String(document.location.href);")
        except Exception:
            return ""
        return str(got or "")

    def title_is_challenge(self):
        """The cheap version, for glancing at the tab twice a second.

        Title only. `page_source` serializes the whole DOM across the
        DevTools channel, which is not a thing to do twice a second
        while a thesis is downloading — and the interstitial puts its
        marker in the TITLE, which is what makes a title-only read
        enough for NOTICING. Deciding is still tab_state()'s job; this
        only says "look properly".
        """
        try:
            title = (self.driver.title or "").lower()
        except Exception:
            return False
        return any(m in title for m in CHALLENGE_MARKERS)

    def arriving(self):
        """Passive: has anything begun arriving since this fetch started?

        Asked against the snapshot the fetch took, not against "is the
        folder non-empty" — which was the old test and which is true of
        anything a previous fetch happened to leave behind.
        """
        return self.watch.anything_started()

    # -- fetching ----------------------------------------------------------
    def _navigate(self, url):
        """Go to `url`. Returns True when a PAGE rendered.

        False means the page load timed out, which is the ordinary and
        expected outcome of asking for a file: a download completes no
        navigation. Telling the two apart is what lets a rendered page
        be read at once instead of after the file-arrival patience.
        """
        # Set around THIS navigation and put straight back, rather than
        # once for the run. The driver is shared: the worker navigates the
        # site root once per structure to prove the session is live, and a
        # fifteen-second page-load timeout left in place would fail that
        # for a reason having nothing to do with it.
        self._set_page_timeout(self.page_timeout_for(url))
        try:
            self.driver.get(url)
            return True
        except Exception as e:
            if not _is_page_load_timeout(e):
                raise
            return False
        finally:
            self._set_page_timeout(self.restore_page_timeout)

    def _attempt(self, url):
        """Navigate, then wait. Returns a Fetched, or raises."""
        self._asked_first = ""
        self._asked_first_challenged = False
        if self.status_first and self.cookie and is_native_url(url):
            answer, why = native_status(url, self.cookie, self.file_timeout)
            self.status_asks += 1
            if answer == "absent":
                raise NoFileAvailable(
                    "the server answered {} for this file — asked over the "
                    "network layer before sending the browser".format(why))
            if answer in ("", "challenged"):
                # Remembered so the second opinion is not asked the same
                # question twice about the same URL.
                self._asked_first = why
                self._asked_first_challenged = answer == "challenged"
        self.watch.begin()
        self._requested = url
        before = self.peek()
        before_doc = self.doc_id()
        return self._delivered(self._await(
            url, rendered=self._navigate(url), before=before,
            before_doc=before_doc))

    def tab_is_stuck_on_a_challenge(self):
        """Is the tab sitting on a challenge page that is no longer news?"""
        return self.title_is_challenge()

    def normalize_tab(self, home_url):
        """Put the tab back on an ordinary page, so it can speak again.

        Called once after a prompt has been dealt with AND the file is
        safely in hand — never before, because navigating during a
        transfer aborts it. Without this the tab stays on the challenge
        page for the rest of the run and every later reading of it is
        meaningless.

        Returns "" when the tab is fine, or a note worth logging.
        """
        if not home_url or not self.tab_is_stuck_on_a_challenge():
            return ""
        try:
            self._set_page_timeout(self.page_timeout)
            try:
                self.driver.get(home_url)
            except Exception as e:
                if not _is_page_load_timeout(e):
                    raise
            finally:
                self._set_page_timeout(self.restore_page_timeout)
        except Exception as e:
            return ("could not move the tab off the verification page "
                    "({}) — later prompts may go unnoticed until it "
                    "moves".format(describe_error(e)))
        if self.tab_is_stuck_on_a_challenge():
            return ("the tab is still showing the verification page; "
                    "later prompts may go unnoticed until it moves")
        return ""

    def resume(self, prompt=None):
        """Wait for a transfer that is ALREADY under way — no navigation.

        **This is what a hold that ended in bytes needs.** Clearing a
        verification prompt returns the browser to the URL it asked for
        and the download starts on its own; navigating again at that
        point issues a fresh request, draws a fresh interstitial, and —
        before partials were checked for growth — destroyed the partly
        downloaded file on the way. On 2026-09-15 that cost a run of
        1,084 records, on a video that was downloading correctly.
        """
        # watch_tab=False. **A resume is never watching for news.** It
        # only ever runs after a prompt has already been dealt with, so
        # a challenge page on screen is the one we know about — and the
        # tab does not move during a download, so it is still there.
        #
        # v1.33.5 taught _attempt() to ignore a frozen challenge page
        # and did not teach resume(), which is the thirteenth time in
        # this module that a rule reached one of a pair and not the
        # other. Live proof: a run from outside the network recorded
        # three prompts where two had happened — the extra one filed
        # against the same file request as the first, with 0 files
        # between them, seventeen seconds later.
        #
        # A genuinely new challenge during a resume still cannot be
        # missed: it stops the file arriving, and the check after the
        # wait is decisive.
        return self._delivered(self._await(
            self._requested, rendered=False, prompt=prompt, watch_tab=False))

    def _delivered(self, got):
        """Which path a file came by — observed for BOTH ways a fetch
        finishes (a fresh attempt and a resume), through this one method.

        v1.34, item D. Since v1.33.9 a file Chrome never delivered can
        still arrive, over the network layer's second opinion. That is
        correct, and it is also the only sign that downloads from this
        site are blocked in this Chrome profile — per-site permissions
        are something the localhost preflight cannot see. Without a
        count the run would quietly become a network-layer run at about
        25 seconds a file, and nothing would say so.
        """
        if getattr(got, "via_network", False):
            self.network_deliveries += 1
            self._network_in_a_row += 1
            if self._network_in_a_row == NETWORK_IN_A_ROW_NOTE and self.say:
                self.say(
                    "{} files in a row came over the network layer after "
                    "Chrome delivered nothing. Downloads from this site may "
                    "be blocked in this Chrome profile; the run goes on, "
                    "more slowly.".format(NETWORK_IN_A_ROW_NOTE))
        else:
            self._network_in_a_row = 0
        return got

    def _await(self, url, rendered=False, prompt=None, before=None,
               watch_tab=True, before_doc=""):
        """Wait for the file, reading the tab while waiting.

        Three things can be true after a navigation, and the live runs
        showed they are not mutually exclusive:

          * a file is arriving, or has arrived;
          * a page rendered, and it says something;
          * a verification prompt appeared at some point, and may have
            been answered before anything here looked at the tab.

        So the tab is watched WHILE waiting rather than only after
        giving up, and a prompt that came and went is carried out on the
        Fetched instead of being lost. That `prompt` is data for the
        audit trail, not a reason to stop: somebody already dealt with
        it.
        """
        state, title, landed, note = "other", "", "", ""
        # What the tab was showing BEFORE this navigation. Everything
        # below asks whether it moved, not what it says — see peek().
        was_title, was_url = before if before else ("", "")
        was_challenge = any(m in (was_title or "").lower()
                            for m in CHALLENGE_MARKERS)

        def moved(now_title, now_url):
            return (now_title, now_url) != (was_title, was_url)

        def served(now_title, now_url):
            """Is what is on screen an answer to THIS request?

            Document identity when the browser will give it, because a
            served page is a new document however familiar it looks;
            appearance when it will not. Both branches below ask this
            one function — the earlier code asked `moved()` in one place
            and `moved()` in the other, which is the same rule twice and
            was wrong in both.
            """
            now_doc = self.doc_id()
            if before_doc and now_doc:
                return now_doc != before_doc
            return moved(now_title, now_url)
        # At most two passes. A prompt cleared while we waited usually
        # restarts the download, so the file deserves a fresh patience —
        # but only one, or a page that flickers could loop.
        for attempt in (1, 2):
            if rendered and attempt == 1:
                # The navigation COMPLETED, which a download never does:
                # Chrome hands a download to its own network stack and
                # the page load times out instead. So a completed
                # navigation means a page is on screen, and it can be
                # read now rather than after eight seconds of waiting
                # for a file that is not coming. On the first live run
                # that wait cost about 44 seconds per record on a
                # structure of link-only records.
                state, title, landed = self.tab_state()
                if not served(title, landed):
                    # The navigation completed but no new document went
                    # on screen, so what is there belongs to the last
                    # request. Wait for the file instead of reading a
                    # page that is not about this one.
                    state = "stale"
                if state == "challenge":
                    raise VerificationRequired(title, landed)
                if state == "absent" and not self.watch.anything_started():
                    raise NoFileAvailable(
                        "the page says there is no file to download — "
                        "tab: {!r}".format(title))
                if state != "stale" and redirected_away(url, landed) \
                        and not self.watch.anything_started():
                    # A redirect is the server answering, but WHAT it
                    # answered is on the page, not in the fact of the
                    # redirect. A page stating an absence was read just
                    # above; anything else — measured, a
                    # "500 Internal Server Error" — establishes nothing,
                    # and until v1.34 it was written down as an absence.
                    # Asked at once rather than after the wait for a
                    # file: the redirect has ruled that out.
                    return self._second_opinion(
                        url, redirect_note(landed, title), title, landed,
                        False)
            t_settle = time.time()
            # A challenge ALREADY on screen before this navigation is
            # not news about this request — it is the page Chrome never
            # moved off. Watching for one is only meaningful when the
            # tab was showing something else to begin with. When it was
            # not, a real challenge still cannot be missed: it stops the
            # file arriving, and the check after the wait is decisive.
            name, note = self.watch.settle(
                self.start_within, self.settle_timeout,
                observe=(self.title_is_challenge
                         if watch_tab and not was_challenge else None),
                stall_limit=self.stall_limit, progress=self.say)
            if self.watch.challenge_at is not None and prompt is None:
                # Wall clock, not the watch's own numbers: the watch
                # returns the moment it SEES a prompt, so its
                # "challenge_gone_at" is usually still None at that
                # point and a prompt would be filed as never cleared.
                prompt = {"observed": self._last_title,
                          "seen_at": t_settle + self.watch.challenge_at,
                          "cleared_in": None}
            if name:
                if prompt is not None and prompt["cleared_in"] is None:
                    prompt["cleared_in"] = time.time() - prompt["seen_at"]
                return Fetched(path=os.path.join(self.incoming, name),
                               name=name, prompt=prompt)
            state, title, landed = self.tab_state()
            # No file came, so the tab IS the evidence, moved or not: a
            # challenge page with nothing arriving is a challenge
            # blocking this request, whether it was drawn just now or
            # left over from the last one.
            if state == "challenge":
                raise VerificationRequired(title, landed)
            if prompt is not None and attempt == 1:
                # It was showing and it is not showing now, so it was
                # answered while we waited. Record when that happened,
                # and give the file the chance the clearance created.
                prompt["observed"] = prompt["observed"] or title
                prompt["cleared_in"] = time.time() - prompt["seen_at"]
                continue
            break
        # **Only a page served for THIS request is evidence.** The
        # rendered fast path above has asked `served()` since v1.33.7;
        # this branch did not, so a "No PDF has been provided" page left
        # on the tab by an EARLIER request was read as the answer to
        # every later navigation that never committed. Measured
        # 2026-09-29: 178 of 181 native absences in one run were written
        # while the tab was already showing an earlier absence page, and
        # a probe watched a real file arrive under exactly such a page.
        # A file slow to start under a stale page would have been written
        # as absent — the false absence this vocabulary exists to
        # prevent. The rule reached one of a pair and not the other; it
        # now reaches both, through the same function.
        stale = state == "absent" and not served(title, landed)
        if state == "absent" and not stale:
            raise NoFileAvailable(
                "the page says there is no file to download — tab: {!r}"
                .format(title))
        # A redirect is the server ANSWERING — but only if the browser
        # actually went somewhere. After a download the tab never moves,
        # so "the tab is on a different URL than I asked for" is true of
        # every attempt and means nothing on its own. It is the MOVE
        # that carries the meaning.
        if served(title, landed) \
                and redirected_away(url, landed) \
                and not self.watch.anything_started():
            return self._second_opinion(
                url, redirect_note(landed, title), title, landed, False)
        # Nothing arrived and the tab said nothing usable. That is not a
        # conclusion, it is the absence of one — so the last word goes to
        # the path that hears status codes, not to this one.
        return self._second_opinion(url, note, title, landed, stale)

    def _second_opinion(self, url, note, title, landed, stale):
        """The last word goes to the path that hears status codes.

        One function for every place the browser gives up, so the rules
        about what may be concluded cannot differ between them: a page
        left by an earlier request is named as such, and a network that
        was already asked (v1.34, natives) is not asked again.
        """
        if self._asked_first and url == self._requested:
            raise (NotConfirmedHere if self._asked_first_challenged
                   else BrowserFetchFailed)(
                "{} — tab: {!r}{}, landed on: {} — and the network layer, "
                "asked first, was {}, so nothing here establishes whether "
                "the file exists".format(
                    note, title,
                    " (left there by an earlier request, so it says "
                    "nothing about this one)" if stale else "",
                    landed or "(unknown)", self._asked_first))
        return ask_the_network(
            url, self.cookie,
            "{} — tab: {!r}{}, landed on: {}".format(
                note, title,
                " (left there by an earlier request, so it says nothing "
                "about this one)" if stale else "",
                landed or "(unknown)"),
            self.file_timeout, folder=self.incoming)

    def fetch(self, url, native_url=""):
        """Fetch one record file, falling back to the native.

        The same fallback rule fetch_record_file() applies over urllib, and
        for the same reason: Digital Commons generates a derivative only
        from Word, PowerPoint and PDF uploads, so for everything else the
        content URL answers with a web page and the native is where the
        file actually is.

        A challenge is NOT a reason to try the native. It is not an answer
        about this URL at all, and asking a second time would spend another
        request to be asked the same question.
        """
        try:
            return self._attempt(url)
        except VerificationRequired:
            raise
        except (NoFileAvailable, BrowserFetchFailed) as first_error:
            if not native_url:
                raise
            first = first_error
        # A prompt met on the derivative is still a prompt, even when
        # the derivative then turns out not to exist. Carried across to
        # whatever the native returns, so the audit trail does not lose
        # it to a fallback.
        first_prompt = getattr(self.watch, "challenge_at", None) is not None
        first_seen = {"observed": self._last_title,
                      "cleared_in": self.watch.challenge_gone_at} \
            if first_prompt else None
        _throttle("file")
        try:
            got = self._attempt(native_url)
        except VerificationRequired:
            raise
        except Exception as second:
            first_absent = isinstance(first, NoFileAvailable)
            second_absent = isinstance(second, NoFileAvailable)
            if first_absent and second_absent:
                raise NoFileAvailable(
                    "no downloadable file — derivative: {} | native: {}"
                    .format(describe_error(first), describe_error(second)))
            # At least one attempt never got an answer, so nothing here
            # licenses a claim that the record has no file. Report the one
            # that was not an answer — that is the one a person can act on.
            if first_absent and not second_absent:
                mark_native_fallback(second, native_url)
            raise (second if not second_absent else first)
        got.used_native = True
        if got.prompt is None and first_seen is not None:
            got.prompt = first_seen
        return got


def redirect_note(landed, title):
    """What a redirect establishes, and no more."""
    return ("the server sent the request to {}, a page that does not say "
            "the file is absent".format(landed or "(unknown)"))


def redirected_away(requested, landed) -> bool:
    """Did the browser end up somewhere other than the URL we asked for?

    Path comparison only: the query string is where the article and the
    file type live, and a server that answers a content URL serves the
    file without moving the page. Both paths must be readable — an empty
    `landed` means the tab could not be read, and a claim must not be
    built on that.

    Deliberately structural. The page it lands on names an institution,
    and matching on that name would put an institution's identity in
    source and be wrong for every other repository.
    """
    if not requested or not landed:
        return False
    try:
        want = urlparse(requested).path or "/"
        got = urlparse(landed).path or "/"
    except Exception:
        return False
    return want.rstrip("/") != got.rstrip("/")


TAB_READY_SECONDS = 20.0
TAB_READY_GRACE = 5.0      # beyond the page-load limit, for the thread itself


class TabNotAnswering(Exception):
    """The debug Chrome window's tab did not answer before the run began."""


def ready_the_tab(driver, home_url, within=None):
    """Put the tab on the site, and prove it answers, before anything else.

    v1.34.1. 2026-09-30: a run's first command to Chrome — setting the
    download folder — waited 270 s on a tab that never answered, then
    ended as an unexplained error. The tab had been left on Chrome's own
    start page in a browser that had been open for a while; a fresh
    browser on the same page did NOT reproduce it, so the cause is not
    established. This does not depend on knowing it: the tab is moved to
    the site's front page, which the run visits a moment later anyway, and
    the whole step is bounded by the clock rather than by chromedriver's
    300 s.

    The navigation runs on a thread so that the bound holds even when
    Chrome answers nothing at all. A thread left waiting is abandoned; the
    run stops before anything is requested, and the message says to
    restart the browser, which is what releases it.
    """
    within = float(TAB_READY_SECONDS if within is None else within)
    out = {}
    # What the page-load timeout WAS, so it can be put back (v1.34.2).
    # This set it to `within` and left it there, so the fetcher, which
    # restores "what was there" when it closes, then restored this
    # function's 20 s instead of the bridge's own setting — a silent
    # change to someone else's setting, which the fetcher was written
    # specifically not to make.
    try:
        was = driver.timeouts.page_load
        prior = float(was) / 1000.0 if was > 1000 else float(was)
    except Exception:                                # noqa: BLE001
        prior = None

    def go():
        try:
            try:
                driver.set_page_load_timeout(within)
            except Exception:                        # noqa: BLE001
                pass
            try:
                driver.get(home_url)
            except Exception as e:                   # noqa: BLE001
                if not _is_page_load_timeout(e) or \
                        "receiving message from renderer" in str(e):
                    raise
            out["title"] = driver.title
        except Exception as e:                       # noqa: BLE001
            out["error"] = e
        finally:
            if prior is not None:
                try:
                    driver.set_page_load_timeout(prior)
                except Exception:                    # noqa: BLE001
                    pass

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(within + TAB_READY_GRACE)
    if t.is_alive() or "title" not in out:
        why = ("no answer within {:.0f}s".format(within) if t.is_alive()
               else describe_error(out.get("error")))
        raise TabNotAnswering(
            "the debug Chrome window's tab did not answer when the run "
            "moved it to {} ({}). Nothing was requested from the site and "
            "Chrome's download folder was not changed. Quit the debug "
            "Chrome window completely, start it again, and start the run "
            "again.".format(home_url, why))
    return out["title"]


def tab_ready_check(driver, home_url, checks=None, say=None):
    """ready_the_tab(), timed, said in the log and on the Checks sheet.

    v1.34.2. It was silent, so on 2026-10-01 its work was credited to the
    per-structure site check ("Site page read in N s") and had to be
    un-credited the same morning. One function both workers call: it
    records the result either way, then raises TabNotAnswering as before.
    """
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    try:
        title = ready_the_tab(driver, home_url)
    except TabNotAnswering as e:
        if checks is not None:
            checks.append(("Tab ready", "failed", str(e), when))
        raise
    took = time.time() - t0
    observed = ("the tab moved to {} and answered in {:.1f} s (title: "
                "{!r})".format(home_url, took, title or ""))
    if checks is not None:
        checks.append(("Tab ready", "passed", observed, when))
    if say:
        say("Chrome's tab is ready: " + observed + ".")
    return title


def _is_page_load_timeout(exc) -> bool:
    """Selenium's page-load timeout, which every successful download raises.

    Matched on the class name rather than by importing it, because the
    module must not import selenium at module scope — it is a declared
    dependency of the run, not of the file.
    """
    return exc.__class__.__name__ == "TimeoutException"


# ---------------------------------------------------------------------------
# The download-path preflight (v1.34, open item 13)
#
# Not a settings inspection — reading preferences is the check_connection
# mistake, verifying everything except the thing that matters. It DOWNLOADS
# A FILE: a few bytes served from this computer, fetched by the attached
# Chrome exactly as a real file is, watched for the same .crdownload →
# final-name transition — TWICE, because the failure that matters for a
# batch is the second download. It costs the repository nothing: no
# request, no verification prompt, nothing for anyone to approve.
# ---------------------------------------------------------------------------
NETWORK_IN_A_ROW_NOTE = 3      # item D: say so after this many in a row
PREFLIGHT_PATH = "/__dc_preflight__/"
PREFLIGHT_BYTES = b"DC Admin Suite download check. Safe to delete.\n"
PREFLIGHT_START_WITHIN = 10.0
PREFLIGHT_MIN_FREE = 1 * 1024 ** 3        # below this, refuse
PREFLIGHT_WARN_FREE = 11 * 1024 ** 3      # below this, say a 10 GB file
                                          # may not fit
WINDOWS_PATH_LIMIT = 259


class NotConfigured(Exception):
    """This computer cannot download for the run — found before any request.

    The fifth stop reason, beside Stop, ServerRefusing, SessionFull and
    NotVerified. A run that cannot download writes a report full of false
    failures, and a report is what every later re-run reads.
    """


class _PreflightServer:
    """A few bytes as an attachment, from 127.0.0.1, for the preflight."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if not self.path.startswith(PREFLIGHT_PATH):
                    self.send_response(404)
                    self.end_headers()
                    return
                name = self.path[len(PREFLIGHT_PATH):] or "check.txt"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 'attachment; filename="{}"'.format(name))
                self.send_header("Content-Length", str(len(PREFLIGHT_BYTES)))
                self.end_headers()
                self.wfile.write(PREFLIGHT_BYTES)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def url(self, n):
        return "http://127.0.0.1:{}{}dc-preflight-{}.txt".format(
            self.server.server_address[1], PREFLIGHT_PATH, n)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def preflight_downloads(fetcher, dest_dir, run_name="DC_FileDownloads_"
                        "YYYYMMDD_HHMMSS", attempts=2, disk_usage=None,
                        checks=None):
    """Can this computer download for this run? Returns notes to log.

    ONE function both workers call before any file is requested. Raises
    NotConfigured with what was OBSERVED — never a guessed cause.

    `checks`, when given, receives one row per check for the master log's
    Checks sheet (CHECKS_HEADERS), written before any refusal is raised.
    """
    notes = []

    def rec(check, result, observed):
        if checks is not None:
            checks.append((check, result, observed,
                           time.strftime("%Y-%m-%d %H:%M:%S")))
    dest_dir = os.path.abspath(os.path.expanduser(dest_dir or "."))
    # Depth: the longest path this run can write, with the capped name.
    longest = len(os.path.join(dest_dir, run_name, "x" * 40,
                               "x" * FILENAME_MAX_CHARS))
    depth = ("the longest path this run can write is {} characters, at "
             "{}; Windows allows {}".format(longest, dest_dir,
                                            WINDOWS_PATH_LIMIT + 1))
    if longest > WINDOWS_PATH_LIMIT:
        if os.name == "nt":
            rec("Destination depth", "refused", depth)
            raise NotConfigured(
                "files here could need paths of {} characters, past "
                "Windows' limit of {}. Choose a destination nearer the top "
                "of the drive.".format(longest, WINDOWS_PATH_LIMIT + 1))
        rec("Destination depth", "note", depth + " — fine on this system")
        notes.append("Paths in this destination can reach {} characters — "
                     "fine on this system, too long for Windows."
                     .format(longest))
    else:
        rec("Destination depth", "passed", depth)
    # Free space.
    try:
        free = (disk_usage or shutil.disk_usage)(dest_dir).free
    except OSError as e:
        rec("Free space", "refused", "could not be read at {}: {}".format(
            dest_dir, describe_error(e)))
        raise NotConfigured("could not read the free space at {}: {}"
                            .format(dest_dir, describe_error(e)))
    room = "{} free at {}".format(human_size(free), dest_dir)
    if free < PREFLIGHT_MIN_FREE:
        rec("Free space", "refused", room)
        raise NotConfigured("only {} free at {}".format(human_size(free),
                                                        dest_dir))
    if free < PREFLIGHT_WARN_FREE:
        rec("Free space", "note", room + " — a single file of up to 10 GB "
            "may not fit")
        notes.append("Only {} free at the destination — a single file of up "
                     "to 10 GB may not fit.".format(human_size(free)))
    else:
        rec("Free space", "passed", room)
    # Two real downloads through the attached Chrome.
    if not isinstance(fetcher, BrowserFetcher):
        rec("Download test", "not run",
            "files are fetched over the network layer, which does not use "
            "Chrome's download folder")
        return notes
    server = _PreflightServer()
    took = []
    try:
        for n in range(1, attempts + 1):
            t0 = time.time()
            fetcher.watch.begin()
            fetcher._navigate(server.url(n))
            name, note = fetcher.watch.settle(
                start_within=PREFLIGHT_START_WITHIN, timeout=None,
                stall_limit=PREFLIGHT_START_WITHIN)
            if not name:
                rec("Download test", "refused",
                    "download {} of {} from this computer did not arrive in "
                    "Chrome ({}); {} arrived before it".format(
                        n, attempts, note or "no file", n - 1))
                raise NotConfigured(
                    "download {} of {} from this computer did not arrive in "
                    "Chrome ({}). Chrome accepted the request and no file "
                    "appeared. Settings that can do this include asking "
                    "where to save each file, a policy restricting "
                    "downloads, or security software — this check cannot "
                    "tell which.".format(n, attempts, note or "no file"))
            took.append(time.time() - t0)
            try:
                os.remove(os.path.join(fetcher.incoming, name))
            except OSError:
                pass
    finally:
        server.stop()
    rec("Download test", "passed",
        "{} of {} test file(s) from this computer arrived through Chrome, "
        "in {}, before any request to the site".format(
            attempts, attempts,
            " and ".join("{:.1f} s".format(t) for t in took)))
    notes.append("Download check: {} test file(s) from this computer "
                 "arrived through Chrome.".format(attempts))
    return notes


def make_fetcher(which, cookie="", incoming_dir="",
                 stall_limit=BROWSER_STALL_LIMIT):
    """The one place a fetch path is chosen. Unknown names are refused
    rather than quietly falling back, because a run silently taking the
    path nobody asked for is a run whose measurements mean nothing."""
    if which == FETCH_NETWORK:
        return NetworkFetcher(cookie, incoming_dir)
    if which == FETCH_BROWSER:
        f = BrowserFetcher(incoming_dir)
        f.stall_limit = float(stall_limit)
        return f
    raise ValueError("unknown fetch path: {!r}".format(which))


# ---------------------------------------------------------------------------
# The verification prompt, as a state rather than an error
#
# This is the monitoring mechanism, not an obstacle to it: the site asks
# whether a person is present, the run stops until one says so, and every
# prompt is recorded with when it came, how much work preceded it and how
# long it took to clear. That record is worth more than the feature — it
# lets the monitoring be demonstrated from the module's own reports.
#
# One object, held by both workers, for the reason BlockWatch is one.
# ---------------------------------------------------------------------------
class VerificationWatch:
    """Counts requests, holds for a person, and records every prompt."""

    def __init__(self, window=VERIFY_WINDOW, poll=VERIFY_POLL,
                 tick=VERIFY_TICK, max_holds=VERIFY_MAX_HOLDS,
                 sleep=None, stopping=None, now=None, announce=None,
                 on_hold=None):
        # A zero window means "do not wait at all", so it has to be
        # preserved rather than floored to something polite.
        self.window = max(0.0, float(window))
        self.poll = poll
        self.tick = tick
        self.max_holds = max_holds
        # Injected so the hold is testable without a browser, a network or
        # a real five minutes. The defaults are looked up at call time.
        self._sleep = sleep
        self._stopping = stopping
        self._now = now or time.time
        self._announce = announce
        # Pushed to the page while a hold is in progress, so the tab
        # title can change. On a second screen the tab title is what gets
        # noticed, and a prompt nobody notices is a run that stops.
        self._on_hold = on_hold
        self.requests = 0        # file requests made on this run
        self.since = 0           # ... since the last prompt
        self.prompts = []        # one dict per prompt, cleared or not
        self.abandoned = ""      # why the run gave up, if it did
        # Which structure was being worked when a prompt arrived. Set by
        # the worker as it moves between structures, and read at record()
        # time rather than passed in, so a caller cannot record a prompt
        # against the wrong one by forgetting an argument.
        self.context = ""

    # -- counting ----------------------------------------------------------
    def requested(self):
        """One file request is about to be made."""
        self.requests += 1
        self.since += 1

    @property
    def cleared(self):
        return [p for p in self.prompts if p["cleared"] is not None]

    def say(self, msg):
        if self._announce:
            self._announce(msg)

    def _rest(self, seconds):
        if self._sleep is not None:
            return self._sleep(seconds)
        return _sleep(seconds)

    def _stop_pressed(self):
        if self._stopping is not None:
            return bool(self._stopping())
        return _stopping()

    # -- holding -----------------------------------------------------------
    def hold(self, still_asking, arrived=None):
        """Wait for a person. Returns (seconds, why), or (-1, "").

        `why` is "cleared" when the page stopped asking and "arrived"
        when a transfer began instead — and **that distinction is
        load-bearing**. On 2026-09-15 a hold ended because clearing the
        prompt had restarted a large video download, and the caller,
        knowing only that the hold was over, deleted the partial file and
        navigated again. The fresh request drew a fresh interstitial,
        twice, and the run stopped on a situation it had created. A hold
        that ends because bytes are arriving must be followed by waiting,
        not by asking again.

        Deliberately does NOT navigate while waiting. Retrying the URL
        every few seconds yanks the page out from under whoever is trying
        to click the checkbox, which is how probe 3 lost a file after a
        two-second clearance. Two passive signals are watched instead: the
        page ceasing to ask, and a file arriving — because clearing the
        prompt usually returns the browser to the URL it was asked for,
        which starts the download by itself.

        Stop still works while holding. A person who would rather end the
        run than clear the prompt must not have to clear it first.
        """
        t0 = self._now()
        if self.window <= 0:
            return -1.0, ""
        self.say(HOLD_MESSAGE.format(self.window / 60.0))
        ticker = Ticker(self.tick)
        try:
            self._show(True, self.window)
            while True:
                waited = self._now() - t0
                if waited >= self.window:
                    return -1.0, ""
                if self._stop_pressed():
                    raise StopRequested(
                        "stopped while waiting for verification")
                if not self._rest(min(self.poll, self.window - waited)):
                    raise StopRequested(
                        "stopped while waiting for verification")
                waited = self._now() - t0
                # Bytes first. When both are true — the page cleared AND
                # the download it restarted has begun — the transfer is
                # the more useful fact, because it is the one that says
                # "do not ask again".
                if arrived is not None and arrived():
                    return waited, "arrived"
                if not still_asking():
                    return waited, "cleared"
                left = max(0.0, self.window - waited)
                self._show(True, left)
                if ticker.due(waited):
                    self.say("Still waiting for verification — {:.0f}s left "
                             "before this run writes out what it has."
                             .format(left))
        finally:
            # Whatever ended the hold — cleared, timed out, or Stop — the
            # page must stop saying a person is being waited for.
            self._show(False, 0)

    def _show(self, holding, left):
        if self._on_hold:
            self._on_hold(bool(holding), float(left), len(self.prompts))

    def record(self, at, since, cleared, observed="", paused=True):
        """Log one prompt: when it came, what preceded it, how it ended.

        `paused` says whether the run had to stop for it. False is the
        prompt somebody answered while the run was still waiting for
        the file — which is the kind the first live run lost entirely,
        and the kind that best demonstrates a person was present.
        """
        self.prompts.append({
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "context": self.context,
            "at": at, "since": since,
            "cleared": None if cleared is None or cleared < 0
            else round(float(cleared), 1),
            "observed": observed,
            "paused": bool(paused),
        })
        self.since = 0

    # -- saying what happened ---------------------------------------------
    def summary(self):
        """One line, and it says so when there was nothing to report.

        An absence is a result: "no verification was requested" and "this
        was never checked" are different claims, and a summary that omits
        the second reads as the first.
        """
        if not self.prompts:
            # "Seen", not "requested". The run can only report what it
            # observed, and on 2026-09-14 it said "no verification was
            # requested" about a run in which one was — answered in
            # about two seconds, before anything looked at the tab.
            # The detection now watches while it waits, which shrinks
            # that window but does not close it, so the claim is
            # written as the claim the run can actually support.
            return ("No verification prompt was seen during this run ({} "
                    "file request(s)).".format(self.requests))
        cleared = self.cleared
        times = [p["cleared"] for p in cleared if p["cleared"]]
        held = [p for p in self.prompts if p.get("paused")]
        line = ("{} verification prompt(s) in {} file request(s); {} "
                "cleared".format(len(self.prompts), self.requests,
                                 len(cleared)))
        if times:
            line += " in {:.0f}s on average".format(sum(times) / len(times))
        if len(held) < len(self.prompts):
            line += ", {} without stopping the run".format(
                len(self.prompts) - len(held))
        missed = len(self.prompts) - len(cleared)
        if missed:
            line += ", {} not cleared".format(missed)
        return line + "."

    def stop_reason(self):
        return (self.abandoned or
                "nobody confirmed a person was present within {:.0f} "
                "minutes".format(self.window / 60.0))

    def rows(self):
        """The prompts as report rows. See VERIFY_HEADERS."""
        return [(n, p["when"], p.get("context", ""), p["at"], p["since"],
                 "" if p["cleared"] is None else p["cleared"],
                 _prompt_outcome(p), p["observed"])
                for n, p in enumerate(self.prompts, start=1)]


def _prompt_outcome(prompt):
    """What the report says happened to one prompt.

    Observational on purpose. "Cleared by a person" was an inference —
    the run knows the page stopped asking, not who made it stop — and
    this module has been bitten before by a message that sounded like a
    finding.
    """
    if prompt["cleared"] is None:
        return "Not cleared"
    if prompt.get("paused"):
        return "Cleared while the run waited"
    return "Cleared without stopping the run"


# Said to the operator when the site asks. Mechanism only — it names no
# vendor and no agreement, because it is true of any repository and useful
# to any institution. The .format() is on the line with the placeholder:
# probe 3's banner printed "{:.0f} minutes" literally because the call was
# attached to the line below, in the one message whose entire job is
# telling the operator how long they have.
HOLD_MESSAGE = (
    "VERIFICATION NEEDED — Digital Commons is asking you to confirm you "
    "are here. Clear it in the Chrome window; this run resumes by itself. "
    "It waits {:.0f} minutes, then writes out what it has.")


def hold_if_challenged(fetcher, verify, where="", announce=None):
    """Check the tab after a navigation that was NOT a file request.

    **The second blind spot the first live run exposed.** Each worker
    navigates the repository's home page once per structure to prove
    the admin session is still live, and nothing ever looked at what
    came back. A verification prompt can be served to any navigation,
    not only to a file request — and one served there was invisible,
    which is one of the two ways that run could meet a prompt and
    report that none had been requested.

    Costs one title read per structure. Raises NotVerified if nobody
    answers, exactly as a prompt on a file request does.
    """
    probe = getattr(fetcher, "is_challenge", None)
    if probe is None or not probe():
        return False
    at = verify.requests
    if announce:
        announce("Digital Commons is asking for verification before it "
                 "will serve {} — the run is waiting.".format(
                     where or "this page"))
    # No "a file arrived" signal here: nothing was being downloaded, so
    # the only thing to watch is the page itself.
    took, _why = verify.hold(probe)
    verify.record(at, verify.since, took,
                  "on {}".format(where or "a site page"), paused=True)
    if took < 0:
        raise NotVerified(verify.stop_reason())
    if announce:
        announce("Verification cleared in {:.0f}s — carrying on.".format(
            took))
    return True


def fetch_says(ctx):
    """How fetch_one's running commentary reaches the log, in BOTH workers.

    One function so the two cannot drift again: it is progress, prefixed
    with the structure, and never a warning (v1.34.1).
    """
    return lambda msg: log("'{}' — {}".format(ctx, msg))


def showall_note(n_rows, page_size=None):
    """The log line for a listing x_showall served in one page.

    v1.34.2: it said "saving about 1 page requests".
    """
    saved = n_rows // (page_size or LISTING_PAGE_SIZE)
    return ("listing: {} record(s) in one page — x_showall worked, saving "
            "about {} page request{}".format(n_rows, saved,
                                             "" if saved == 1 else "s"))


def structures_done_text(master, total):
    """How many structures FINISHED, said so it is true of a stopped one.

    v1.34.2. `len(master)` counted the structure in flight as well, because
    a stop writes its row too — so a run stopped at record 44 of 391 of its
    only structure said "1 of 1 structure(s) done". A row written
    mid-structure is a stopped structure, and is said as one.
    """
    stopped = [m for m in master if "mid-structure" in str(m[7])]
    finished = len(master) - len(stopped)
    text = "{} of {} structure(s) finished".format(finished, total)
    if stopped:
        text += ", {} stopped part-way".format(len(stopped))
    return text


def prompt_announcement(at, files, first):
    """The log line for a verification prompt, true for the first one too."""
    if first:
        return ("Verification asked for at file request {} of this run — "
                "the first prompt of the run, after {} file(s)."
                .format(at, files))
    return ("Verification asked for at file request {} of this run, {} "
            "file(s) after the previous prompt.".format(at, files))


def fetch_one(fetcher, verify, url, native_url="", order=0, announce=None,
              home_url=""):
    """Fetch one file, holding for a person as many times as it takes.

    The single place both workers fetch through, so that the hold, the
    retry and the giving-up cannot exist in one worker and not the other.
    That shape has produced nine defects in this module, three of them in
    this pair of functions.

    Raises NotVerified when nobody confirms — a stop reason, not a failure
    — and otherwise raises exactly what the fetcher raised.
    """
    holds = 0
    # v1.34: a long transfer reports its progress where the hold reports
    # its countdown — the same log, the same cadence (see Ticker).
    if hasattr(fetcher, "say"):
        fetcher.say = announce or verify.say

    def _done(got):
        """The one way out with a file. Both returns go through it.

        The first version recorded the prompt and put the tab back on
        the outer success path only — and the resume path, which is the
        COMMON one when a prompt interrupts a transfer, returned past
        both. Logic on one path and not its twin is this module's
        signature defect; a single exit is the fix for it.
        """
        _record_carried_prompt(got, verify, announce)
        _unstick_tab(fetcher, home_url, holds, announce)
        return got

    while True:
        verify.requested()
        try:
            got = fetcher.fetch(url, native_url)
        except VerificationRequired as challenge:
            holds += 1
            at, since = verify.requests, verify.since
            if announce:
                # v1.34.2: the first prompt of a run has no previous one,
                # and said "0 file(s) after the previous prompt" anyway.
                announce(prompt_announcement(at, max(0, since - 1),
                                             first=not verify.prompts))
            took, why = verify.hold(
                getattr(fetcher, "is_challenge", lambda: False),
                getattr(fetcher, "arriving", None))
            verify.record(at, max(0, since - 1), took, challenge.observed)
            if took < 0:
                raise NotVerified(verify.stop_reason())
            if announce:
                announce("Verification cleared in {:.0f}s — {}.".format(
                    took, "the file it interrupted is arriving"
                    if why == "arrived" else "resuming"))

            # **Whatever ended the hold, the question is the same: is
            # anything coming?** If it is, wait for it — clearing the
            # prompt restarts the download, so asking again would issue
            # a fresh request and, before partials were checked for
            # growth, delete the partly downloaded file on the way. That
            # is what stopped a run of 1,084 records on 2026-09-15, on a
            # video that was downloading correctly.
            #
            # One rule rather than two branches. The first version had a
            # separate path for "the hold ended in bytes" and another
            # for "a partial is still growing", and planting showed the
            # second was all but unreachable — a guard that quietly does
            # nothing is the shape this module has a rule against.
            watch = getattr(fetcher, "watch", None)
            coming = bool(watch is not None
                          and (watch.anything_started() or watch.partials()))
            if coming and hasattr(fetcher, "resume"):
                try:
                    got = fetcher.resume()
                except VerificationRequired:
                    # It really is asking again. Fall through to the
                    # retry, which holds and counts properly.
                    pass
                else:
                    return _done(got)

            # Nothing was coming, so the request has to be made again —
            # and only now is it safe to tidy up. A partial that is
            # still growing is left alone and named, never deleted.
            if watch is not None:
                cleared, growing = watch.clear_partials()
                if cleared and announce:
                    announce("Cleared {} stranded transfer(s) before "
                             "retrying: {}".format(len(cleared),
                                                   ", ".join(cleared)))
                if growing and announce:
                    announce("{} transfer(s) began arriving while we "
                             "waited and are left alone: {}".format(
                                 len(growing), ", ".join(growing)))

            if holds >= verify.max_holds:
                # Probe 3 wrote off a file because a single immediate retry
                # after a two-second clearance still saw the challenge. So
                # the retry re-enters the hold — but not forever: a site
                # that asks again the moment a person answers is not
                # something to keep answering.
                verify.abandoned = (
                    "the verification prompt returned {} times on the same "
                    "file, so the run stopped rather than keep asking"
                    .format(holds))
                raise NotVerified(verify.stop_reason())
            # Loop: retry the file the prompt interrupted.
            continue
        # The fetch SUCCEEDED — and may still have met a prompt that
        # somebody answered before it could stop anything. **This is the
        # case the first live run got wrong**: it met one, the operator
        # cleared it in about two seconds, the file arrived, and the run
        # reported that no verification had been requested. A prompt
        # handled well is the most important kind to record, because it
        # is the one that proves somebody was there.
        return _done(got)


def _unstick_tab(fetcher, home_url, holds, announce=None):
    """Move the tab off a challenge page, once the file is safely here.

    **Only after a hold**, and only after the fetch has returned, since
    navigating during a transfer aborts it. Left alone, the tab stays
    on Cloudflare's page for the rest of the run, and the cheap signal
    — glancing at the tab while waiting — goes deaf: on 2026-09-15 it
    instead produced 145 prompts that never happened.
    """
    if not holds or not home_url:
        return
    fn = getattr(fetcher, "normalize_tab", None)
    if fn is None:
        return
    note = fn(home_url)
    if note and announce:
        announce(note)


def _record_carried_prompt(got, verify, announce=None):
    """Record a prompt the fetch met and somebody answered unprompted.

    **The case the first live run got wrong**: it met one, the operator
    cleared it in about two seconds, the file arrived, and the run
    reported that no verification had been requested. A prompt handled
    well is the most important kind to record, because it is the one
    that proves somebody was there.

    A function rather than a branch, because three paths now reach it
    and this module's standing defect is logic that exists on one path
    and not its twin.
    """
    if not getattr(got, "prompt", None):
        return
    verify.record(verify.requests, max(0, verify.since - 1),
                  got.prompt.get("cleared_in"),
                  got.prompt.get("observed", ""), paused=False)
    if announce:
        announce("A verification prompt appeared and was cleared without "
                 "stopping the run; recorded in the report.")


# ---------------------------------------------------------------------------
# Worker 1: map the hierarchy fresh (same crawl as the Hierarchy Mapping
# Tool), write the report workbook, and install it as this run's model.
# ---------------------------------------------------------------------------
def report_destinations(out_dir, report_dir, stamp):
    """Where the files go and where the reports go.

    Returns (run_dir, report_run_dir, split). With no report folder - or one
    naming the same place - both are the same run folder and the behavior is
    exactly what it was before the setting existed. Otherwise the reports
    get a run folder of their own, and are written flat: the filenames
    already carry the context, and the whole point of separating them is
    that the report folder can be handed on without the content.
    """
    run_dir = os.path.join(out_dir, "DC_FileDownloads_" + stamp)
    split = bool(report_dir) and \
        os.path.abspath(report_dir) != os.path.abspath(out_dir)
    report_run_dir = (os.path.join(report_dir, "DC_FileDownloads_" + stamp)
                      if split else run_dir)
    return run_dir, report_run_dir, split


def map_worker(session, out_dir, report_dir=""):
    reset_rate_state()
    set_note_hook(log)
    set_stop_hook(STOP_EVENT.is_set)
    # A hierarchy report is a report: it follows the report folder when one
    # is given. Default "" keeps every existing caller behaving as before.
    out_dir = report_dir or out_dir
    base_url = session["base_url"].rstrip("/")
    root = root_label(base_url)
    try:
        set_state(phase="starting", summary="", paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        set_state(phase="mapping")
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

        set_progress(0, 0, "Parsing structure table…")
        ctx_types = extract_contexts(html)
        if ctx_types is None:
            return fail(
                "Couldn't find the 'Contents of this site' table. Confirm "
                "you're logged in as an administrator in the debug Chrome "
                "window and that the base URL in Settings is correct.")
        if not ctx_types:
            return fail("No structures found on the listing page.")

        contexts = sorted(ctx_types)
        total = len(contexts)
        log("Found {} structures. Reading parent groups (~1 s each; "
            "Pause/Stop available)…".format(total))

        parents = {}
        t0 = time.time()
        for idx, ctx in enumerate(contexts, start=1):
            check_pause_stop()   # Stop during mapping abandons the map

            driver.get(edit_group_url(base_url, ctx))
            time.sleep(1.0)
            html = driver.page_source

            if looks_like_login_page(html, driver.current_url):
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and map again.".format(ctx))

            parent = extract_parent(html, base_url)
            if parent is None:
                time.sleep(2.0)  # page may not have settled — one retry
                parent = extract_parent(driver.page_source, base_url)
            if parent is None:
                log("No x_group input for '{}' — treating as direct child "
                    "of the platform.".format(ctx))
                parent = ""
            parents[ctx] = parent

            set_progress(idx, total, "{} → {} · {}".format(
                ctx, parent or root, eta_text(idx, total, t0)))

        set_state(phase="writing")
        set_progress(total, total, "Writing hierarchy report…")
        rows, max_levels = [], 1
        for ctx in sorted(parents):
            chain = build_chain(ctx, parents, root)
            max_levels = max(max_levels, len(chain))
            rows.append((ctx, ctx_types.get(ctx, ""), chain))

        fname = "DC_Hierarchy_Report_{}.xlsx".format(
            time.strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(out_dir, fname)
        write_hierarchy_workbook(path, rows, max_levels,
                                 _primary_rgb(session))
        log("Wrote " + path)

        set_model(build_model(rows, "mapped just now → " + fname))
        set_progress(total, total, "Done")
        set_state(phase="done", report_file=path,
                  outcome=outcome_tone(run_warnings()),
                  summary="{} structures mapped → {}. Choose the scope and "
                          "options below, then Start.".format(len(rows),
                                                              fname))
    except StopRequested:
        set_state(phase="idle", paused=False)
        set_progress(0, 0, "")
        log("Mapping stopped — no report written. Load an existing report "
            "or map again.")
    except Exception as e:
        fail("Unexpected error: {}: {}".format(e.__class__.__name__, e))


# ---------------------------------------------------------------------------
# Listing crawl — one record state at a time, so every item's state is
# known. The FIRST page of the first state is read from the driver (already
# navigated by the caller); every further page is fetched with urllib.
# ---------------------------------------------------------------------------
def _fetch_listing(url: str, cookie: str):
    """Fetch one listing page, dropping x_showall if the server refuses it.

    Returns (html, url_actually_used) - the caller needs the second value
    because paging resolves the next link against it.

    Proven both ways on 2026-09-08 against the real ETD collection: the
    `published` state answered x_showall=1 with all 3,106 records in one
    page, and another state answered HTTP 400. Rather than give up a
    parameter that saves 124 requests where it works, drop it only where it
    is refused, say so in the log, and page on.
    """
    try:
        return fetch_html(url, cookie, LISTING_TIMEOUT), url
    except (LoginRequired, RateLimited):
        # A session expiry and a throttle are not the page being too big,
        # and paging would meet the same wall. Let the caller deal.
        raise
    except urllib.error.HTTPError as e:
        if "x_showall=1" not in url:
            raise
        if e.code != 400:
            raise
        why = "the server refused x_showall with HTTP 400"
    except Exception as e:                # noqa: BLE001 - see below
        # ANY transport failure on the one-page request, not just an
        # explicit refusal.
        #
        # Measured: the ETD `published` state answered
        # `RemoteDisconnected: Remote end closed connection without
        # response` after about sixty seconds. That state is 3,106 records
        # in roughly 4.8 MB, and the same request had succeeded from
        # another network the evening before — so this is a marginal, intermittent cost of
        # asking for the whole thing at once, not a property of the state.
        #
        # x_showall is an OPTIMIZATION. When it fails for any reason the
        # answer is to page, which is ~125 small requests at the admin-page
        # gap: about two minutes, and reliable. Losing 3,106 records to
        # save two minutes is the wrong trade, and it is what happened —
        # the structure carried on with 270 records from another state and
        # the run silently became a different, much smaller test.
        if "x_showall=1" not in url:
            raise
        why = "the one-page request failed ({})".format(describe_error(e))

    plain = url.replace("&x_showall=1", "").replace("x_showall=1&", "")
    _note("listing: {} — asking for pages instead. This costs about {} extra "
          "requests and loses nothing.".format(why, LISTING_PAGE_SIZE))
    _throttle("page")
    return fetch_html(plain, cookie, LISTING_TIMEOUT), plain


def looks_like_admin_page(html: str) -> bool:
    """A page Digital Commons served us as an admin, not an error or a bounce.

    `window.pageData` is present on every admin page - confirmed against
    every captured sample, gallery and EdiKit alike. It is what lets a
    record state with NO RECORDS be told apart from a page we could not
    read, which are otherwise identical to a parser looking for a table
    that is not there.
    """
    return "pageData" in (html or "")


def crawl_listing(base_url, ctx, gallery, state, cookie, first_html=None,
                  allow_flip=True):
    """Crawl every page of one state's listing. Returns (rows, gallery) —
    `gallery` may flip when the type guess picked the wrong flavor and the
    other one matched. Raises LoginRequired / ValueError."""
    parse = parse_gallery_listing if gallery else parse_edikit_listing
    other = parse_edikit_listing if gallery else parse_gallery_listing
    url = (gallery_listing_url if gallery else edikit_listing_url)(
        base_url, ctx, state)

    if first_html is not None:
        html = first_html
        # The caller handed us a page it already had. Ask the same
        # question of it that _fetch_listing's own fetcher asks, because
        # otherwise a login page arrives here and is read as a record
        # state holding no records — which is what happened on the
        # urllib path on 2026-09-22, and this path had a standing XFAIL
        # saying it would happen here too. No landed URL to check on
        # this one: the caller has the driver, not us.
        if looks_like_login_page(html, ""):
            raise LoginRequired()
    else:
        html, url = _fetch_listing(url, cookie)
    page = parse(html)
    if page is None:                      # wrong flavor? try the other one
        flipped = other(html)
        if flipped is None:
            if looks_like_admin_page(html):
                # A valid admin page with no listing table is a record
                # state holding no records. On 2026-09-08 a series with
                # records in one state produced four warnings reading "no
                # recognizable item listing" for the four empty ones -
                # true of the markup, alarming, and wrong about the cause.
                # An empty state is the ordinary case, not a failure.
                _note("listing: no records in the '{}' state".format(
                    STATE_LABELS.get(state, state)))
                return [], gallery
            if not allow_flip:
                # The family is already established for this structure, so
                # an unreadable page means THIS RECORD STATE is unreadable -
                # not that the whole structure is the other kind. Guessing
                # again is how 'etd' came to request editor_gallery.cgi on
                # 2026-09-08 and be answered HTTP 400: a nonsensical request
                # whose failure then masked whatever the state had actually
                # returned.
                raise ValueError(
                    "no recognizable item listing for this record state "
                    "(the structure is already known to be a {} listing)"
                    .format("gallery" if gallery else "series/journal/ETD"))
            gallery = not gallery
            url = (gallery_listing_url if gallery else edikit_listing_url)(
                base_url, ctx, state)
            _throttle("page")
            html, url = _fetch_listing(url, cookie)
            page = (parse_gallery_listing if gallery
                    else parse_edikit_listing)(html)
            if page is None:
                raise ValueError("no recognizable item listing")
        else:
            gallery = not gallery
            page = flipped

    rows, seen = [], set()
    pages = 0
    while True:
        pages += 1
        new = 0
        for row in page["rows"]:
            if row["article"] not in seen:
                seen.add(row["article"])
                rows.append(row)
                new += 1
        nxt = page.get("next")
        if not nxt or new == 0 or pages >= MAX_LISTING_PAGES:
            # Say something only where there is something to say. A
            # structure is crawled once per record state, so an empty state
            # would otherwise announce a one-page success four times over -
            # and a gallery would be credited with x_showall, which appears
            # only in the EdiKit URL. Neither is true, and a log line that
            # is not true is the defect this module spent a day removing.
            if not gallery and pages == 1 and len(rows) > LISTING_PAGE_SIZE:
                _note(showall_note(len(rows)))
            elif pages > 1:
                # Not a request count: the first page of the first state
                # comes from the driver navigation, not from urllib.
                _note("listing: {} record(s) over {} page(s)"
                      .format(len(rows), pages))
            break
        check_pause_stop()
        url = urljoin(url, nxt)
        _throttle("page")
        html, url = _fetch_listing(url, cookie)
        page = (parse_gallery_listing if gallery
                else parse_edikit_listing)(html)
        if page is None:
            break
    return rows, gallery


# ---------------------------------------------------------------------------
# Per-item planning — decide which files to fetch for one record, honoring
# the run options. Returns a list of "jobs":
#   {"kind","visibility","version","version_date","url","orig_hint"}
# May fetch the item's pickvers / additional_files pages (urllib).
# ---------------------------------------------------------------------------
def plan_item_jobs(base_url, ctx, item, record_state, gallery, opts, cookie,
                   warn):
    posted = record_state in POSTED_STATES
    record_vis = "public" if posted else "hidden"
    jobs = []

    def want_vis(vis):
        return opts["hidden"] if vis == "hidden" else opts["public"]

    art = item["article"]

    # ---- access state, and optionally the embargo date -------------------
    # Access comes free with the listing. The embargo date does not: it
    # lives on the record's admin page, so it costs one extra request per
    # restricted record. That is why it is opt-in and why it is fetched
    # ONLY for records already flagged restricted — on the collection this
    # was measured against that is 23% of records rather than all of them.
    #
    # A failure here must cost the embargo date and nothing else. The
    # record's files are the point of the run, and losing them because a
    # metadata page would not load is the partial-failure-becomes-total-
    # loss shape this module has already had four times.
    release = item.get("release", "")
    access = item.get("access") or access_state(release)
    embargo = ""
    if opts.get("embargo") and access == ACCESS_RESTRICTED:
        _throttle("page")
        try:
            meta = parse_record_metadata(
                fetch_html(admin_record_url(base_url, ctx, art, gallery),
                           cookie))
            embargo = embargo_from_metadata(meta)
            if not embargo:
                warn("item {}: restricted, but its record page states no "
                     "embargo period".format(art))
        except LoginRequired:
            raise
        except Exception as e:
            warn("item {}: could not read the embargo date — {}; the "
                 "record's files are unaffected".format(
                     art, describe_error(e)))

    def tag(job):
        job["access"] = access
        job["release"] = release
        job["embargo"] = embargo
        return job

    # ---- primary: current version --------------------------------------
    # native_url is the fallback fetch_record_file() uses when this record
    # has no generated derivative behind the default content URL.
    if opts["primary"] and want_vis(record_vis):
        jobs.append({"kind": "primary", "visibility": record_vis,
                     "version": "current", "version_date": "",
                     "url": primary_file_url(base_url, ctx, art),
                     "native_url": native_file_url(base_url, ctx, art),
                     "orig_hint": ""})

    # ---- native: current version, alongside the derivative --------------
    # Opt-in, for preservation and migration work that wants the source
    # .docx as well as the stamped PDF. A record with no derivative gets its
    # native from the fallback above without this being checked, and the
    # dedupe at the end of this function keeps the two from colliding.
    if opts.get("native") and want_vis(record_vis):
        jobs.append({"kind": "native", "visibility": record_vis,
                     "version": "current", "version_date": "",
                     "url": native_file_url(base_url, ctx, art),
                     "native_url": "",
                     "orig_hint": ""})

    # ---- primary: every revision (all-versions mode) --------------------
    if opts["primary"] and opts["versions"] == "all":
        _throttle("page")
        # The page URL, kept because the links on it are resolved against
        # it. The revisions table serves document-relative hrefs -
        # `viewcontent.cgi?article=...` with no leading slash - and this
        # used to join them onto the site root, producing
        # https://host/viewcontent.cgi?... with the /cgi/ missing. Every
        # such URL was refused, so all-versions mode has never once
        # produced a working link. It went unnoticed because galleries have
        # empty revisions tables, where the mode is a silent no-op, and no
        # EdiKit structure had been run until 2026-09-08.
        pv_url = pickvers_url(base_url, ctx, art)
        revs = parse_pickvers(fetch_html(pv_url, cookie))
        if revs is None:
            warn("no View-revisions table for item {} — skipped "
                 "versions".format(art))
        else:
            label_versions(revs)
            for rev in revs:
                if not rev["files"]:
                    continue
                for f in rev["files"]:
                    # Per FILE since v1.34.2: the editor selects the
                    # current PDF and the current native separately.
                    if f["is_current"]:
                        vis = record_vis      # the live version
                    else:
                        vis = "public" if rev["show_prev"] else "hidden"
                    fvis = "hidden" if f["kind"] == "coverletter" else vis
                    if not want_vis(fvis):
                        continue
                    full = urljoin(pv_url, f["href"])
                    version = f["version"]
                    if (f["is_current"] and f["kind"] == "primary"
                            and is_unstamped_url(full)):
                        # The revisions table's copy of the current PDF
                        # is the UNSTAMPED one; the primary job above
                        # fetches the stamped derivative the public sees.
                        # Different bytes (one journal record: 254,280 against
                        # 235,659), so both are kept — and named apart,
                        # because "current" and "current_2" read as a
                        # duplicate. Decided with the operator (v1.34.2).
                        version = VERSION_CURRENT_UNSTAMPED
                    jobs.append({
                        "kind": f["kind"],
                        "visibility": fvis,
                        "version": version,
                        "version_date": rev["date"],
                        "url": full,
                        "native_url": (native_variant(full)
                                       if f["kind"] == "primary" else ""),
                        "orig_hint": "",
                    })

    # ---- supplemental files ---------------------------------------------
    # The listing's "Additional Files" count is NOT a gate, and using it as
    # one silently lost files. Checked on 2026-09-08: ETD article 4395 holds
    # two supplemental files — one shown, one hidden — while its listing cell
    # reads "-", which _listing_rows maps to af=0. The old condition then
    # skipped the Supplemental content page outright and both files vanished
    # from the run with no error and no warning. 3,221 of that structure's
    # 3,383 records read "-", so the skip was discarding an unknown quantity
    # of real files across the whole repository.
    #
    # The page is therefore always fetched when supplementals are wanted, at
    # the cost of one request per record. `af` is kept as a CROSS-CHECK
    # rather than a gate: a disagreement between what the listing claims and
    # what the page actually holds is worth reporting in either direction,
    # and the page always wins.
    if opts["supp"]:
        _throttle("page")
        # Same reasoning as the revisions page above. These hrefs happen
        # to be absolute today, which is why supplemental files worked
        # while revisions did not, but joining against the page they came
        # from is correct either way and costs nothing.
        af_url = additional_files_url(base_url, ctx, art)
        supp = parse_additional_files(fetch_html(af_url, cookie))
        af = item.get("af")
        if supp is None:
            # No table at all. Only worth saying so if the listing promised
            # files — otherwise this is the ordinary case for most records.
            if af:
                warn("item {}: the listing counts {} additional file(s) but "
                     "the Supplemental content page has no table".format(
                         art, af))
            supp = []
        elif af is not None and af != len(supp):
            warn("item {}: the listing counts {} additional file(s), the "
                 "Supplemental content page lists {} — using the page".format(
                     art, af, len(supp)))
        for n, f in enumerate(supp, start=1):
            vis = "public" if (f["shown"] and posted) else "hidden"
            if not want_vis(vis):
                continue
            jobs.append({"kind": "supp{}".format(n),
                         "visibility": vis,
                         "version": "current", "version_date": "",
                         "url": urljoin(af_url, f["href"]),
                         "native_url": "",
                         "orig_hint": f["name"]})
    return [tag(j) for j in dedupe_jobs(jobs)]


VERSION_CURRENT_UNSTAMPED = "current-unstamped"


def is_unstamped_url(url) -> bool:
    """True for a content link that asks for the unstamped file."""
    q = parse_qs(urlparse(url or "").query, keep_blank_values=True)
    return (q.get("unstamped") or [""])[0].strip().lower() == "yes"


def file_identity(job):
    """Which FILE a job fetches — not which address it fetches it from.

    v1.34.2, and the reason it exists is the adjacent-check failure once
    more: dedupe_jobs() compared URL text, and Digital Commons gives one
    file several addresses. In all-versions mode with natives on, a video
    record's single file was planned three times — the primary, which has
    no derivative and falls back to the native; the natives job, at the
    short path-form URL; and the current revision's native, at its dated
    CGI URL. Three keys, one file, saved as X, X_2 and X_3: 93 of 142
    files of one symposium collection in one run. A journal did the same
    to its current natives.

    A file is (kind, form, version, revision): the stamped default stream
    and the revisions table's unstamped copy are different bytes and stay
    two files; any address for the current native is the current native;
    an earlier revision is that revision, told apart by its date. Rows
    read back from a report carry the same fields, so a re-run asks the
    same question.

    What this cannot see at plan time: whether a primary will fall back to
    its native. That is learned when it is fetched — see SavedFiles.
    """
    kind = str(job.get("kind", ""))
    version = str(job.get("version", ""))
    form = ""
    if kind == "primary":
        form = ("unstamped" if (version == VERSION_CURRENT_UNSTAMPED
                                or is_unstamped_url(job.get("url", "")))
                else "stamped")
    current = version in ("current", VERSION_CURRENT_UNSTAMPED)
    return (kind, form, "current" if current else version,
            "" if current else str(job.get("version_date", "")))


def dedupe_jobs(jobs):
    """Drop jobs that would fetch the same FILE twice within one record.

    Keyed on file_identity(), not the URL (v1.34.2). First occurrence wins
    its position and its URL — for the current native that is the natives
    job's path-form URL, which is the one the network layer is asked about
    first. A later duplicate can still contribute what the first lacked:
    the revision's date, or a filename hint.
    """
    out, seen = [], {}
    for job in jobs:
        key = file_identity(job)
        if key not in seen:
            seen[key] = len(out)
            out.append(job)
            continue
        kept = out[seen[key]]
        if job.get("orig_hint") and not kept.get("orig_hint"):
            kept["orig_hint"] = job["orig_hint"]
        if job.get("version_date") and not kept.get("version_date"):
            kept["version_date"] = job["version_date"]
    return out


class SavedFiles:
    """The files already saved in this run, by record and identity.

    The half of the duplicate defect a planner cannot see. A primary with
    no derivative falls back to its native, and only the fetch learns that
    — so the natives job planned after it would fetch the same file again.
    Both workers hold one of these and ask it before every fetch: a job
    whose file is already on disk for the same record is not requested,
    not saved again, and gets no row of its own; the row that saved it is
    the record. The unreached-rows writers ask it too, so a run stopped
    between the two does not leave the duplicate for a re-run to fetch.

    Only a SAVED file counts. An absence or a failure leaves the later job
    to ask for itself: one extra request costs less than a file assumed.
    """

    def __init__(self):
        self._have = {}
        self.skipped = 0

    @staticmethod
    def _key(ctx, article, job):
        return (str(ctx), str(article)) + file_identity(job)

    def already(self, ctx, article, job):
        """The saved filename this job would duplicate, or ""."""
        return self._have.get(self._key(ctx, article, job), "")

    def saved(self, ctx, article, job, name):
        self._have.setdefault(self._key(ctx, article, job), name or "?")

    def skip(self):
        self.skipped += 1

    def summary(self):
        """One line for the log, or "" when nothing was skipped."""
        if not self.skipped:
            return ""
        return ("{} planned file(s) were a file already saved for the same "
                "record — a primary with no derivative falls back to its "
                "native — and were not fetched again.".format(self.skipped))


# ---------------------------------------------------------------------------
# Worker 2: the DOWNLOAD run.
# ---------------------------------------------------------------------------
def download_worker(session, out_dir, scope_mode, parents_sel, opts,
                    report_dir=""):
    reset_rate_state()
    set_note_hook(log)
    set_stop_hook(STOP_EVENT.is_set)
    set_cooldown_budget(opts.get("cooldowns", COOLDOWN_MAX_WAITS))
    set_request_delay(opts.get("delay", REQUEST_DELAY))
    set_page_delay(opts.get("page_delay", PAGE_DELAY))
    pending = None
    base_url = session["base_url"].rstrip("/")
    with LOCK:
        model = MODEL
    if model is None:
        return fail("No hierarchy is loaded.")

    targets = resolve_targets(model, scope_mode, parents_sel)
    ordered, excl_comm, excl_unknown = split_targets(model, targets)
    total = len(ordered)
    if total == 0:
        return fail("Nothing to download — the scope contains no "
                    "publication structures (communities are excluded "
                    "automatically).")

    states = ([s for s in POSTED_STATES] if opts["published"] else []) + \
             ([s for s in UNPOSTED_STATES] if opts["unpublished"] else [])

    primary = _primary_rgb(session)
    plan = plan_spec(opts)          # what an un-reached record would need
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir, report_run_dir, split_reports = report_destinations(
        out_dir, report_dir, stamp)
    master = []       # (order, ctx, type, records, files, hidden, warns,
                      #  status, time, report)
    tot_files = 0
    tot_failed = 0
    tot_restricted = 0
    tot_unknown = 0
    tot_absent = 0
    tot_unconfirmed = 0
    tot_403 = 0
    tot_inventory = 0
    # The session's allowance, spent across every structure in the run —
    # the server rations the account, not the structure. Plan-time sizing
    # is an estimate because no row knows its size before it is fetched;
    # this counts what actually arrives.
    budget = SessionBudget(int(opts.get("max_files", 0) or 0),
                           int(opts.get("max_mb", 0) or 0))
    # How files are fetched, and what happens when the site asks whether a
    # person is here. Both are made once and held for the whole run, and
    # both are handed to fetch_one(), which is the only place either
    # worker fetches through.
    fetch_path = opts.get("fetch_path", FETCH_BROWSER)
    verify = VerificationWatch(
        window=float(opts.get("verify_window", VERIFY_WINDOW)),
        announce=log, on_hold=show_verify_hold)
    fetcher = None
    # What the preflight observed, for the master log's Checks sheet.
    preflight_checks = []
    # The files saved so far, by identity — see SavedFiles.
    saved = SavedFiles()

    def flush_pending(how):
        """Write out the rows gathered for the structure in flight.

        Called from BOTH early-exit paths. It existed only on the stop path
        until 2026-09-08, which meant any unexpected error - a full disk on
        an overnight run being the obvious one - lost the record of
        everything already downloaded in that structure. Files on disk and
        nothing saying where they came from is the same loss a stop used to
        cause, and the reason this function exists at all.
        """
        nonlocal tot_files
        if not pending:
            return 0
        rows = pending["rows"]
        # Everything this structure was going to do and did not, written
        # before the report is, so the report is a complete account of the
        # structure rather than only of the part that ran.
        #
        # The record that was IN FLIGHT comes first: its remaining files
        # are rows that merge_rows would otherwise have no way to keep.
        # See unreached_job_rows().
        inflight = pending.get("inflight")
        start = pending["pos"][0]
        if inflight and inflight["index"] == start:
            # This record WAS reached and planned, so it must not also be
            # reported as one the run never got to — two claims about the
            # same record, one of them untrue.
            start += 1
            left = unreached_job_rows(
                inflight["jobs"], inflight["pos"][0], len(rows),
                inflight["item"], inflight["state_txt"],
                inflight["pub"], inflight["adm"],
                already=lambda job, _c=pending["ctx"],
                _a=inflight["item"]["article"]: saved.already(_c, _a, job))
            if left:
                rows = rows + left
                log("{} file(s) of article {} in '{}' were planned and not "
                    "reached — recorded as '{}' so a re-run collects them."
                    .format(len(left), inflight["item"]["article"],
                            pending["ctx"], NOT_ATTEMPTED))
        missed = not_attempted_rows(
            pending["items"], start, len(rows),
            base_url, pending["ctx"], pending["gallery"], pending["plan"])
        if missed:
            rows = rows + missed
            log("{} record(s) in '{}' were never reached — recorded as "
                "'{}' so a re-run can pick them up.".format(
                    len(missed), pending["ctx"], NOT_ATTEMPTED))
        if not rows:
            return 0
        pname = "DC_FileDownload_Report_{}_{}_PARTIAL.xlsx".format(
            safe_token(pending["ctx"]), stamp)
        try:
            write_structure_report(os.path.join(pending["dir"], pname),
                                   rows, primary)
        except Exception as e:
            log("WARNING: could not write the partial report — {}"
                .format(describe_error(e)))
            pname = ""
        done = sum(1 for r in rows if r[COL_STATUS] == "Downloaded")
        hid = sum(1 for r in rows if r[COL_STATUS] == "Downloaded"
                  and r[COL_VISIBILITY] == "hidden")
        # Account for every row, not just the ones that became files. The
        # first stopped v1.21 run wrote 110 rows and said "24 file(s)",
        # leaving 86 rows the log never mentioned: 11 files the server said
        # do not exist, and 75 records never reached. Both were correct in
        # the report and invisible in the log, which is the same defect as
        # a wrong number — someone reads the log, not the workbook.
        gone = sum(1 for r in rows
                   if str(r[COL_STATUS]).startswith(ABSENT_PREFIXES))
        never = sum(1 for r in rows if r[COL_STATUS] == NOT_ATTEMPTED)
        failed = len(rows) - done - gone - never
        parts = ["{} downloaded".format(done)]
        if failed:
            parts.append("{} failed".format(failed))
        if gone:
            parts.append("{} not present".format(gone))
        if never:
            parts.append("{} never reached".format(never))
        breakdown = ", ".join(parts)
        master.append(
            (pending["idx"], pending["ctx"], pending["type"],
             pending["n_items"], done, hid, len(warns),
             "{} mid-structure ({} row(s): {})".format(
                 how, len(rows), breakdown),
             timestr, pname))
        tot_files += done
        log("{} inside '{}' — {} row(s) ({}) written to {}".format(
            "Stopped" if how == "STOPPED" else "Failed", pending["ctx"],
            len(rows), breakdown, pname or "(report failed)"))
        return never

    def finish_master(partial_reason=None):
        if not master:
            return None
        set_state(phase="writing")
        set_progress(len(master), total, "Writing master log…")
        suffix = "_PARTIAL" if partial_reason else ""
        fname = "DC_FileDownload_MasterLog_{}{}.xlsx".format(stamp, suffix)
        path = os.path.join(report_run_dir, fname)
        # The audit trail rides with the run log, and it is written on
        # every path out of the run — including the partial ones, which
        # are precisely the runs a verification ended.
        write_master_workbook(path, master, primary,
                              verify.rows(), verify.summary(),
                              check_rows=preflight_checks)
        log("Wrote {}{}".format(
            path, " ({})".format(partial_reason) if partial_reason else ""))
        set_state(last_file=path)
        return path

    try:
        set_state(phase="starting", summary="", last_file=None,
                  run_dir=None, paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return
        report = getattr(driver, "_dc_timeout_report", "")
        if report:
            # Said out loud so that a 120.0s failure can be recognized as
            # an unraised timeout rather than investigated from scratch.
            log("Chrome bridge — " + report)

        try:
            os.makedirs(run_dir, exist_ok=True)
            if split_reports:
                os.makedirs(report_run_dir, exist_ok=True)
                log("Reports go to {} — downloads to {}"
                    .format(report_run_dir, run_dir))
        except OSError as e:
            return fail("Couldn't create the run folder {} ({})."
                        .format(run_dir, e.__class__.__name__))
        set_state(run_dir=run_dir)
        log("Saving files into " + run_dir)

        # The fetch path, opened once and closed in the `finally` at the
        # bottom of this worker — every way out of the run goes through
        # it, which is what module contract 5 requires of anything that
        # repoints the browser's download folder.
        try:
            tab_ready_check(driver, base_url + "/", preflight_checks, log)
        except TabNotAnswering as e:
            return fail("Not started: {}".format(e))
        fetcher = make_fetcher(fetch_path, "",
                               os.path.join(run_dir, INCOMING_DIRNAME),
                               stall_limit=float(opts.get(
                                   "stall_limit", BROWSER_STALL_LIMIT)))
        fetcher.open(driver)
        try:
            for note in preflight_downloads(fetcher, out_dir,
                                            checks=preflight_checks):
                log(note)
        except NotConfigured as e:
            return fail("Not started (NotConfigured): {}".format(e))
        if fetch_path == FETCH_BROWSER:
            log("Fetching files through Chrome. You may be asked to verify "
                "that you are present; the run pauses until you do, waits "
                "{:.0f} minutes, and records each prompt in the report."
                .format(verify.window / 60.0))
        else:
            log("Fetching files over the network layer. This path cannot "
                "answer a request to verify that you are present — if one "
                "arrives, the run stops and says so.")

        set_state(phase="downloading")
        if excl_comm:
            log("Excluded {} community structure(s) — no uploads."
                .format(len(excl_comm)))
        if excl_unknown:
            log("Excluded {} chain-only intermediate parent(s) of unknown "
                "type.".format(len(excl_unknown)))
        log("Processing {} structure(s); record states: {}…".format(
            total, ", ".join(STATE_LABELS[s] for s in states)))

        t0 = time.time()
        for s_idx, ctx in enumerate(ordered, start=1):
            check_pause_stop()
            type_label = model["types"].get(ctx, "")
            gallery = type_label in GALLERY_TYPE_LABELS
            timestr = time.strftime("%H:%M:%S")
            warns = []

            def warn(msg, _ctx=ctx, _warns=warns):
                _warns.append(msg)
                log("WARNING: '{}' — {}".format(_ctx, msg))

            # -- one driver navigation, on the SITE ROOT ---------------
            #
            # Its whole job is to prove the admin session is live and
            # refresh the cookies. Everything else is fetched over urllib,
            # where the timeout and the retry logic are ours.
            #
            # It used to load the first listing page with x_showall=1 —
            # 3,383 rows and ~4.8 MB on the ETD collection — which
            # exceeded Selenium's 120s command default. v1.28 changed that
            # to the PAGED listing and two runs still died at
            # exactly 120.0s, so an `editor.cgi` navigation is not
            # dependable through the DevTools channel whatever its size.
            #
            # The site root is what retry_worker has always used and it
            # has never timed out. A login bounce still shows here, and a
            # structure-level bounce is caught by crawl_listing, which
            # raises LoginRequired.
            first_url = base_url + "/"
            first_html, _bounced = session_still_open(driver, base_url, log)
            if _bounced:
                master.append((s_idx, ctx, type_label, 0, 0, 0, len(warns),
                               "LOGIN REQUIRED", timestr, ""))
                finish_master("session expired; {} of {} structures done"
                              .format(s_idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and start again.".format(ctx))
            cookie = cookie_header(driver)
            fetcher.use_cookie(cookie)
            # A prompt that arrives is recorded against the structure being
            # worked when it arrived.
            verify.context = ctx
            # And the navigation just made is itself something the site
            # can challenge. Checked here because it was not checked at
            # all until the first live run met a prompt this worker
            # never saw. One title read per structure.
            hold_if_challenged(fetcher, verify,
                               "the site page it checks between "
                               "structures", warn)

            # -- crawl each requested state's listing ---------------------
            items = []                 # (item_row, state)
            seen_ids = set()
            listing_err = None
            state_errs = []
            # Set once any state's listing has parsed. After that the
            # editor family is a fact about this structure, not a guess to
            # be revisited per state.
            flavour_known = False
            try:
                for k, state in enumerate(states):
                    label = STATE_LABELS.get(state, state)
                    try:
                        # No first_html: the driver's page is the paged
                        # one, fetched only to prove the session. Handing
                        # it over would silently give up x_showall on the
                        # first state of every structure.
                        rows, gallery = crawl_listing(
                            base_url, ctx, gallery, state, cookie,
                            None, allow_flip=not flavour_known)
                    except (LoginRequired, StopRequested):
                        raise
                    except Exception as e:
                        # One state's listing failing must not discard the
                        # states that worked. On 2026-09-08 'etd' listed
                        # 3,106 records under `published` and then answered
                        # HTTP 400 for another state, and because this
                        # handler sat outside the loop the whole structure
                        # was skipped - 3,106 records thrown away over one
                        # refused request.
                        msg = describe_error(e)
                        state_errs.append("{}: {}".format(label, msg))
                        warn("listing for '{}' records failed — {}; "
                             "continuing with the other record states"
                             .format(label, msg))
                        continue
                    flavour_known = True
                    for row in rows:
                        if row["article"] not in seen_ids:
                            seen_ids.add(row["article"])
                            items.append((row, state))
                    _throttle("page")
            except LoginRequired:
                master.append((s_idx, ctx, type_label, 0, 0, 0, len(warns),
                               "LOGIN REQUIRED", timestr, ""))
                finish_master("session expired; {} of {} structures done"
                              .format(s_idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and start again.".format(ctx))
            except StopRequested:
                raise
            except Exception as e:              # nothing per-state caught
                listing_err = describe_error(e)

            # Only a structure that yielded NO records at all is skipped.
            # Partial listings proceed, with the failures recorded as
            # warnings against the structure.
            if listing_err is None and state_errs and not items:
                listing_err = "; ".join(state_errs)

            if listing_err is not None:
                master.append((s_idx, ctx, type_label, 0, 0, 0, len(warns),
                               "No item listing ({})".format(listing_err),
                               timestr, ""))
                log("WARNING: '{}' — {}; skipped.".format(ctx, listing_err))
                set_progress(s_idx, total, "{} · skipped · {}".format(
                    ctx, eta_text(s_idx, total, t0)))
                continue

            if not items:
                master.append((s_idx, ctx, type_label, 0, 0, 0, len(warns),
                               "No matching records", timestr, ""))
                set_progress(s_idx, total, "{} · empty · {}".format(
                    ctx, eta_text(s_idx, total, t0)))
                continue

            items.sort(key=lambda pair: int(pair[0]["article"]))

            # Whether this listing's Type column is a release option or a
            # plain document type is a fact about the structure, so it is
            # settled once, here, with every record in hand — not per row
            # and not per page.
            if resolve_listing_access([it for it, _s in items]) == "type-only":
                log("{}: this listing's Type column carries a document type, "
                    "not a release option, so it says nothing about access. "
                    "Rows are marked '{}'.".format(ctx, ACCESS_NA))

            # -- per-item download loop -----------------------------------
            t_struct = time.time()          # this structure's own pace
            _rs = rate_state()
            hits_before = _rs["hits"]
            file_before, page_before = _rs["hits_file"], _rs["hits_page"]
            ctx_dir = os.path.join(run_dir, safe_token(ctx) or "structure")
            os.makedirs(ctx_dir, exist_ok=True)
            rpt_dir = report_run_dir if split_reports else ctx_dir
            report_rows = []
            # Everything the stop handler needs to write out the rows for
            # the structure in flight. `rows` is the same list object the
            # loop appends to, so it stays current without being reset.
            # `pos[0]` is how many records of `items` have been finished.
            # It is a one-element list because the stop handler reads it
            # from outside the loop and needs the live value, not a copy
            # taken when the structure started.
            pending = {"idx": s_idx, "ctx": ctx, "type": type_label,
                       "dir": rpt_dir, "rows": report_rows,
                       "n_items": len(items), "items": items,
                       "gallery": gallery, "plan": plan, "pos": [0],
                       # The record being worked, and how far into its own
                       # job list the run got. See unreached_job_rows().
                       "inflight": None}
            n_files = n_hidden = n_absent = n_inventory = 0
            n_unconfirmed = 0
            order = 0
            expired_mid = False
            # Refusals since the last file served. The same object the
            # retry worker holds — see BlockWatch for why it is an object.
            watch = BlockWatch()

            for i_idx, (item, state) in enumerate(items, start=1):
                check_pause_stop()
                art = item["article"]
                state_txt = STATE_LABELS.get(state, state)
                pub_url = preview_record_url(base_url, ctx, art)
                adm_url = admin_record_url(base_url, ctx, art, gallery)

                try:
                    jobs = plan_item_jobs(base_url, ctx, item, state,
                                          gallery, opts, cookie, warn)
                except LoginRequired:
                    expired_mid = True
                    break
                except Exception as e:
                    # **Eleventh instance of "a partial failure must not
                    # escalate into total loss", and the live run of
                    # 2026-09-14 is where it was caught**: article 1222's
                    # admin page answered HTTP 502, the log said
                    # "skipped", and the record then appeared in NO row
                    # of the report at all — not downloaded, not absent,
                    # not un-reached. 1,093 of 1,094 records accounted
                    # for, and a re-run could never find the missing one,
                    # because a re-run reads the report.
                    #
                    # It is written as a RECORD row carrying the run's
                    # plan, which is exactly what an un-reached record
                    # gets, so the re-run re-plans it rather than trying
                    # to fetch an admin page.
                    msg = describe_error(e)
                    warn("item {}: {} while planning — recorded so a "
                         "re-run can plan it again".format(art, msg))
                    order += 1
                    report_rows.append(
                        (order, art, item.get("title", ""), state_txt,
                         RECORD_KIND, "", "", "", "", "", "",
                         "Failed: {} while planning".format(msg),
                         adm_url, pub_url, adm_url,
                         time.strftime("%H:%M:%S"), "",
                         item.get("access")
                         or access_state(item.get("release", "")),
                         item.get("release", ""), "", plan, ""))
                    # It WAS reached, so it must not also be reported as
                    # a record the run never got to — two rows making
                    # two different claims about one record.
                    pending["inflight"] = None
                    pending["pos"][0] = i_idx
                    continue

                # What flush_pending needs to record the files of THIS
                # record if the run ends before its job list does. Set
                # after planning, because before it there is no job list.
                inflight = {"index": i_idx - 1, "item": item,
                            "state_txt": state_txt, "pub": pub_url,
                            "adm": adm_url, "jobs": jobs, "pos": [0]}
                pending["inflight"] = inflight

                for j_idx, job in enumerate(jobs):
                    # Everything before this index has a row. Advanced at
                    # the TOP, so every way out of the body below — a
                    # stop, a full session, an unanswered verification —
                    # leaves the marker naming the first job with no row.
                    inflight["pos"][0] = j_idx
                    check_pause_stop()
                    # Checked BEFORE the next fetch rather than after the last
                    # one. Asking "is the budget spent?" at the top of the
                    # loop answers "and is there more to do?" for free: if
                    # the last file filled the session and nothing follows,
                    # the loops simply end and the run reports itself as
                    # finished. Raising on the way out of the final row
                    # instead would report a session that did exactly what
                    # the plan said as one that was cut short.
                    if budget.exhausted():
                        raise SessionFull(budget.reason())
                    if saved.already(ctx, art, job):
                        # The same file is already on disk for this
                        # record, saved by an earlier job. No request, no
                        # second copy, no row: the row that saved it is
                        # the record. See SavedFiles.
                        saved.skip()
                        continue
                    order += 1
                    if opts.get("inventory"):
                        # No request at all — not even a throttled one.
                        # Everything in this row came from admin pages the
                        # planner already fetched, and admin pages are not
                        # what the server is rationing.
                        n_inventory += 1
                        report_rows.append(
                            (order, art, item["title"], state_txt,
                             job["kind"], job["visibility"], job["version"],
                             job["version_date"], job["orig_hint"], "", "",
                             INVENTORIED, job["url"], pub_url, adm_url,
                             time.strftime("%H:%M:%S"), "",
                             job["access"], job["release"], job["embargo"],
                             "", ""))
                        continue
                    _throttle("file")
                    try:
                        # What fetch_one reports while it works — a long
                        # transfer's minute ticks, a prompt asked and
                        # cleared — is progress, not a warning. It was
                        # handed `warn` here and plain `log` in
                        # retry_worker, so on 2026-09-30 two healthy
                        # ticks for a 3 GB video were the run's "2
                        # warning(s)". Both workers now say the same.
                        got = fetch_one(fetcher, verify, job["url"],
                                        job.get("native_url", ""),
                                        order, fetch_says(ctx),
                                        home_url=base_url + "/")
                    except LoginRequired:
                        expired_mid = True
                        break
                    except StopRequested:
                        # NotVerified arrives through here, and it is a
                        # stop reason rather than a row outcome. Without
                        # this clause the `except Exception` below would
                        # swallow it and record "Failed:" on a file that
                        # was never refused — asserting something untrue
                        # about the run AND about the record.
                        raise
                    except Exception as e:
                        # An ABSENCE is not a FAILURE, and until 2026-09-09
                        # this reported both as "Failed:" and raised a
                        # warning for each. The first full run made the
                        # cost of that obvious: `native` was requested for
                        # 86 records in a series of 2017 PDF uploads, every
                        # one answered 404 — which is the server saying,
                        # definitely, that there is no separate native —
                        # and the log filled with WARNING lines that read
                        # like breakage on a run where nothing had broken.
                        #
                        # is_definite_absence() already draws exactly this
                        # line for fetch_record_file's fallback; the worker
                        # simply was not using it. A 404/410, an empty body
                        # or a web page where a file was expected is an
                        # answer. Anything else is the server declining to
                        # answer, and stays a failure.
                        msg = describe_error(e)
                        stop_after_row = ""
                        absent = (isinstance(e, NoFileAvailable)
                                  or is_definite_absence(e))
                        forbidden = (
                            isinstance(e, urllib.error.HTTPError)
                            and e.code == 403)
                        if forbidden:
                            watch.refused(art, job["access"])
                        elif absent:
                            watch.absent()
                        if absent:
                            n_absent += 1
                            status = "{}: {}".format(
                                ABSENT_LABELS.get(job["kind"], ABSENT_OTHER),
                                msg)
                        elif forbidden and watch.is_block():
                            # Recorded in the report, not repeated in the
                            # log. The one summary line below says it.
                            status = "Failed: " + msg
                        # Raising HERE would discard the very row that
                        # proved the block — the report would stop one row
                        # short of its own reason. The decision is taken
                        # now and acted on after the row is appended.
                            if watch.should_stop(
                                    opts.get("stop_when_blocked", True)):
                                stop_after_row = watch.stop_reason()
                            said = watch.take_message()
                            if said:
                                warn(said)
                        elif isinstance(e, NotConfirmedHere):
                            # Counted, not warned row by row: the summary
                            # line for the structure says it once.
                            n_unconfirmed += 1
                            status = "{}: {}".format(UNCONFIRMED, msg)
                        else:
                            status = "Failed: " + msg
                            warn("item {} {}: {}".format(
                                art, job["kind"], msg))
                        report_rows.append(
                            (order, art, item["title"], state_txt,
                             job["kind"], job["visibility"], job["version"],
                             job["version_date"], job["orig_hint"], "", "",
                             status, job["url"], pub_url, adm_url,
                             time.strftime("%H:%M:%S"), "",
                             job["access"], job["release"], job["embargo"],
                             "", ""))
                        if stop_after_row:
                            raise ServerRefusing(stop_after_row)
                        continue

                    # The derivative was absent and the native answered
                    # instead — relabel so the report names what was saved,
                    # and point the File URL link at what was actually got.
                    if got.used_native and job["kind"] == "primary":
                        job["kind"] = "native"
                        job["url"] = job["native_url"]

                    # No extension on the fallback name — build_filename
                    # derives one from the bytes, then the Content-Type
                    # (a native .docx must not be saved as .pdf). Only the
                    # leading bytes are read: on the browser path the file
                    # is already on disk and may be a 200 MB thesis.
                    orig = got.name or job["orig_hint"] \
                        or "article-{}".format(art)
                    nbytes = got.nbytes
                    fname = build_filename(ctx, art, job["kind"],
                                           job["visibility"], job["version"],
                                           orig, got.ctype, got.head())
                    try:
                        path = place_file(got, ctx_dir, fname)
                    except SaveFailed as e:
                        # One file, not the run. See SaveFailed.
                        warn("item {} {}: {}".format(art, job["kind"], e))
                        report_rows.append(
                            (order, art, item["title"], state_txt,
                             job["kind"], job["visibility"], job["version"],
                             job["version_date"], orig, "", "",
                             "Failed: {}".format(e), job["url"], pub_url,
                             adm_url, time.strftime("%H:%M:%S"), got.ctype,
                             job["access"], job["release"], job["embargo"],
                             "", fetched_via(got, fetcher)))
                        continue
                    n_files += 1
                    saved.saved(ctx, art, job, os.path.basename(path))
                    # The server handed over content, so whatever run of
                    # refusals was building, it is over.
                    watch.served()
                    budget.spend(nbytes)
                    if job["visibility"] == "hidden":
                        n_hidden += 1
                    report_rows.append(
                        (order, art, item["title"], state_txt, job["kind"],
                         job["visibility"], job["version"],
                         job["version_date"], orig, os.path.basename(path),
                         nbytes, "Downloaded", job["url"], pub_url,
                         adm_url, time.strftime("%H:%M:%S"), got.ctype,
                         job["access"], job["release"], job["embargo"], "",
                         fetched_via(got, fetcher)))

                if expired_mid:
                    break

                # This record is finished. Anything past it is un-reached
                # if the run ends here, so the marker moves only once the
                # record's whole job list has been attempted.
                inflight["pos"][0] = len(jobs)
                pending["inflight"] = None
                pending["pos"][0] = i_idx

                # Fractional: structures finished, plus how far into
                # this one. A single-structure run therefore climbs from 0
                # to 1 instead of sitting at 1 throughout.
                set_progress(run_position(s_idx - 1, i_idx, len(items)),
                             total,
                             "{} · item {}/{} · {} file(s) · {}".format(
                                 ctx, i_idx, len(items), n_files,
                                 eta_text(i_idx, len(items), t_struct)))

            # -- per-structure report -------------------------------------
            # A session expiry leaves the rest of the listing un-reached
            # just as a Stop does, and the same rows are written for it.
            if expired_mid:
                missed = not_attempted_rows(
                    items, pending["pos"][0], len(report_rows),
                    base_url, ctx, gallery, plan)
                if missed:
                    report_rows.extend(missed)
                    log("{} record(s) in '{}' were never reached — recorded "
                        "as '{}' so a re-run can pick them up.".format(
                            len(missed), ctx, NOT_ATTEMPTED))

            report_name = ""
            if report_rows and opts.get("inventory"):
                # One file: the inventory and the job that produced it.
                n_restr = sum(1 for r in report_rows
                              if r[COL_ACCESS] == ACCESS_RESTRICTED)
                n_unk = sum(1 for r in report_rows
                            if r[COL_ACCESS] == ACCESS_UNKNOWN)
                size, basis = suggest_session_size(
                    int(opts.get("observed_ceiling", 0) or 0))
                counts = {
                    "records": len(items), "files": len(report_rows),
                    "restricted": n_restr, "unknown": n_unk,
                    "session_size": size or len(report_rows),
                    "sessions": (
                        -(-len(report_rows) // size) if size else 1),
                    "basis": basis,
                }
                report_name = "DC_FileDownload_Plan_{}_{}.xlsx".format(
                    safe_token(ctx), stamp)
                write_plan_workbook(os.path.join(rpt_dir, report_name),
                                    report_rows, opts, counts, primary)
                log("{}: plan written — {} file(s) across {} record(s); "
                    "{}".format(ctx, len(report_rows), len(items),
                                "sessions of {} ({})".format(size, basis)
                                if size else
                                "one session takes it all"))
            elif report_rows:
                suffix = "_PARTIAL" if expired_mid else ""
                report_name = "DC_FileDownload_Report_{}_{}{}.xlsx".format(
                    safe_token(ctx), stamp, suffix)
                write_structure_report(os.path.join(rpt_dir, report_name),
                                       report_rows, primary)

            if expired_mid:
                master.append((s_idx, ctx, type_label, len(items), n_files,
                               n_hidden, len(warns), "LOGIN REQUIRED",
                               timestr, report_name))
                finish_master("session expired; {} of {} structures done"
                              .format(s_idx - 1, total))
                return fail(
                    "Digital Commons session expired at '{}'. Log in inside "
                    "the debug Chrome window and start again — files "
                    "already saved are kept.".format(ctx))

            # Say what happened, not what usually happens. On the first
            # complete v1.10 run this NOTE told the operator that "files reported
            # as rate limited exist — re-run this structure" on a run that
            # had recovered every one of its 40 rows. A message that
            # asserts a failure which did not occur is the same defect as
            # a report that asserts a file is missing when it is not.
            hits_now = rate_state()
            n_failed = sum(1 for r in report_rows
                           if str(r[COL_STATUS]).startswith("Failed"))
            if hits_now["hits"] > hits_before:
                log("NOTE: '{}' met server pushback {} time(s) — {} on file "
                    "downloads, {} on admin pages; the gaps are now {:.1f}s "
                    "before a download and {:.1f}s before a page. {}"
                    .format(ctx, hits_now["hits"] - hits_before,
                            hits_now["hits_file"] - file_before,
                            hits_now["hits_page"] - page_before,
                            request_delay(), page_delay(),
                            "Every row was fetched anyway."
                            if not n_failed else
                            "{} row(s) were left unfetched — those files "
                            "exist; re-run this structure.".format(
                                n_failed)))
            tot_failed += n_failed

            # Access state of what actually landed on disk. Counted from
            # the rows rather than from the listing, so it describes the
            # batch in the folder and not the collection it came from.
            n_restricted = sum(1 for r in report_rows
                               if r[COL_STATUS] == "Downloaded"
                               and r[COL_ACCESS] == ACCESS_RESTRICTED)
            n_unknown = sum(1 for r in report_rows
                            if r[COL_STATUS] == "Downloaded"
                            and r[COL_ACCESS] == ACCESS_UNKNOWN)
            tot_restricted += n_restricted
            tot_unknown += n_unknown

            # One line for the whole structure, instead of a WARNING per
            # record. Says what happened; explains the platform rule
            # without claiming to know why any particular record has no
            # native, which the module cannot tell from a 404.
            if n_absent:
                log("{}: {} requested file(s) do not exist — the server "
                    "answered 404/410 or served no file. Digital Commons "
                    "holds a separate native only when the upload was not "
                    "already a PDF.".format(ctx, n_absent))
            tot_absent += n_absent
            tot_unconfirmed += n_unconfirmed
            if n_unconfirmed:
                log(unconfirmed_line(ctx, n_unconfirmed))

            tot_inventory += n_inventory
            tot_403 += watch.total
            if watch.total:
                log("{}: {} file request(s) refused with HTTP 403 across {} "
                    "record(s){}. Those files exist; re-run this report "
                    "once the refusal clears.".format(
                        ctx, watch.total, len(watch.articles),
                        ", {} of them on records marked 'open access'"
                        .format(watch.open_access)
                        if watch.open_access else ""))

            tot_files += n_files
            if opts.get("inventory"):
                status = "Inventoried {} file(s)".format(n_inventory)
            else:
                status = "Downloaded {} file(s)".format(n_files)
            if watch.total:
                status += "; {} refused (403)".format(watch.total)
            if n_absent:
                status += "; {} not present".format(n_absent)
            if n_unconfirmed:
                status += "; {} not confirmed from this network".format(
                    n_unconfirmed)
            if n_restricted:
                status += "; {} access-restricted".format(n_restricted)
            if n_unknown:
                status += "; {} access unknown".format(n_unknown)
            if warns:
                status += "; {} warning(s)".format(len(warns))
            master.append((s_idx, ctx, type_label, len(items), n_files,
                           n_hidden, len(warns), status, timestr,
                           report_name))
            if opts.get("inventory"):
                log("{}: {} record(s), {} file(s) inventoried — nothing was "
                    "downloaded.".format(ctx, len(items), n_inventory))
            else:
                log("{}: {} record(s), {} file(s){}{}{}".format(
                ctx, len(items), n_files,
                ", {} hidden".format(n_hidden) if n_hidden else "",
                ", {} access-restricted".format(n_restricted)
                if n_restricted else "",
                ", {} access unknown".format(n_unknown) if n_unknown else ""))
            set_progress(s_idx, total, "{} · done · {}".format(
                ctx, eta_text(s_idx, total, t0)))

        path = finish_master()
        set_progress(total, total, "Done")
        if opts.get("inventory"):
            summary = ("INVENTORY ONLY — {} file(s) across {} structure(s) "
                       "recorded and none downloaded. Re-run this report to "
                       "fetch them.".format(tot_inventory, total))
        else:
            summary = "{} file(s) from {} structure(s) → {}".format(
                tot_files, total, run_dir)
        n_warn = sum(row[6] for row in master)
        if n_warn:
            summary += " · {} warning(s), see structure reports".format(
                n_warn)
        if tot_unconfirmed:
            summary += (" · {} file request(s) could not be confirmed from "
                        "this network".format(tot_unconfirmed))
        # Said plainly, because the folder on disk does not say it. These
        # files are posted and visible but not licensed to leave the
        # institution, and until 2026-09-09 they were reported as public.
        if tot_restricted:
            summary += (" · {} file(s) are ACCESS-RESTRICTED — see the "
                        "Access column before sharing this folder"
                        .format(tot_restricted))
        if tot_unknown:
            summary += (" · {} file(s) have an unrecognized release option; "
                        "treat them as restricted until checked"
                        .format(tot_unknown))
        if tot_403:
            summary += (" · {} file request(s) REFUSED with HTTP 403 — those "
                        "files exist and were not fetched; re-run this "
                        "report".format(tot_403))
        rl = rate_state()
        if rl["hits"]:
            # Attributed, because with two paces the only useful thing a
            # lockout can tell you is which one provoked it.
            summary += (" · met server pushback {} time(s) ({} on downloads "
                        "at {:.1f}s, {} on admin pages at {:.1f}s)".format(
                            rl["hits"], rl["hits_file"], rl["base"],
                            rl["hits_page"], rl["page_base"]))
            if rl["cooldowns"]:
                summary += ", waited out {} lockout(s)".format(
                    rl["cooldowns"])
            summary += (
                " · {} row(s) left unfetched — those files exist, re-run "
                "to collect them".format(tot_failed) if tot_failed
                else " · every row was fetched")
        else:
            # An absence of pushback is the finding when the paces differ,
            # so it is stated rather than left as silence.
            summary += (" · no server pushback at {:.1f}s before downloads "
                        "and {:.1f}s before admin pages".format(
                            rl["base"], rl["page_base"]))
        # Always, including when there was nothing to report: "no prompts"
        # and "never looked" are different claims and a summary that omits
        # the second reads as the first.
        summary += " · " + verify.summary()
        if path:
            summary += " · master log: " + os.path.basename(path)
        # Absences are the server answering, and access-restricted files
        # came down as asked: neither is a problem. A refusal, a file not
        # confirmed, a row left unfetched, an unrecognized release option
        # and any warning are.
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(
                      n_warn + tot_unconfirmed + tot_403 + tot_failed
                      + tot_unknown,
                      succeeded=None if opts.get("inventory") else tot_files))
    except StopRequested as _stop:
        # ServerRefusing subclasses StopRequested, so everything below
        # already does the right thing; only the wording differs.
        blocked = isinstance(_stop, ServerRefusing)
        full = isinstance(_stop, SessionFull)
        unverified = isinstance(_stop, NotVerified)
        # A stop must not throw away the rows already gathered for the
        # structure in flight. On 2026-09-08 a stop at item 33 of 38
        # discarded 33 downloaded rows: the files were on disk and nothing
        # recorded where they came from. _PARTIAL existed only for a
        # session expiry, which is not the common way a run ends early.
        never = flush_pending("STOPPED")
        if blocked:
            log("Stopped: {} — the server is refusing every file. Nothing "
                "about the remaining records is known to be wrong; they "
                "were not asked for. Re-run this report when the refusal "
                "clears.".format(_stop))
        elif unverified:
            # Nothing went wrong. The site asked whether a person was
            # here, the run stopped and waited, and no one answered — so
            # this must not be worded as a failure, and the remaining
            # files must not be described as missing. They were never
            # requested.
            log("Stopped: {}. Nothing is known to be wrong with the "
                "remaining files — they were not asked for. Everything "
                "already fetched is kept and the rest is recorded and "
                "resumable; start again when you can watch the Chrome "
                "window.".format(_stop))
        elif full:
            # The plan said this much, and this much was done. Reporting
            # that as an interruption would assert something untrue about
            # a run in which nothing went wrong.
            log("Session complete — {}. The remaining records are recorded "
                "and resumable; start the next session from this run's "
                "folder when the allowance refreshes.".format(_stop))
        else:
            log("Stop requested — {}.".format(
                structures_done_text(master, total)))
        path = finish_master("stopped early; {}".format(
            structures_done_text(master, total)))
        if path:
            # Say that the run is resumable, and how. A stopped run now
            # records the records it never reached, and that is worth
            # nothing at all if the person reading the summary does not
            # know it happened.
            if blocked:
                # The count comes from the exception, not from a loop
                # variable sniffed out of scope.
                summary = ("STOPPED — the server is refusing every file "
                           "({}). {} file(s) were downloaded before that; "
                           "the rest are recorded and resumable."
                           .format(_stop, budget.files))
            elif unverified:
                summary = ("Stopped — {}. {} file(s) downloaded before "
                           "that; the rest were not requested and are "
                           "recorded and resumable."
                           .format(_stop, budget.files))
            elif full:
                summary = ("Session complete — {}. {} file(s) downloaded; "
                           "the rest are recorded and resumable."
                           .format(_stop, budget.files))
            else:
                # Not tot_files: that only accumulates once a structure
                # finishes, so a stop part-way through one reported the
                # files of every structure BEFORE it and none of its own.
                summary = ("Stopped early ({}, {} file(s)).".format(
                    structures_done_text(master, total), budget.files))
            if never:
                summary += (" {} record(s) were never reached and are "
                            "recorded as '{}' — re-run this report to "
                            "collect them.".format(never, NOT_ATTEMPTED))
            # The live run of 2026-09-14 held for two verification
            # prompts and its on-screen summary said nothing about
            # either: the line was appended on the path where a run
            # finishes, and a long run almost always ends on this one
            # instead. The monitoring record has to be where the
            # operator actually looks.
            summary += " " + verify.summary()
            summary += " Partial master log: " + os.path.basename(path)
            # A Stop the operator pressed, or a session that spent its
            # planned allowance, is what was intended. A server refusing
            # every file, or a prompt nobody cleared, is not; a refusal
            # before any file came down is red.
            set_state(phase="done", paused=False, summary=summary,
                      outcome=outcome_tone(
                          sum(row[6] for row in master)
                          + (1 if blocked or unverified else 0),
                          succeeded=budget.files if blocked else None))
        else:
            set_state(phase="idle", paused=False)
            set_progress(0, 0, "")
            log("Stopped before any structure was processed.")
    except Exception as e:
        flush_pending("FAILED")
        finish_master("unexpected error; {}".format(
            structures_done_text(master, total)))
        fail(unexpected_failure(e))
    finally:
        # Module contract 5: a browser-driven download sets the download
        # folder back. In a `finally` and not at the end of the happy
        # path, because this worker leaves through `return fail(...)` in
        # five places, and the cost of missing one is the person's next
        # manual download landing silently in a run folder.
        #
        # UNGUARDED BEHAVIORALLY, and said here rather than left to look
        # covered. The suite cannot drive this worker — it needs Chrome
        # and a loaded hierarchy — so the only check on this block is
        # structural, and planting proved that structural check adjacent:
        # gutting the restore while leaving `finally:` and
        # `fetcher.close()` in the source kept it green. retry_worker's
        # identical block IS covered behaviorally, which is the argument
        # for the two staying identical.
        #
        # close() reports rather than raises, so a tidy-up problem cannot
        # replace whatever actually ended the run.
        if fetcher is not None:
            for note in fetcher.close():
                log(note)
        log(verify.summary())
        if saved.summary():
            log(saved.summary())
        save_run_log(report_run_dir, stamp)


# ---------------------------------------------------------------------------
# Records the run never reached.
#
# A run that ends early — Stop, a session expiry, a full disk — used to
# record only the rows it ATTEMPTED. Everything past the point it stopped
# appeared nowhere at all, so the re-run control could not pick it up: it
# recovered failures, and a record that was never tried is not a failure,
# it is an absence. That is fine for a test and useless for the actual job,
# which is working a 3,383-record collection across several nights.
#
# So an early exit now writes one row per un-reached record. Two things make
# such a row resumable:
#
#  * Its File URL is the record's admin URL, which names the article and
#    carries the context — a real address, not a placeholder.
#  * Its Plan cell records what the original run WOULD have fetched for it.
#    The re-run needs this because a record's real jobs cannot be known
#    without fetching its revisions and supplemental pages; the retry
#    therefore re-plans these rows rather than re-fetching a URL. Carrying
#    the intent in the report (rather than reading whatever boxes happen to
#    be ticked at re-run time) keeps the recovery traceable to the run that
#    was interrupted.
#
# A record row whose Plan cell is empty is reported as unretryable rather
# than fetched. Its File URL is an HTML admin page, and fetching it would
# write a web page to disk under a filename that claims to be the record's
# file — a plausible, wrong result of exactly the kind this module keeps
# having to unlearn.
# ---------------------------------------------------------------------------
NOT_ATTEMPTED = "Not attempted"
RECORD_KIND = "record"

# How many consecutive HTTP 403s before the run says, once, that this is a
# block rather than a per-record permission problem.
#
# Measured from outside the institution's network: the ETD collection
# downloaded 99 files
# and then answered 403 to 80 consecutive requests. Every one of those 80
# was on a record whose release option reads "open access", and four
# ACCESS-RESTRICTED files had already downloaded successfully — so the
# refusal has nothing to do with what the records permit. It is a ceiling
# on the session or the address.
#
# The module cannot know the cause, so it does not claim one. It reports
# what is observable: a run of refusals, how many records it spans, and
# that it includes records marked open access. Eighty identical warnings
# say the same thing eighty times and imply eighty separate problems.
BLOCK_RUN_403 = 6

# Consecutive refusals before the run gives up on the structure entirely.
# Higher than the reporting threshold on purpose: saying "this looks like a
# block" is cheap and reversible, ending the run is neither. Twenty at the
# download gap is about two minutes of evidence.
BLOCK_STOP_403 = 20


class BlockWatch:
    """Decides when a series of HTTP 403s is one block, and when to stop.

    **This exists as a class because it used to exist as six local variables
    inside download_worker, and retry_worker never got a copy.** On
    2026-09-11 a re-run met sixty refusals in an unbroken row: it printed
    sixty identical WARNING lines and never stopped, because none of the
    detection was in the code path a re-run takes. That is the eighth
    instance of the same shape — a failure mode fixed in one worker and not
    the other — so the logic is a single object that both workers hold, and
    there is no second copy to forget.

    It also corrects what the counter *counted*. The original reset on any
    outcome that was not a 403:

        if forbidden: run_403 += 1
        else:         run_403 = 0

    which is wrong in both directions. A 404 reset it — but a 404 hands over
    no content, so it is no evidence the server has anything left to give;
    a block interleaved with missing natives could stay invisible. And a
    SUCCESSFUL download never reset it at all, because a success does not
    reach the `except` clause the counter lived in — so refusals scattered
    across fifty good downloads could accumulate into a false block.

    So the run here means **refusals since the last file the server actually
    served**:

        served()   a file came down, so whatever the run was, it is over
        absent()   the server answered that there is nothing there: neutral,
                   because no content changed hands
        refused()  a 403

    `say_at` and `stop_at` are parameters rather than reads of the module
    constants so that a caller — including a test — can drive the thresholds
    without rewriting the module. They default to the constants above.
    """

    def __init__(self, say_at=None, stop_at=None):
        self.say_at = BLOCK_RUN_403 if say_at is None else say_at
        self.stop_at = BLOCK_STOP_403 if stop_at is None else stop_at
        self.run = 0             # refusals since the last file served
        self.total = 0           # refusals seen at all
        self.articles = set()    # records those refusals spanned
        self.open_access = 0     # ... of them marked "open access"
        self.said = False        # the one summary line has been emitted

    def served(self):
        """A file actually came down. Whatever the run was, it is over."""
        self.run = 0

    def absent(self):
        """The server answered, and the answer was that there is nothing.

        Deliberately not a reset and deliberately not a refusal: no content
        changed hands, so it says nothing either way about the budget. The
        method exists rather than being an omission at the call site so
        that "an absence is neutral here" is a decision written down once,
        not an accident of which branch forgot to call something.
        """

    def refused(self, article="", access=""):
        """One HTTP 403 on a file request."""
        self.run += 1
        self.total += 1
        if article != "":
            self.articles.add(article)
        if access == ACCESS_OPEN:
            self.open_access += 1

    def is_block(self):
        """True once this looks like one block rather than separate rows.

        Stays true once it has been said, so every later refusal is recorded
        in the report without a warning line of its own.
        """
        return self.said or self.run >= self.say_at

    def should_stop(self, enabled=True):
        """True when the run is long enough to give up on the structure."""
        return bool(enabled) and self.run >= self.stop_at

    def stop_reason(self):
        return ("{} file request(s) refused since the last file the server "
                "served, across {} record(s)"
                .format(self.run, len(self.articles)))

    def take_message(self):
        """The ONE summary line, or "" once it has already been given.

        Returns rather than logs, so the same object works in a worker, in
        a test, and anywhere else without a logging hook. Marking it said is
        the point of `take` being in the name.

        It checks the threshold itself rather than trusting the caller to
        have checked. Both workers do check, but an object that arms itself
        when it has nothing to say is one refactor away from swallowing the
        real announcement.
        """
        if self.said or self.run < self.say_at:
            return ""
        self.said = True
        return ("the server has refused {} file request(s) since the last "
                "file it served, across {} record(s){}. A 403 is 'you may "
                "not have this', so this is a block on this session or "
                "address, not a permission problem with these records. "
                "Further 403s are recorded in the report without a line "
                "each.".format(
                    self.run, len(self.articles),
                    " — including {} marked 'open access'".format(
                        self.open_access) if self.open_access else ""))


# Status prefixes for a DEFINITE absence — the server answered, and the
# answer was that there is nothing there. Kept distinct from "Failed:" so
# that a report, a warning count, a run summary and the re-run control all
# agree about which rows represent something that went wrong. `native` gets
# its own wording because its absence is the ordinary case: Digital Commons
# holds a separate native only when the upload was not already a PDF, so
# asking for one and being told 404 is information, not breakage.
# Inventory mode: the row is a file that EXISTS and was deliberately not
# fetched. Distinct from a failure (nothing went wrong), from an absence
# (the file is there), and from "not attempted" (the record was reached and
# fully planned). It re-runs like any other unfetched row, which is the
# point: inventory the whole collection cheaply, then spend each day's
# download budget on the rows you choose.
INVENTORIED = "Inventoried"

ABSENT_OTHER = "No file"
ABSENT_LABELS = {
    "native": "No native file",
    "primary": "No file",
}
ABSENT_PREFIXES = tuple(sorted(
    set(ABSENT_LABELS.values()) | {ABSENT_OTHER}))

_PLAN_FLAGS = ("primary", "supp", "native", "public", "hidden",
               "published", "unpublished", "embargo")


def plan_spec(opts: dict) -> str:
    """The run's download intent, as a compact string for the report."""
    parts = [name for name in _PLAN_FLAGS if opts.get(name)]
    parts.append("versions=" + str(opts.get("versions", "current")))
    return ",".join(parts)


def parse_plan_spec(text: str) -> dict:
    """Turn a Plan cell back into an opts dict. {} when it says nothing."""
    text = (text or "").strip()
    if not text:
        return {}
    opts = {name: False for name in _PLAN_FLAGS}
    opts["versions"] = "current"
    seen = False
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("versions="):
            value = token.split("=", 1)[1].strip()
            if value in ("current", "all"):
                opts["versions"] = value
                seen = True
            continue
        if token in opts:
            opts[token] = True
            seen = True
    return opts if seen else {}


def not_attempted_rows(items, start, order_from, base_url, ctx, gallery,
                       plan, when=None):
    """Report rows for the records at items[start:] — one row each.

    `items` is the (item, state) list the structure enumerated; `start` is
    the index of the first record NOT reached. Pure, so it can be tested
    without Chrome, a hierarchy or a live session — which is the only way
    anything inside download_worker ever gets tested.
    """
    stamp = when or time.strftime("%H:%M:%S")
    rows = []
    for offset, (item, state) in enumerate(items[start:]):
        art = item["article"]
        release = item.get("release", "")
        rows.append((
            order_from + offset + 1, art, item.get("title", ""),
            STATE_LABELS.get(state, state), RECORD_KIND, "", "", "",
            "", "", "", NOT_ATTEMPTED,
            admin_record_url(base_url, ctx, art, gallery),
            preview_record_url(base_url, ctx, art),
            admin_record_url(base_url, ctx, art, gallery),
            stamp, "",
            item.get("access") or access_state(release), release, "",
            plan, ""))
    return rows


def unreached_job_rows(jobs, start, order_from, item, state_txt,
                       pub_url, adm_url, when=None, already=None):
    """Rows for the files of the record that was IN FLIGHT when a run ended.

    **Tenth instance of the shape this module keeps meeting, and the first
    one found by reading rather than by losing data.** A run that stops
    part-way through a record wrote file rows for the jobs it had attempted
    and a "Not attempted" RECORD row for the record itself — and
    merge_rows() then correctly drops that record row, because a record
    with file rows has by definition been planned. The jobs the run never
    reached were therefore in no file anywhere, exactly as 199 rows were on
    2026-09-11.

    Verified against the real merge before this function existed: one
    Downloaded row plus one record row for the same article, merged, leaves
    the file row alone.

    It matters much more now than it did. A stop used to arrive at a
    structure boundary or on a wall of refusals; a verification prompt
    arrives wherever it arrives, which is usually the middle of a record.

    These are real file URLs, so they re-run by fetching rather than by
    replanning, and they need no Plan cell. Pure, so the rule is testable
    without Chrome.

    `already(job)` (v1.34.2) names a job whose file this run has already
    saved for the record — SavedFiles.already — and such a job gets no
    row: written as not attempted, a re-run would fetch the duplicate the
    run itself declined to.
    """
    stamp = when or time.strftime("%H:%M:%S")
    rows = []
    for job in jobs[start:]:
        if already is not None and already(job):
            continue
        rows.append((
            order_from + len(rows) + 1, item["article"], item.get("title", ""),
            state_txt, job["kind"], job["visibility"], job["version"],
            job["version_date"], job["orig_hint"], "", "", NOT_ATTEMPTED,
            job["url"], pub_url, adm_url, stamp, "",
            job["access"], job["release"], job["embargo"], "", ""))
    return rows


# ---------------------------------------------------------------------------
# Worker 3: re-run the rows a previous report says did not download.
#
# Why this exists. Digital Commons answers a sustained run with a fixed
# lockout (2026-09-08: ten minutes, absolute expiry, ~30 requests in), so a
# large collection cannot be fetched in one pass however politely it is
# paced. Re-running the whole structure to recover ten rows means re-fetching
# everything already on disk, which provokes the next lockout. Reading the
# report and re-attempting only what failed is what makes a big collection
# practical in chunks - and it needs no hierarchy at all, because the report
# already names every URL.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Merging several sessions' reports.
#
# A long job is worked in sessions because Digital Commons caps how many
# files it will hand over per day. Each session writes its own report, so
# after four nights a run folder holds four of them and the question "what
# is still outstanding?" is answered by all four together, not by any one.
#
# Without this, session 4 re-requests what session 3 downloaded — and on a
# capped allowance that is not merely wasteful, it is the whole day's
# budget spent on files already on disk. It is also invisible: every one of
# those requests succeeds.
#
# Two kinds of row, and they supersede each other differently:
#
#  * A FILE row is identified by what it points at — context, article,
#    kind, version, version date and URL. A later report's status for the
#    same file wins.
#  * A RECORD row ("Not attempted") stands for a record nobody planned yet.
#    It is superseded not by another record row but by the FILE rows that
#    replanning it produced. Keying on the URL alone misses this: the
#    record row's URL is an admin page and its files' URLs are not, so the
#    record row would survive and be replanned every session forever.
# ---------------------------------------------------------------------------
def row_identity(row):
    """What makes two report rows the same row."""
    if row.get("kind") == RECORD_KIND:
        return ("record", row.get("context", ""), row.get("article", ""))
    return ("file", row.get("context", ""), row.get("article", ""),
            row.get("kind", ""), row.get("version", ""),
            row.get("version_date", ""), row.get("url", ""))


def merge_rows(sources):
    """Latest status wins, across reports given OLDEST FIRST.

    `sources` is a list of (source_name, rows). Pure, because the ordering
    and supersession rules are the whole risk and they are not reachable
    through the IO.
    """
    merged = {}
    # Which (context, article) gained real file rows, and in which source.
    planned_at = {}
    for seq, (name, rows) in enumerate(sources):
        for row in rows:
            row = dict(row)
            row["source"] = name
            row["_seq"] = seq
            merged[row_identity(row)] = row
            if row.get("kind") != RECORD_KIND:
                key = (row.get("context", ""), row.get("article", ""))
                planned_at.setdefault(key, seq)
                planned_at[key] = min(planned_at[key], seq)

    out = []
    for ident, row in merged.items():
        if ident[0] == "record":
            key = (row.get("context", ""), row.get("article", ""))
            # Superseded once the record was actually planned — in this
            # source or any later one.
            if key in planned_at and planned_at[key] >= row["_seq"]:
                continue
        out.append(row)
    # Within a record the PRIMARY comes first (v1.34.2), then the rest by
    # kind as before. Alphabetical put `native` ahead of `primary`, and a
    # primary with no derivative falls back to that very native — which
    # SavedFiles can only see once the primary has been fetched. Live,
    # 2026-10-02: article 1061 was stopped mid-transfer with both rows
    # Not attempted, and a re-run in alphabetical order would have saved
    # its video twice. Primary first is also the order a fresh Start
    # plans a record's jobs in.
    out.sort(key=lambda r: (r.get("context", ""),
                            int(r["article"]) if str(r.get("article", "")).isdigit()
                            else 0,
                            0 if r.get("kind") == "primary" else 1,
                            r.get("kind", "")))
    for row in out:
        row.pop("_seq", None)
    return out


def session_slice(rows, cap, max_mb=0, avg_mb=0.0):
    """The next session's work: as many outstanding rows as the caps allow.

    Pure, because "how much is left" is the number the operator plans
    against and getting it wrong is invisible — a session that quietly
    takes everything looks exactly like one that took its share.

    `cap` is the file limit; `max_mb` with `avg_mb` is the volume limit,
    and the two are applied together with the smaller winning. Either at
    or below 0 means "do not limit on this", and both off takes everything,
    which is what a structure smaller than an allowance wants and what
    every run did before sessions existed.

    The volume limit here is an ESTIMATE — no row knows its size until it
    is fetched. SessionBudget is what enforces it against real bytes.
    """
    caps = [c for c in (cap, (int(max_mb / avg_mb) if max_mb and avg_mb > 0
                              else 0)) if c and c > 0]
    if not caps:
        return list(rows), 0
    take = list(rows[:max(1, min(caps))])
    return take, max(0, len(rows) - len(take))


# ---------------------------------------------------------------------------
# SNAPSHOT DRIFT
#
# A plan is a snapshot and stays one — that is what makes it stable and
# re-runnable. But a session may run days after the plan was taken, and a
# row's Visibility and Access came from that snapshot. A record that has
# since been restricted would be downloaded and then LABELLED WITH THE OLD
# VALUE, which is the failure that matters here: the report is the only
# thing marking which files on disk are access-restricted.
#
# Not choking on drift was mostly already handled — a removed record's URL
# answers 404 (a definite absence: recorded, not retried) and a refused one
# answers 403, and neither stops a session. The hazard was never the crash.
# It was the label.
#
# One x_showall request per structure at session start answers all of it at
# once: every removal, every state change and every release-option change.
# That is an ADMIN page, which is not what the server rations, so it costs
# about a second and nothing from the download allowance.
#
# Records ADDED since the plan are counted and reported, and that is all.
# The operator was explicit that a snapshot missing new records is the correct
# behaviour, not a defect to fix.
DRIFT_GONE = "Skipped: no longer in this structure's listing"


def reconcile_rows(rows, live, planned=None):
    """Re-label planned rows from a fresh listing. Pure, and mutates `rows`.

    `live` maps article id -> {"access", "release", "record_state"} as the
    listing reads NOW. `rows` are the planned rows, each with those same
    keys as the plan recorded them.

    Returns a summary dict: `gone` (planned articles the listing no longer
    carries), `changed` ((article, field, was, now) tuples, already applied
    to the rows), `added` (a count — see above), and `checked`.

    A row that drifted is marked `row["drift"]`, so the caller can skip the
    departed ones without re-deriving why.
    """
    out = {"gone": [], "changed": [], "added": None,
           "checked": len(rows or []), "unreadable": False}
    live = live or {}
    if rows and not live:
        # NOTHING CAME BACK, AND THAT IS NOT EVIDENCE OF ANYTHING.
        #
        # Measured (v1.33.8). An expired session bounced the listing to
        # the login page, which was read as "no records in this state",
        # which arrived here as an empty `live` — and every outstanding
        # row was marked departed and skipped, with the report saying the
        # repository no longer carries them. One row that run; it would
        # have been all 51 on the next, and every row of an ETD re-run
        # after that.
        #
        # The asymmetry decides it. Requesting a record that really has
        # gone costs one request and gets an honest per-record answer.
        # Skipping one that is still there loses it silently and for
        # good, because the next re-run reads this report. So an empty
        # listing re-labels nothing, marks nothing gone, and says it
        # could not establish what the listing carries.
        out["unreadable"] = True
        return out
    # `planned` is every record the PLAN covers, which is not the same as
    # the records in `rows`: `rows` is what is still outstanding, and a
    # session's slice of that. Without it the count of records added since
    # the plan cannot be computed at all, and None means exactly that —
    # never 0, which would read as "nothing has been added".
    seen = set()
    for r in rows or []:
        art = str(r.get("article", ""))
        seen.add(art)
        current = live.get(art)
        if current is None:
            out["gone"].append(art)
            r["drift"] = "gone"
            continue
        for field in ("access", "release", "record_state"):
            was = (r.get(field) or "").strip()
            now = (current.get(field) or "").strip()
            # An EMPTY new value is not a change to empty. A listing that
            # does not express access at all (a faculty series, where the
            # Type column carries a document type) must not blank the
            # access the plan recorded — an absent reading is not a reading
            # of "none".
            if now and now != was:
                out["changed"].append((art, field, was, now))
                r[field] = now
                r["drift"] = "changed"
    if planned is not None:
        out["added"] = len([a for a in live if a not in set(planned)])
    return out


def drift_summary(report):
    """One log line for a reconciliation, or "" when nothing moved.

    States the absence as a result too: a session that checked and found
    nothing should say so, because "no drift" and "never checked" look
    identical in a log otherwise — and they are not the same claim.
    """
    if not report:
        return ""
    if report.get("unreadable"):
        return ("listing drift — the listing came back with no records at "
                "all, so nothing could be checked against it. {} planned "
                "row(s) keep the plan's labels and are requested as "
                "planned; an empty reading is not a record going away. If "
                "the run then finds nothing, the session may have expired "
                "— log in inside the debug Chrome window."
                .format(report.get("checked", 0)))
    gone, changed = report.get("gone") or [], report.get("changed") or []
    added = report.get("added")
    unknown = (" — records added since the plan cannot be counted from "
               "this folder, which carries no plan workbook, so nothing is "
               "claimed about them") if added is None else ""
    if not gone and not changed and not added:
        return ("checked {} planned row(s) against the listing as it reads "
                "now: nothing has been removed, re-stated or re-released "
                "since the plan was taken{}"
                .format(report.get("checked", 0), unknown))
    parts = []
    if gone:
        parts.append("{} row(s) name a record the listing no longer carries "
                     "({}) — those are skipped, not requested"
                     .format(len(gone), ", ".join(sorted(gone)[:8])
                             + (", ..." if len(gone) > 8 else "")))
    if changed:
        fields = sorted({f for _a, f, _w, _n in changed})
        # Count the RECORDS, not the change tuples. One record whose access
        # and release both moved is two tuples and one record, and the first
        # version of this line reported it as "2 row(s) changed" — a message
        # asserting something untrue, produced by counting the wrong list.
        who = {a for a, _f, _w, _n in changed}
        parts.append("{} record(s) changed since the plan ({}) and are "
                     "re-labelled from the listing, not from the plan"
                     .format(len(who), ", ".join(fields)))
    if added:
        # Reported, not acted on. A plan is a snapshot by design.
        parts.append("{} record(s) have been added since the plan was taken "
                     "and are not part of it".format(added))
    return "listing drift — " + "; ".join(parts) + unknown


def live_listing(base_url, ctx, gallery, states, cookie, warn=None):
    """Read a structure's listing NOW, as {article: {...}}.

    Crawls only the record states the plan actually used: a record that
    moved to a state outside the job is out of the job, and calling that
    "removed" would be a claim the module cannot support.

    Returns (live, failed_states). A listing that will not load is NOT
    fatal — reconciliation is a labelling improvement, not a precondition
    for downloading — so the caller is told which states went unread and
    says so, rather than reporting a clean check it did not perform.
    """
    live, failed = {}, []
    for state in states:
        label = STATE_LABELS.get(state, state)
        try:
            rows, gallery = crawl_listing(base_url, ctx, gallery, state,
                                          cookie, None, allow_flip=False)
        except (LoginRequired, StopRequested):
            raise
        except Exception as e:
            failed.append(label)
            if warn:
                warn("could not re-read the '{}' listing to check for "
                     "changes since the plan — {}. Rows for that state keep "
                     "the plan's labels.".format(label, describe_error(e)))
            continue
        resolve_listing_access(rows)
        for row in rows:
            live[str(row["article"])] = {
                "access": row.get("access", ""),
                "release": row.get("release", ""),
                "record_state": label,
            }
        _throttle("page")
    return live, failed


def report_sort_key(name):
    """Chronological order for report filenames.

    Every report carries a YYYYMMDD_HHMMSS stamp, which sorts correctly as
    text. Falling back to the name keeps behaviour defined for anything
    hand-renamed rather than guessing from the filesystem.
    """
    m = re.search(r"(\d{8}_\d{6})", os.path.basename(name))
    return (m.group(1) if m else "", os.path.basename(name))


RETRY_REQUIRED_COLUMNS = ("Article ID", "File Kind", "Visibility", "Version",
                          "Version Date", "Original Filename", "Status",
                          "File URL", "Admin Record URL",
                          "Public Record URL")


def read_failed_rows(path: str, include_unconfirmed: bool = False):
    """Rows from a previous report that did not download.

    `path` may be one report or a run folder holding several. Columns are
    read by NAME, never by position: this module already carried a
    hand-typed column index once, and a retry that read the wrong column
    would re-fetch the wrong thing while looking like it worked.
    """
    from openpyxl import load_workbook

    path = os.path.expanduser(path or "")
    if os.path.isdir(path):
        # EVERY session's report, not just the first kind. A folder worked
        # across four nights holds the original report and three retry
        # reports, and the retries are where the successes are — reading
        # only DC_FileDownload_Report_* would merge the failures and miss
        # every file that was recovered.
        found = []
        for pattern in ("DC_FileDownload_Plan_*.xlsx",
                        "DC_FileDownload_Report_*.xlsx",
                        "DC_FileDownload_Retry_*.xlsx"):
            found += glob.glob(os.path.join(path, pattern))
        found = sorted(found, key=report_sort_key)
        if not found:
            raise ValueError(
                "No plan or download reports in " + path)
    elif os.path.isfile(path):
        found = [path]
    else:
        raise ValueError("No such report or folder: " + path)

    sources, skipped, absent = [], [], 0
    n_unconfirmed = 0
    for rp in found:
        name = os.path.basename(rp)
        # BIND IT, so it can be closed. `load_workbook(...).active`
        # leaves the Workbook itself unreferenced, and with read_only=True
        # openpyxl holds the file open until it is closed — which nothing
        # could do here, because nothing had it.
        #
        # POSIX never noticed: a file can be unlinked while open. Windows
        # cannot, and on 2026-09-21 the first Windows CI run this project
        # has ever had failed nine tests on it, every one at
        # `shutil.rmtree` cleaning up a temp directory it could no longer
        # delete. In production it is worse than a test failure: every
        # re-run reads a report through here, so after one re-run the
        # operator cannot move, rename or overwrite that report until the
        # module process exits.
        #
        # Every other workbook reader in the suite already closed its
        # own — six modules, six closes, and this one. A rule that
        # reached all of a group but one, and the thing that hid it is
        # that chaining `.active` leaves no name to check.
        wb = load_workbook(rp, read_only=True, data_only=True)
        try:
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            try:
                head = list(next(it))
            except StopIteration:
                raise ValueError("Report has no rows: " + name)
            missing = [h for h in RETRY_REQUIRED_COLUMNS if h not in head]
            if missing:
                raise ValueError(
                    "{} does not look like a download report — missing "
                    "column(s): {}".format(name, ", ".join(missing)))
            idx = {h: head.index(h) for h in head if h}
            parsed = []

            for r in it:
                if not r or r[idx["Status"]] is None:
                    continue
                status = str(r[idx["Status"]])
                url = str(r[idx["File URL"]] or "").strip()
                adm = str(r[idx["Admin Record URL"]] or "").strip()
                article = str(r[idx["Article ID"]] or "").strip()
                ctx = ""
                for candidate in (adm, url):
                    if candidate:
                        ctx = parse_qs(urlparse(candidate).query).get(
                            "context", [""])[0]
                    if ctx:
                        break

                def cell(header, default=""):
                    i2 = idx.get(header)
                    return default if i2 is None else str(r[i2] or default)

                # EVERY row is parsed, not just the unfetched ones: the merge
                # needs to know what LATER sessions downloaded in order to
                # supersede what earlier ones left outstanding. Filtering here
                # is what made a multi-session folder unreadable.
                parsed.append({
                    "context": ctx,
                    "article": article,
                    "kind": str(r[idx["File Kind"]] or "primary"),
                    "visibility": str(r[idx["Visibility"]] or "public"),
                    "version": str(r[idx["Version"]] or "current"),
                    "version_date": str(r[idx["Version Date"]] or ""),
                    "orig_hint": str(r[idx["Original Filename"]] or ""),
                    "url": url,
                    "adm": adm,
                    "pub": str(r[idx["Public Record URL"]] or ""),
                    "was": status,
                    "source": name,
                    "title": cell("Title"),
                    "record_state": cell("Record State"),
                    "access": cell("Access"),
                    "release": cell("Type / Release Option"),
                    "embargo": cell("Embargo"),
                    "plan": parse_plan_spec(cell("Plan")),
                    "replan": str(r[idx["File Kind"]] or "") == RECORD_KIND,
                })
            sources.append((name, parsed))
        finally:
            wb.close()

    # Latest status wins across every session in the folder.
    merged = merge_rows(sources)

    rows = []
    for row in merged:
        status = row["was"]
        if status.startswith("Downloaded"):
            continue
        if status.startswith(ABSENT_PREFIXES):
            absent += 1
            continue
        if status.startswith(UNCONFIRMED) and not include_unconfirmed:
            n_unconfirmed += 1
            continue
        if row["replan"]:
            if not row["plan"]:
                skipped.append(
                    "{} record row for article {}: it was never attempted, "
                    "but the report records no Plan, so there is nothing to "
                    "reproduce. Re-run the structure instead.".format(
                        row["source"], row["article"] or "?"))
                continue
            if not row["adm"]:
                skipped.append(
                    "{} record row for article {}: no admin record URL"
                    .format(row["source"], row["article"] or "?"))
                continue
        elif not (row["url"] and row["article"] and row["context"]):
            # Never guess. A row we cannot address is reported as such
            # rather than turned into a request for something else.
            skipped.append("{} row for article {!r}: no {}".format(
                row["source"], row["article"] or "?",
                "file URL" if not row["url"] else
                "article id" if not row["article"] else "context"))
            continue
        rows.append(row)

    # Every record the PLAN knows about, per structure — and ONLY when the
    # job's full scope is actually in what was read.
    #
    # Twice on 2026-09-11 this reported records "added since the plan was
    # taken" that had not been added. First it measured against the rows
    # the session was working on: 270 in the listing, 14 in the session,
    # "256 added". Then it measured against every row in the folder, which
    # is better and still wrong: a folder of session reports records what
    # each session had OUTSTANDING, not what the job covers. 270 in the
    # listing, 61 in the folder, "209 added". None had been added either
    # time.
    #
    # The distinction the module cannot make from session reports alone is
    # "added since the plan" versus "in the plan, and not in this folder" —
    # they look identical. A PLAN WORKBOOK is the artifact that settles it,
    # because it enumerates every record in the job by construction. With
    # no plan workbook in hand the question is unanswerable, and {} says
    # that: not an empty job, an unknown one. Reporting a number here
    # rather than admitting the gap is what produced both wrong lines.
    has_plan = any(os.path.basename(f).startswith("DC_FileDownload_Plan_")
                   for f in found)
    known = {}
    if has_plan:
        for row in merged:
            ctx, art = row.get("context", ""), str(row.get("article", ""))
            if ctx and art:
                known.setdefault(ctx, set()).add(art)

    if n_unconfirmed:
        # One line, not one per row, and it says how to include them.
        skipped.append(
            "{} row(s) could not be confirmed from the network they were "
            "run on and were left out; tick \"Include rows that could not "
            "be confirmed\" to try them again".format(n_unconfirmed))
    return rows, skipped, absent, known


def retry_native_url(base_url: str, row: dict) -> str:
    """The native fallback for a retried row, or "" when there is none.

    Only a CURRENT primary has one. A row already labelled `native` is
    itself the native URL; a supplemental or a dated revision URL names one
    exact file, and falling back from it would fetch a different file and
    report it under this row's name.
    """
    if row["version"] != "current":
        return ""
    if row["kind"] != "primary":
        return ""
    return native_file_url(base_url, row["context"], row["article"])


def _plan_cell(plan):
    """The Plan column's text for a re-run row.

    read_failed_rows() hands the worker a PARSED plan — a dict — because
    that is what plan_item_jobs() needs. The report cell wants the token it
    was read from, and openpyxl refuses a dict outright, which is the only
    reason this was caught rather than shipping a column of "{}".
    """
    if isinstance(plan, dict):
        return plan_spec(plan) if plan else ""
    return plan or ""


def _written_identity(row):
    """(article, kind, url) for a finished report row, read BY NAME."""
    i = REPORT_HEADERS.index
    return (str(row[i("Article ID")]), str(row[i("File Kind")]),
            str(row[i("File URL")]))


def retry_not_attempted_rows(rows, start, order_from, when=None,
                             written=None):
    """Report rows for the re-run rows at rows[start:] — one each.

    The counterpart of not_attempted_rows() for the re-run worker, which
    never had one. v1.20 gave download_worker the rule that a run ending
    early must record the records it never reached, with the plan needed to
    resume them; retry_worker's _PARTIAL wrote only the rows it had
    attempted. On 2026-09-11 a session of 448 rows stopped at 249 and the
    other 199 were in no file anywhere — so resuming from that folder would
    have silently dropped them from the job.

    **That is load-bearing for the whole sessions architecture**, in which
    session 2 reads session 1's folder to learn what is left. Ninth instance
    of a failure mode fixed in one worker and not the other.

    Pure, so it can be tested without Chrome or a live session.
    """
    stamp = when or time.strftime("%H:%M:%S")
    # The marker names the last source row KNOWN to be finished, so the row
    # in flight when the session ended is inside this slice even though its
    # outcome was already recorded. Writing it again would put "Failed:
    # HTTP 403" and "Not attempted" in the same report for the same file —
    # a report contradicting itself, which is worse than either row alone.
    # Erring toward the duplicate and dropping it here is the safe
    # direction; erring toward omission loses the row entirely.
    done = {_written_identity(r) for r in (written or [])}
    # Supersession, the same rule merge_rows() applies across reports: a
    # RECORD row stands for a record nobody planned yet, and it is
    # superseded by the FILE rows that planning it produced — whose URLs
    # differ by construction, since a record row's URL is an admin page.
    # Identity alone therefore cannot drop a record row that was fully
    # expanded before the session ended.
    #
    # With both rules here, `pos` is an optimisation rather than the thing
    # correctness rests on: breaking the marker leaves the output identical
    # and no check fails. Recorded as such rather than counted as a guarded
    # behaviour.
    # Only FILE rows supersede. Excluding record rows here is unreachable
    # today — nothing writes a record row into `written` within one flush —
    # so no check fails when it is removed, and it is recorded as unguarded
    # rather than counted as a verified behaviour. It stays because
    # merge_rows() draws the same line, and two copies of one rule that
    # disagree is how the merge lost record rows in the first place.
    planned_out = {str(r[REPORT_HEADERS.index("Article ID")])
                   for r in (written or [])
                   if str(r[REPORT_HEADERS.index("File Kind")])
                   != RECORD_KIND}
    out = []
    for row in (rows[start:] if start is not None else []):
        art = str(row.get("article", ""))
        kind = str(row.get("kind", ""))
        ident = (art, kind, str(row.get("url", "")))
        if ident in done:
            continue
        if (kind == RECORD_KIND or row.get("replan")) and art in planned_out:
            continue
        out.append((
            order_from + len(out) + 1, row.get("article", ""),
            row.get("title", ""), row.get("record_state", ""),
            # A row that was already a record row stays one; a file row
            # keeps its kind, so resuming re-asks for that file and not for
            # the whole record again.
            row.get("kind", RECORD_KIND), row.get("visibility", ""),
            row.get("version", ""), row.get("version_date", ""),
            row.get("orig_hint", ""), "", "", NOT_ATTEMPTED,
            row.get("url", ""), row.get("pub", ""), row.get("adm", ""),
            stamp, "",
            row.get("access", ""), row.get("release", ""),
            row.get("embargo", ""), _plan_cell(row.get("plan")), ""))
    return out


def inflight_retry_rows(inflight, written, order_from, already=None,
                        when=None):
    """Rows for the unreached files of the record a re-run was planning.

    `inflight` is {"jobs": [...], "pos": [k]}: the record's expanded jobs
    and the index of the one last handed to the fetch loop. Everything
    before k has a row or was a file already saved; k itself may or may
    not (a stop arrives before its row, a refusal after it), so it is
    included unless a written row already names it. `already(job)` is
    SavedFiles.already: a duplicate the run declined is not left for the
    next re-run to fetch. Pure, so the rule is testable without Chrome.

    UNGUARDED, and said so rather than counted: the `already` filter here
    is unreachable today. The generator that feeds this worker asks
    SavedFiles before it moves the marker past a job, so a saved
    duplicate never sits at or after the marker. Planting its removal
    left every check green (v1.34.2). It stays because download_worker's
    writer needs the same filter and does reach it — its marker moves
    BEFORE the skip — and two copies of one rule that disagree is how
    this module keeps losing rows.
    """
    if not inflight:
        return []
    stamp = when or time.strftime("%H:%M:%S")
    done = {_written_identity(r) for r in (written or [])}
    out = []
    for job in inflight["jobs"][inflight["pos"][0]:]:
        art = str(job.get("article", ""))
        if (art, str(job.get("kind", "")), str(job.get("url", ""))) in done:
            continue
        if already is not None and already(job):
            continue
        out.append((
            order_from + len(out) + 1, job.get("article", ""),
            job.get("title", ""), job.get("record_state", ""),
            job.get("kind", ""), job.get("visibility", ""),
            job.get("version", ""), job.get("version_date", ""),
            job.get("orig_hint", ""), "", "", NOT_ATTEMPTED,
            job.get("url", ""), job.get("pub", ""), job.get("adm", ""),
            stamp, "", job.get("access", ""), job.get("release", ""),
            job.get("embargo", ""), "", ""))
    return out


def _flush_retry(pending, stamp, header_rgb):
    """Write the rows a re-run had gathered when it ended early.

    Also writes the rows it never reached. A row that was part-attempted
    when the session ended is written BOTH as whatever happened to it and
    as not-attempted; merge_rows() resolves that, because a record row is
    superseded by any file row for the same record. Duplicating a row the
    merge will drop is the safe direction — losing it is not.

    Returns the report's filename, or "" when there was nothing to write
    or the write itself failed — the caller says which, and a failed write
    must not swallow the exception that brought us here.
    """
    if not pending:
        return ""
    # The re-planned record in flight first, as download_worker does: its
    # unreached files are rows nothing else would write (v1.34.2).
    left = inflight_retry_rows(pending.get("inflight"), pending["rows"],
                               len(pending["rows"]),
                               already=pending.get("already"))
    pending["rows"].extend(left)
    never = retry_not_attempted_rows(
        pending.get("src") or [], (pending.get("pos") or [None])[0],
        len(pending["rows"]), written=pending["rows"])
    pending["rows"].extend(never)
    if not pending["rows"]:
        return ""
    name = "DC_FileDownload_Retry_{}_{}_PARTIAL.xlsx".format(
        safe_token(pending["ctx"]), stamp)
    try:
        write_structure_report(os.path.join(pending["dir"], name),
                               pending["rows"], header_rgb)
    except Exception as e:
        log("WARNING: could not write the partial retry report — {}"
            .format(describe_error(e)))
        return ""
    return name


def retry_worker(session, out_dir, report_path, opts, report_dir=""):
    reset_rate_state()
    set_note_hook(log)
    set_stop_hook(STOP_EVENT.is_set)
    set_cooldown_budget(opts.get("cooldowns", COOLDOWN_MAX_WAITS))
    set_request_delay(opts.get("delay", REQUEST_DELAY))
    set_page_delay(opts.get("page_delay", PAGE_DELAY))
    base_url = session["base_url"].rstrip("/")
    # Bound before anything that can raise, because both the `except`
    # clauses and the `finally` at the bottom read them. `pending` used
    # to be bound part-way down the try, which meant an exception raised
    # above it replaced the real error with an UnboundLocalError.
    pending = None
    fetcher = None
    # What the preflight observed, for the master log's Checks sheet.
    preflight_checks = []
    # The files saved so far, by identity — see SavedFiles.
    saved = SavedFiles()
    verify = VerificationWatch(
        window=float(opts.get("verify_window", VERIFY_WINDOW)),
        announce=log, on_hold=show_verify_hold)

    try:
        rows, skipped, absent, known = read_failed_rows(
            report_path,
            include_unconfirmed=bool(opts.get("retry_unconfirmed", False)))
    except Exception as e:
        return fail(describe_error(e))

    if not rows:
        # Say which of the two it is. "Nothing to re-run" on a report whose
        # every row records a definite absence is true but unhelpful, and
        # reads like the report was not understood.
        if absent:
            return fail(
                "Nothing to re-run: every row that did not download records "
                "a file the server said is not there ({} of them). Those do "
                "not become present on a second ask.".format(absent))
        return fail("That report has no failed rows — nothing to re-run. "
                    "(Only rows whose Status does not begin 'Downloaded' "
                    "are retried.)")

    # This session's share of what is still outstanding.
    outstanding = len(rows)
    # The file cap only. The megabyte cap is enforced against real bytes
    # by SessionBudget during the run, which is exact; estimating it here
    # too could only make this slice too SMALL, and a slice that is too
    # small wastes an allowance. Taking too many rows costs nothing — the
    # budget ends the session and the rest stay outstanding.
    rows, remaining = session_slice(rows, int(opts.get("max_files", 0) or 0))

    primary = _primary_rgb(session)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir, report_run_dir, split_reports = report_destinations(
        out_dir, report_dir, stamp)
    total = len(rows)
    # A cell, because expand() grows it as records become files.
    nonlocal_total = [total]
    by_ctx = {}
    for r in rows:
        by_ctx.setdefault(r["context"], []).append(r)
    # v1.34: progress and ETA by RECORDS, whose number the report fixes.
    # The row total grows as record rows are re-planned into files
    # (887 → 1,485 on 2026-09-28), so an ETA taken from it meant nothing.
    rec_total = len({(r["context"], r["article"]) for r in rows})
    rec_seen = set()

    def show_progress(ctx):
        set_progress(len(rec_seen), rec_total,
                     "{} · record {}/{} · {} file row(s) done · {}".format(
                         ctx, len(rec_seen), rec_total, done,
                         eta_text(len(rec_seen), rec_total, t0)))

    try:
        set_state(phase="starting", summary="", last_file=None,
                  run_dir=None, paused=False)
        driver = _attach_or_fail(session)
        if driver is None:
            return

        os.makedirs(run_dir, exist_ok=True)
        if split_reports:
            os.makedirs(report_run_dir, exist_ok=True)
            log("Reports go to {} — downloads to {}"
                .format(report_run_dir, run_dir))
        set_state(run_dir=run_dir, phase="downloading")
        n_replan = sum(1 for r in rows if r.get("replan"))
        # Say where this session sits in the whole job, not just what it
        # is about to do. "448 rows" says nothing about whether that is
        # the end of the work or a fortieth of it.
        if remaining:
            log("Session of {} row(s). {} outstanding in this plan, so {} "
                "remain after this one.".format(
                    len(rows), outstanding, remaining))
        else:
            log("Session of {} row(s) — all that is outstanding in this "
                "plan.".format(len(rows)))
        log("Re-running {} row(s) across {} structure(s) from {}"
            .format(total, len(by_ctx), report_path))
        if n_replan:
            log("{} of those are records the interrupted run never reached; "
                "their files will be planned again from the record.".format(
                    n_replan))
        if absent:
            log("{} row(s) recorded a file the server said is not there — "
                "not retried.".format(absent))
        for s in skipped:
            log("WARNING: skipped — " + s)

        # One navigation, to validate the session and refresh cookies. The
        # report gives us every URL, so this is the only driver use here.
        #
        # The fetcher is built BEFORE it, so the same check the download
        # worker makes can be made here: this navigation can be
        # challenged too, and neither worker looked at it until the
        # first live run.
        try:
            tab_ready_check(driver, base_url + "/", preflight_checks, log)
        except TabNotAnswering as e:
            return fail("Not started: {}".format(e))
        fetcher = make_fetcher(opts.get("fetch_path", FETCH_BROWSER), "",
                               os.path.join(run_dir, INCOMING_DIRNAME),
                               stall_limit=float(opts.get(
                                   "stall_limit", BROWSER_STALL_LIMIT)))
        fetcher.open(driver)
        try:
            for note in preflight_downloads(fetcher, out_dir,
                                            checks=preflight_checks):
                log(note)
        except NotConfigured as e:
            return fail("Not started (NotConfigured): {}".format(e))
        _html, _bounced = session_still_open(driver, base_url, log)
        if _bounced:
            return fail("Digital Commons session expired. Log in inside the "
                        "debug Chrome window and start again.")
        hold_if_challenged(fetcher, verify,
                           "the site page it checks before starting", log)
        cookie = cookie_header(driver)
        fetcher.use_cookie(cookie)

        # The session's allowance, spent across every structure in the
        # re-run. Same object download_worker holds: the server rations the
        # account, so a limit that lived in one worker would be no limit.
        budget = SessionBudget(int(opts.get("max_files", 0) or 0),
                               int(opts.get("max_mb", 0) or 0))
        # The same fetch path and the same verification state the download
        # worker holds, made the same way. This pair of functions has
        # produced four of the ten "fixed in one worker, not the other"
        # defects in this module, three of them in the past week, so both
        # are objects rather than inline logic and both workers fetch
        # through the one fetch_one(). The fetcher itself was built
        # above, before the session-validating navigation.
        if fetcher.path == FETCH_BROWSER:
            log("Fetching files through Chrome. You may be asked to verify "
                "that you are present; the run pauses until you do, waits "
                "{:.0f} minutes, and records each prompt in the report."
                .format(verify.window / 60.0))
        else:
            log("Fetching files over the network layer. This path cannot "
                "answer a request to verify that you are present — if one "
                "arrives, the run stops and says so.")
        master, tot_files, done = [], 0, 0
        n_rows = 0
        # Counted, not derived by subtraction from a number that moves.
        # v1.25 reported "recovered 153 of 75 failed row(s) · -78 still
        # failing": the summary used `total` — the ORIGINAL row count, 75 —
        # while re-planning had grown the real total to 228, so the
        # denominator was stale and the remainder went negative. A negative
        # count is the loudest possible version of a message asserting
        # something untrue, and it was produced by arithmetic on a figure
        # rather than by counting outcomes.
        tot_rows, tot_absent = 0, 0
        tot_unconfirmed = 0
        # What an early exit needs to write out. Same idea as
        # download_worker's `pending`: `rows` is the live list the loop
        # appends to, so it is current without being copied.
        # (bound at the top of the function, above the try)
        t0 = time.time()
        for c_idx, (ctx, ctx_rows) in enumerate(sorted(by_ctx.items()), 1):
            check_pause_stop()
            timestr = time.strftime("%H:%M:%S")
            ctx_dir = os.path.join(run_dir, safe_token(ctx) or "structure")
            os.makedirs(ctx_dir, exist_ok=True)
            rpt_dir = report_run_dir if split_reports else ctx_dir
            out_rows, n_files, n_hidden, warns = [], 0, 0, []
            n_absent = 0
            n_unconfirmed = 0
            # The same detector download_worker holds. Until 2026-09-11 it
            # was six local variables in that worker only, so a re-run that
            # met a block logged one warning per refusal and never stopped.
            watch = BlockWatch()
            # A prompt that arrives is recorded against the structure being
            # worked when it arrived.
            verify.context = ctx
            # `src` and `pos` are what lets an early exit write out the
            # rows it never reached — see retry_not_attempted_rows().
            pending = {"ctx": ctx, "dir": rpt_dir, "rows": out_rows,
                       "src": ctx_rows, "pos": [0], "inflight": None,
                       "already": (lambda job, _c=ctx: saved.already(
                           _c, job.get("article"), job))}

            # --- snapshot drift ------------------------------------------
            # The plan is a snapshot and stays one. This re-reads the
            # listing once, before fetching anything, so a record that has
            # been restricted since the plan was taken is not downloaded and
            # then labelled with the plan's old Access value. One admin page
            # per record state, which the server does not ration.
            drift = None
            if opts.get("reconcile", True):
                def _dwarn(msg, _c=ctx, _w=warns):
                    _w.append(msg)
                    log("WARNING: '{}' — {}".format(_c, msg))
                states = []
                for r in ctx_rows:
                    code = STATE_BY_LABEL.get(r.get("record_state", ""), "")
                    if code and code not in states:
                        states.append(code)
                if states:
                    gallery_guess = ("editor_gallery.cgi"
                                     in (ctx_rows[0].get("adm") or "").lower())
                    live, unread = live_listing(
                        base_url, ctx, gallery_guess, states, cookie, _dwarn)
                    # Only reconcile rows whose state WAS read. A state that
                    # would not load must not make its records look removed.
                    unread_labels = set(unread)
                    checkable = [r for r in ctx_rows
                                 if r.get("record_state", "")
                                 not in unread_labels]
                    drift = reconcile_rows(checkable, live,
                                           planned=known.get(ctx))
                    line = drift_summary(drift)
                    if line:
                        log("{}: {}".format(ctx, line))

            # A record row carries no file URL of its own, so it is expanded
            # here into the jobs the interrupted run would have built for
            # it — using the Plan the report recorded, not whatever boxes
            # happen to be ticked now, so the recovery reproduces the run
            # that was interrupted rather than a new one.
            # Plan each record as we reach it, NOT all of them up front.
            #
            # The first version built the whole work list first: for 75
            # un-reached records that is 150 throttled requests — about
            # twelve minutes — during which the progress bar never moved
            # off "Attaching to Chrome…", not one file was written, and a
            # Stop threw all of it away. download_worker plans inside its
            # loop for exactly this reason; hoisting it out of the retry
            # loop reintroduced the problem the module had already solved.
            #
            # `expand` is a generator, so planning a record and fetching
            # its files interleave: progress advances, Stop costs one
            # record, and the report is written from whatever is done.
            def expand(rows):
                for _src_i, row in enumerate(rows):
                    # Moved only once this source row's whole job list has
                    # been consumed, so the marker means "everything before
                    # here was attempted" and nothing else.
                    pending["pos"][0] = _src_i
                    if not row.get("replan") or row.get("drift") == "gone":
                        # `drift == "gone"` short-circuits the re-plan as
                        # well as the fetch. Re-planning costs two admin
                        # pages per record to build a file list for a
                        # record the listing says is not there.
                        if saved.already(ctx, row.get("article"), row):
                            saved.skip()     # see SavedFiles
                            continue
                        yield row
                        continue
                    gallery = ("editor_gallery.cgi"
                               in (row["adm"] or "").lower())
                    state = STATE_BY_LABEL.get(row["record_state"], "")
                    if not state:
                        warns.append(
                            "article {}: unrecognized record state {!r} — "
                            "cannot re-plan it".format(
                                row["article"], row["record_state"]))
                        log("WARNING: '{}' — {}".format(ctx, warns[-1]))
                        continue
                    item = {"article": row["article"], "title": row["title"],
                            "release": row["release"],
                            "access": row["access"], "af": None}

                    def _warn(msg, _c=ctx, _w=warns):
                        _w.append(msg)
                        log("WARNING: '{}' — {}".format(_c, msg))

                    try:
                        check_pause_stop()
                        jobs = plan_item_jobs(base_url, ctx, item, state,
                                              gallery, row["plan"], cookie,
                                              _warn)
                    except (LoginRequired, StopRequested):
                        raise
                    except Exception as e:
                        # Same loss as download_worker's, in the worker
                        # that has been given every one of these late.
                        # The row is handed on with a status instead of
                        # being dropped, so the record stays in the
                        # report and stays re-runnable.
                        msg = describe_error(e)
                        warns.append(
                            "article {}: {} while re-planning — recorded "
                            "so it can be planned again".format(
                                row["article"], msg))
                        log("WARNING: '{}' — {}".format(ctx, warns[-1]))
                        skipped = dict(row)
                        skipped["skip_status"] = (
                            "Failed: {} while re-planning".format(msg))
                        yield skipped
                        continue
                    # One record row becomes several file rows, so the
                    # denominator grows as we go. Better a total that moves
                    # than a bar reading 100% with most of the work to come.
                    nonlocal_total[0] += len(jobs) - 1
                    # The record now IN FLIGHT, so an early exit can write
                    # the files of it that were never reached (v1.34.2).
                    # Until now this worker had no such record: a re-run
                    # stopped part-way through a re-planned record wrote
                    # the files it had fetched, and _flush_retry then
                    # dropped the record row because file rows existed
                    # for it — so the remaining files were in no row
                    # anywhere. download_worker has had unreached_job_rows
                    # for exactly this since v1.33. Found on reading, for
                    # v1.34.2, and confirmed by driving the worker before
                    # a line changed. Eighteenth one-of-a-pair.
                    inflight = {"jobs": [], "pos": [0]}
                    pending["inflight"] = inflight
                    for job in jobs:
                        inflight["jobs"].append({
                            "context": ctx, "article": row["article"],
                            "kind": job["kind"],
                            "visibility": job["visibility"],
                            "version": job["version"],
                            "version_date": job["version_date"],
                            "orig_hint": job["orig_hint"], "url": job["url"],
                            "native_url": job.get("native_url", ""),
                            "adm": row["adm"], "pub": row["pub"],
                            "title": row["title"],
                            "record_state": row["record_state"],
                            "access": job["access"],
                            "release": job["release"],
                            "embargo": job["embargo"],
                            "was": NOT_ATTEMPTED, "source": row["source"],
                        })
                    for k, job in enumerate(inflight["jobs"]):
                        inflight["pos"][0] = k
                        # Asked as each job is reached, not when the record
                        # is planned: this generator interleaves with the
                        # fetches, so the primary before this job has
                        # already been fetched — and may have fallen back
                        # to the very native this job names.
                        if saved.already(ctx, row["article"], job):
                            saved.skip()
                            nonlocal_total[0] -= 1
                            continue
                        yield job
                    pending["inflight"] = None

            for order, row in enumerate(expand(ctx_rows), 1):
                check_pause_stop()
                rec_seen.add((ctx, row.get("article")))
                if row.get("skip_status"):
                    # Planning it failed. Recorded rather than dropped,
                    # and NOT fetched: its URL is an admin page, and
                    # writing that to disk under a filename claiming to
                    # be the record's file is the plausible wrong result
                    # this module keeps having to unlearn.
                    done += 1
                    n_rows = order
                    out_rows.append(
                        (order, row["article"], row.get("title", ""),
                         row.get("record_state", ""), RECORD_KIND,
                         "", "", "", "", "", "", row["skip_status"],
                         row["adm"], row["pub"], row["adm"],
                         time.strftime("%H:%M:%S"), "",
                         row.get("access", ""), row.get("release", ""),
                         row.get("embargo", ""),
                         _plan_cell(row.get("plan")), ""))
                    continue
                if row.get("drift") == "gone":
                    # Recorded as what it is. Requesting it would spend a
                    # download from a capped allowance to be told 404 by a
                    # server that has already told us, for free, that the
                    # record is not there.
                    done += 1
                    n_rows = order
                    out_rows.append(
                        (order, row["article"], row.get("title", ""),
                         row.get("record_state", ""), row["kind"],
                         row["visibility"], row["version"],
                         row["version_date"], row["orig_hint"], "", "",
                         DRIFT_GONE, row["url"], row["pub"], row["adm"],
                         time.strftime("%H:%M:%S"), "",
                         row.get("access", ""), row.get("release", ""),
                         row.get("embargo", ""), "", ""))
                    continue
                # Checked BEFORE the next fetch rather than after the last
                # one. Asking "is the budget spent?" at the top of the
                # loop answers "and is there more to do?" for free: if
                # the last file filled the session and nothing follows,
                # the loops simply end and the run reports itself as
                # finished. Raising on the way out of the final row
                # instead would report a session that did exactly what
                # the plan said as one that was cut short.
                if budget.exhausted():
                    raise SessionFull(budget.reason())
                done += 1
                n_rows = order
                _throttle("file")
                kind, art = row["kind"], row["article"]
                try:
                    got = fetch_one(
                        fetcher, verify, row["url"],
                        row.get("native_url")
                        or retry_native_url(base_url, row),
                        order, fetch_says(ctx),
                        home_url=base_url + "/")
                except LoginRequired:
                    if out_rows:
                        pname = ("DC_FileDownload_Retry_{}_{}_PARTIAL.xlsx"
                                 .format(safe_token(ctx), stamp))
                        write_structure_report(
                            os.path.join(rpt_dir, pname), out_rows, primary)
                    return fail(
                        "Digital Commons session expired part-way through. "
                        "Log in inside the debug Chrome window and re-run; "
                        "rows already fetched are kept.")
                except StopRequested:
                    # NotVerified is a stop reason, not a row outcome.
                    # Without this clause the `except Exception` below
                    # would record "Failed:" against a file that was
                    # never refused — and this worker is exactly where
                    # that class of omission keeps happening.
                    raise
                except Exception as e:
                    # Same distinction the download worker draws: a 404/410
                    # or an empty answer is the server telling us there is
                    # nothing there, and recording that as a failure is what
                    # made a clean run look broken.
                    msg = describe_error(e)
                    stop_after_row = ""
                    absent = (isinstance(e, NoFileAvailable)
                              or is_definite_absence(e))
                    forbidden = (isinstance(e, urllib.error.HTTPError)
                                 and e.code == 403)
                    if forbidden:
                        watch.refused(art, row.get("access", ""))
                    elif absent:
                        watch.absent()
                    if absent:
                        n_absent += 1
                        status = "{}: {}".format(
                            ABSENT_LABELS.get(kind, ABSENT_OTHER), msg)
                    elif forbidden and watch.is_block():
                        # One block, not one problem per row. Recorded in
                        # the report; the summary line below says it once.
                        status = "Failed: " + msg
                        warns.append("article {} {}: {}".format(art, kind, msg))
                    # Raising HERE would discard the very row that
                        # proved the block — the report would stop one row
                        # short of its own reason. The decision is taken
                        # now and acted on after the row is appended.
                        if watch.should_stop(
                                opts.get("stop_when_blocked", True)):
                            stop_after_row = watch.stop_reason()
                        said = watch.take_message()
                        if said:
                            log("WARNING: '{}' — {}".format(ctx, said))
                    elif isinstance(e, NotConfirmedHere):
                        # The same rule as download_worker: counted, and
                        # said once for the structure.
                        n_unconfirmed += 1
                        status = "{}: {}".format(UNCONFIRMED, msg)
                    else:
                        status = "Failed: " + msg
                        warns.append("article {} {}: {}".format(art, kind, msg))
                        log("WARNING: '{}' — article {} {}: {}".format(
                            ctx, art, kind, msg))
                    out_rows.append(
                        (order, art, row.get("title", ""),
                         row.get("record_state", ""), kind,
                         row["visibility"],
                         row["version"], row["version_date"],
                         row["orig_hint"], "", "", status,
                         row["url"], row["pub"], row["adm"],
                         time.strftime("%H:%M:%S"), "",
                         row.get("access", ""), row.get("release", ""),
                         row.get("embargo", ""), "", ""))
                    show_progress(ctx)
                    if stop_after_row:
                        raise ServerRefusing(stop_after_row)
                    continue

                if got.used_native and kind == "primary":
                    kind = "native"
                    # And point File URL at what was got, as
                    # download_worker always has: a row saying "native"
                    # beside the derivative's address was this worker's
                    # half of that rule, missing (v1.34.2).
                    row = dict(row, url=(row.get("native_url")
                                         or retry_native_url(base_url, row)
                                         or row["url"]))
                orig = got.name or row["orig_hint"] \
                    or "article-{}".format(art)
                nbytes = got.nbytes
                fname = build_filename(ctx, art, kind, row["visibility"],
                                       row["version"], orig, got.ctype,
                                       got.head())
                try:
                    path = place_file(got, ctx_dir, fname)
                except SaveFailed as e:
                    # One file, not the run — the same rule as
                    # download_worker, through the same function.
                    warns.append("article {} {}: {}".format(art, kind, e))
                    log("WARNING: '{}' — article {} {}: {}".format(
                        ctx, art, kind, e))
                    out_rows.append(
                        (order, art, row.get("title", ""),
                         row.get("record_state", ""), kind, row["visibility"],
                         row["version"], row["version_date"], orig, "", "",
                         "Failed: {}".format(e),
                         row["url"], row["pub"], row["adm"],
                         time.strftime("%H:%M:%S"), got.ctype,
                         row.get("access", ""), row.get("release", ""),
                         row.get("embargo", ""), "",
                         fetched_via(got, fetcher)))
                    continue
                n_files += 1
                saved.saved(ctx, art, dict(row, kind=kind),
                            os.path.basename(path))
                # Content changed hands, so the run of refusals is over.
                watch.served()
                budget.spend(nbytes)
                if row["visibility"] == "hidden":
                    n_hidden += 1
                out_rows.append(
                    (order, art, row.get("title", ""),
                     row.get("record_state", ""), kind, row["visibility"],
                     row["version"], row["version_date"], orig,
                     os.path.basename(path), nbytes, "Downloaded",
                     row["url"], row["pub"], row["adm"],
                     time.strftime("%H:%M:%S"), got.ctype,
                     row.get("access", ""), row.get("release", ""),
                     row.get("embargo", ""), "",
                     fetched_via(got, fetcher)))
                show_progress(ctx)


            rname = "DC_FileDownload_Retry_{}_{}.xlsx".format(
                safe_token(ctx), stamp)
            write_structure_report(os.path.join(rpt_dir, rname), out_rows,
                                   primary)
            tot_files += n_files
            tot_rows += n_rows
            tot_absent += n_absent
            tot_unconfirmed += n_unconfirmed
            if n_unconfirmed:
                log(unconfirmed_line(ctx, n_unconfirmed))
            if watch.total:
                log("{}: {} file request(s) refused with HTTP 403 across {} "
                    "record(s){}. Those files exist; re-run this report "
                    "once the refusal clears.".format(
                        ctx, watch.total, len(watch.articles),
                        ", {} of them on records marked 'open access'"
                        .format(watch.open_access)
                        if watch.open_access else ""))
            status = "Recovered {} of {} row(s)".format(n_files, n_rows)
            if watch.total:
                status += "; {} refused (403)".format(watch.total)
            if n_absent:
                status += "; {} not present".format(n_absent)
            if n_unconfirmed:
                status += "; {} not confirmed from this network".format(
                    n_unconfirmed)
            master.append((c_idx, ctx, "retry", n_rows, n_files,
                           n_hidden, len(warns), status, timestr, rname))
            log("{}: recovered {} of {} row(s){}".format(
                ctx, n_files, n_rows,
                "; {} the server says are not there".format(n_absent)
                if n_absent else ""))

        mpath = os.path.join(
            report_run_dir,
            "DC_FileDownload_RetryMasterLog_{}.xlsx".format(stamp))
        write_master_workbook(mpath, master, primary,
                              verify.rows(), verify.summary(),
                              check_rows=preflight_checks)
        log("Wrote " + mpath)
        set_state(last_file=mpath)
        set_progress(nonlocal_total[0], nonlocal_total[0], "Done")
        # "failed row(s)" was also wrong: since v1.20 a re-run collects
        # records that were never attempted, and a record nobody tried is
        # not a failure.
        summary = "recovered {} of {} row(s) → {}".format(
            tot_files, tot_rows, run_dir)
        if remaining:
            summary += (" · {} row(s) still outstanding in this plan — run "
                        "the next session".format(remaining))
        if tot_absent:
            summary += (" · {} the server says are not there"
                        .format(tot_absent))
        if tot_unconfirmed:
            summary += (" · {} could not be confirmed from this network"
                        .format(tot_unconfirmed))
        still = tot_rows - tot_files - tot_absent - tot_unconfirmed
        if still > 0:
            summary += " · {} still failing, see the retry report".format(
                still)
        rl = rate_state()
        if rl["hits"]:
            summary += (" · met server pushback {} time(s) ({} on downloads "
                        "at {:.1f}s, {} on admin pages at {:.1f}s)".format(
                            rl["hits"], rl["hits_file"], rl["base"],
                            rl["hits_page"], rl["page_base"]))
        summary += " · " + verify.summary()
        summary += " · master log: " + os.path.basename(mpath)
        # `still` and the structure warnings overlap: every failure the
        # fixtures can produce is ALSO a warning, so planting `still` out
        # changed no tone (1.0.1). It stays for a row that fails without a
        # warning, a case no check exercises - said here rather than
        # counted as covered.
        set_state(phase="done", summary=summary, paused=False,
                  outcome=outcome_tone(
                      max(still, 0) + tot_unconfirmed
                      + sum(row[6] for row in master),
                      succeeded=tot_files + tot_absent))
    except StopRequested as _stop:
        # Until 2026-09-09 this wrote no report at all. download_worker was
        # given flush_pending in v1.20 for exactly this — a stop at row 33
        # of 38 left 33 files on disk and nothing recording where they came
        # from — and retry_worker was never given the same treatment. Making
        # the re-run long enough to want to stop is what exposed it.
        pname = _flush_retry(pending, stamp, primary)
        # ServerRefusing subclasses StopRequested so the partial report is
        # written either way, but a run the SERVER ended must not be
        # reported as one the user ended.
        blocked = isinstance(_stop, ServerRefusing)
        full = isinstance(_stop, SessionFull)
        unverified = isinstance(_stop, NotVerified)
        if blocked:
            log("The server is refusing file requests — {}. Stopping; the "
                "files exist, so re-run this report once the refusal "
                "clears.".format(str(_stop)))
        elif unverified:
            # Nothing failed. The rows not fetched were never requested.
            log("Stopped: {}. The remaining rows were not requested, so "
                "nothing is known to be wrong with them; re-run this "
                "report when you can watch the Chrome window."
                .format(str(_stop)))
        elif full:
            # Not an interruption. The plan said this much and this much
            # was done, so it must not read like something went wrong.
            log("Session complete — {}. Start the next session from this "
                "run's folder when the allowance refreshes."
                .format(str(_stop)))
        else:
            log("Stop requested — {} of {} row(s) attempted.".format(
                done, nonlocal_total[0]))
        summary = "{} ({} of {} row(s), {} recovered).".format(
            "Stopped: the server is refusing file requests" if blocked
            else "Session complete" if full
            else "Stopped: nobody confirmed a person was present"
            if unverified
            else "Stopped early",
            done, nonlocal_total[0], budget.files)
        summary += " " + verify.summary()
        summary += (" Partial report: " + pname) if pname else \
                   " No rows had been attempted, so no report was written."
        set_state(phase="done", paused=False, summary=summary,
                  outcome=outcome_tone(
                      sum(row[6] for row in master)
                      + (1 if blocked or unverified else 0),
                      succeeded=budget.files if blocked else None))
    except Exception as e:
        # A full disk on a long re-run is the obvious case, and it must not
        # cost the record of everything already fetched.
        pname = _flush_retry(pending, stamp, primary)
        if pname:
            log("Partial report written: " + pname)
        fail(unexpected_failure(e))
    finally:
        # Module contract 5, and the same `finally` download_worker has:
        # this worker also leaves through `return fail(...)` part-way
        # through, and Chrome's download folder must be given back on
        # every one of those paths.
        if fetcher is not None:
            for note in fetcher.close():
                log(note)
        log(verify.summary())
        if saved.summary():
            log(saved.summary())
        save_run_log(report_run_dir, stamp)


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
 main{max-width:46rem;margin:0 auto;padding:1.25rem}
 fieldset{border:1px solid #d7d4cc;border-radius:6px;margin:0 0 1rem;padding:.75rem 1rem;background:#fff}
 legend{font-weight:600;padding:0 .35rem}
 details{margin:.5rem 0 .25rem;border-left:3px solid var(--accent);padding:0 0 0 .7rem}
 details>summary{cursor:pointer;font-size:.9rem;font-weight:600;color:var(--primary);
                 padding:.2rem 0}
 details>summary:focus-visible{outline:3px solid var(--primary);outline-offset:2px}
 details .hint{margin:.35rem 0}
 input[type=text]{width:100%;padding:.45rem .6rem;border:1px solid #a9a396;border-radius:5px;font:inherit}
 select{width:100%;padding:.45rem .6rem;border:1px solid #a9a396;border-radius:5px;font:inherit;background:#fff}
 button{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
        padding:.6rem 1.3rem;font:inherit;font-weight:600;cursor:pointer}
 button.secondary{background:#fff;color:var(--primary);border:2px solid var(--primary)}
 button.small{padding:.45rem .8rem;font-weight:600}
 button:disabled{opacity:.55;cursor:not-allowed}
 .row{display:flex;gap:.6rem;align-items:end;flex-wrap:wrap;margin:.4rem 0}
 .row>div{flex:1;min-width:12rem}
 .controls{display:flex;gap:.6rem;flex-wrap:wrap}
 label{display:block;margin-bottom:.3rem}
 .inline{display:flex;gap:.45rem;align-items:center;margin:.35rem 0}
 .inline label{margin:0}
 .optgrid{display:flex;gap:2rem;flex-wrap:wrap}
 .optgrid>div{min-width:14rem}
 .optgrid h3{font-size:.95rem;margin:.2rem 0 .1rem}
 .hint{font-size:.85rem;color:#54524c;margin:.35rem 0 0}
.opt{font-weight:400;color:#54524c}
 .sum{margin:.6rem 0 0;padding:.5rem .7rem;border-radius:6px;background:#eef1f5;
      border:1px solid #d7d4cc;font-size:.9rem}
 .sum.loaded{background:#e8f0e6;border-color:#4a6741}
 #list{border:1px solid #d7d4cc;border-radius:6px;max-height:16rem;overflow:auto;
       margin-top:.5rem;background:#fff}
 #list .item{display:flex;gap:.5rem;align-items:baseline;padding:.3rem .6rem;border-bottom:1px solid #efece6}
 #list .item:last-child{border-bottom:0}
 #list .meta{font-size:.82rem;color:#54524c}
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

<fieldset id="sec0"><legend>What do you want to do?</legend>
 <div class="inline">
  <input type="radio" id="jobnew" name="jobmode" value="new" checked>
  <label for="jobnew">Start a new job &mdash; choose what to download from a
   hierarchy report</label>
 </div>
 <div class="inline">
  <input type="radio" id="jobresume" name="jobmode" value="resume">
  <label for="jobresume">Continue a job already planned &mdash; from a
   previous run's report</label>
 </div>
 <p class="hint">A large collection is worked through over several sittings,
 so most runs after the first are the second kind. Continuing needs no
 hierarchy and no scope: the previous report already names every file's URL,
 every record the last session never reached, and the job it was planned
 as &mdash; which is what stops session 5 from quietly being a different
 job from session 1. The steps that do not apply are hidden rather than
 left on the page to be guessed at.</p>
</fieldset>

<fieldset id="sec1"><legend id="leg1">1 · Hierarchy source</legend>
 <div class="row">
  <div>
   <label for="repdir">Reports folder</label>
   <input type="text" id="repdir" value="" autocomplete="off" spellcheck="false">
  </div>
  <button type="button" id="scan" class="small secondary">Scan</button>
 </div>
 <div class="row">
  <div>
   <label for="reports">Existing hierarchy report</label>
   <select id="reports" aria-describedby="scanhint" disabled>
    <option value="">— scan the folder to list reports —</option>
   </select>
  </div>
  <button type="button" id="load" class="small" disabled>Load report</button>
 </div>
 <p class="hint" id="scanhint">Reports are DC_Hierarchy_Report_*.xlsx files
 written by the Hierarchy Mapping Tool (or by this module), listed newest first.</p>
 <div class="row">
  <button type="button" id="mapnow" class="secondary">Map hierarchy now
   (writes a new report)</button>
 </div>
 <div id="hiersum" class="sum" role="status" aria-live="polite">No hierarchy loaded yet.</div>
</fieldset>

<fieldset id="sec2"><legend id="leg2">2 · Scope</legend>
 <div class="inline">
  <input type="radio" id="modeall" name="mode" value="all" checked>
  <label for="modeall">All structures in the hierarchy</label>
 </div>
 <div class="inline">
  <input type="radio" id="modesel" name="mode" value="selected">
  <label for="modesel">Only these parents — plus everything beneath them</label>
 </div>
 <div id="selwrap" hidden>
  <label for="filter" style="margin-top:.4rem">Filter structures</label>
  <input type="text" id="filter" placeholder="type to filter…" autocomplete="off">
  <div id="list" role="group" aria-label="Parent structures"></div>
  <p class="hint" id="selcount">0 parents checked.</p>
 </div>
 <p class="hint">Community structures hold no uploads — they are excluded
 automatically, even when checked as scope parents.</p>
</fieldset>

<fieldset id="sec3"><legend id="leg3">3 · What to download</legend>
 <div class="optgrid">
  <div>
   <h3 id="kindh">File kinds</h3>
   <div class="inline" role="group" aria-labelledby="kindh">
    <input type="checkbox" id="kprimary" checked>
    <label for="kprimary">Primary files</label>
   </div>
   <div class="inline">
    <input type="checkbox" id="ksupp" checked>
    <label for="ksupp">Supplemental files</label>
   </div>
   <div class="inline">
    <input type="checkbox" id="knative">
    <label for="knative">Native / original files — the file as uploaded,
     <em>alongside</em> the generated PDF. Costs one request per record,
     and records uploaded as PDF have no separate native, so those are
     reported as &ldquo;No native file&rdquo; rather than as failures.</label>
   </div>
  </div>
  <div>
   <h3 id="invh">Inventory</h3>
   <div class="inline">
    <input type="checkbox" id="kinventory">
    <label for="kinventory">Inventory only &mdash; record every file without
     downloading any</label>
   </div>
   <p class="hint">Walks the same records and plans the same files, and
   fetches <strong>nothing</strong>. It is how a job this big becomes a
   number instead of a guess: about 3&frac12; hours for the 3,383-record ETD
   collection, and the report it writes re-runs like any other.</p>
   <details><summary>What an inventory gives you</summary>
    <p class="hint">A full report &mdash; every file URL, kind, visibility,
    version and version date, plus access state, release option and embargo
    date &mdash; without asking the server for a single file.</p>
    <p class="hint">Admin pages are not what the server rations, so an
    inventory is never cut short by the download cap. A complete inventory
    is always possible even when a complete download is not, and that is
    what the whole plan-then-sessions workflow is built on.</p>
    <p class="hint">Point <strong>Re-run from report</strong> at the result
    to download from it, a session at a time, in whatever order you choose
    &mdash; which is what makes a capped allowance spendable deliberately
    rather than alphabetically.</p>
   </details>
  </div>
  <div>
   <h3 id="acch">Access</h3>
   <p class="hint">A record can be posted and visible and still be
    licensed only for use inside the institution — its release option
    says so. Every report now carries an <strong>Access</strong> column
    read from the listing, at no extra cost.</p>
   <div class="inline">
    <input type="checkbox" id="kembargo">
    <label for="kembargo">Also look up the embargo date for
     access-restricted records — one extra request per <em>restricted</em>
     record, because the date is on the record page and not in the
     listing</label>
   </div>
  </div>
  <div>
   <h3 id="vish">Visibility</h3>
   <div class="inline" role="group" aria-labelledby="vish">
    <input type="checkbox" id="vpublic" checked>
    <label for="vpublic">Public files</label>
   </div>
   <div class="inline">
    <input type="checkbox" id="vhidden" checked>
    <label for="vhidden">Hidden files (unshown supplemental &amp; previous
     versions; every file on unposted records)</label>
   </div>
  </div>
  <div>
   <h3 id="verh">Versions</h3>
   <div class="inline">
    <input type="radio" id="vercur" name="ver" value="current" checked>
    <label for="vercur">Current versions only</label>
   </div>
   <div class="inline">
    <input type="radio" id="verall" name="ver" value="all">
    <label for="verall">All versions — every revision file, incl.
     originally-submitted native files and journal cover letters</label>
   </div>
   <p class="hint" id="verneed"><strong>Cover letters and earlier
   revisions come only with All versions.</strong> They live on each
   record's View-revisions page, which Current versions does not read. In
   All versions the current PDF is kept twice under different names: the
   stamped copy the public sees (<code>current</code>) and the revisions
   table's unstamped copy (<code>current-unstamped</code>).</p>
  </div>
  <div>
   <h3 id="rech">Records</h3>
   <div class="inline" role="group" aria-labelledby="rech">
    <input type="checkbox" id="rpub" checked>
    <label for="rpub">Posted records (incl. queued for update)</label>
   </div>
   <div class="inline">
    <input type="checkbox" id="runpub">
    <label for="runpub">Unposted records (not yet posted / rejected /
     withdrawn)</label>
   </div>
  </div>
 </div>
 <p class="hint">Digital Commons generates a PDF only from Word, PowerPoint
 and PDF uploads. A record holding anything else — an image, a spreadsheet,
 a dataset, audio, video — has no generated PDF, and its original file is
 downloaded automatically. Check <strong>Native / original files</strong>
 only when you want the original <em>as well as</em> the generated PDF.</p>
 <p class="hint">"All versions" reads each record's View-revisions page —
 roughly one extra request per record. Supplemental files add one request
 per record that has them.</p>
</fieldset>

<fieldset id="secfetch"><legend id="legfetch">4 · How files are fetched</legend>
 <div class="inline">
  <input type="radio" id="fetchbrowser" name="fetchpath" value="browser" checked>
  <label for="fetchbrowser">Through the Chrome window <span class="opt">(recommended)</span></label>
 </div>
 <div class="inline">
  <input type="radio" id="fetchnetwork" name="fetchpath" value="network">
  <label for="fetchnetwork">Over the network layer</label>
 </div>
 <p class="hint">Fetching through Chrome means <strong>you may be asked to
 verify that you are present</strong>. The run pauses when that happens,
 resumes by itself once you clear it in the Chrome window, and records every
 prompt in the report. The network layer cannot answer such a request at
 all, so a run on that path stops if one arrives.</p>
 <p class="hint" id="dlstatement">__STATEMENT__</p>

 <label for="verifywin">Wait this long for you to verify, in minutes</label>
 <input type="number" id="verifywin" value="__VERIFYMIN__" min="0"
        max="__VERIFYMAXMIN__" step="1" inputmode="numeric">
 <p class="hint">While it waits, the run is idle and this page's browser tab
 title changes, so a prompt is noticeable on a second screen. 0 means do not
 wait: the run stops at the first prompt and writes out what it has.</p>

 <label for="stalllimit">Give up on a download that stops growing for this
  long, in seconds</label>
 <input type="number" id="stalllimit" value="__STALLSEC__" min="1"
        max="__STALLMAXSEC__" step="1" inputmode="numeric">
 <p class="hint">A download is waited for as long as it keeps growing,
 however large the file. __STALLSEC__ is a starting guess, not a
 measurement; every stall is recorded in the log so it can be revised.</p>

 <details><summary>What the two paths do differently</summary>
  <p class="hint">Digital Commons periodically asks a client to confirm that
  a person is at it. A browser can put that question in front of you and you
  answer it in a couple of seconds; a request made outside a browser cannot
  answer it at all and simply sees the request refused, indefinitely.
  Fetching through the window you are already logged into is therefore the
  path that can complete a long job, and the pauses are a feature of it
  rather than a fault.</p>
  <p class="hint"><strong>Admin pages are fetched over the network layer on
  both settings.</strong> They are never challenged, and that is where the
  timeout and retry control matters.</p>
  <p class="hint">The network layer remains selectable because the two paths
  have not been compared at collection scale, and a question worth answering
  deserves a control rather than an edit to the source. It reports the same
  outcomes; it just cannot get past a verification request.</p>
  <p class="hint">Files arriving through Chrome land in a
  <code>_incoming</code> folder inside this run's folder and are moved to
  their final names from there, so your own Downloads folder is not touched.
  The module sets Chrome's download folder back when the run ends, however
  it ends.</p>
 </details>
</fieldset>

<fieldset id="seclimits"><legend id="leglimits">5 · Session limits</legend>
 <p class="hint">Digital Commons caps how much it will hand over to an
 account in a day, so a large job is several sittings. These two numbers are
 what ends a sitting. <strong>Whichever is reached first stops the
 session</strong> &mdash; and the rest of the job is written out as
 outstanding, so the next session picks it up from this run's report.</p>

 <label for="sessioncap">Files this session</label>
 <input type="number" id="sessioncap" value="__LIMFILES__" min="0"
        max="100000" step="10" inputmode="numeric">

 <label for="sessionmb">Megabytes this session</label>
 <input type="number" id="sessionmb" value="__LIMMB__" min="0" max="1000000"
        step="25" inputmode="numeric">

 <p class="hint">0 in either box means do not limit on that one; 0 in both
 takes everything outstanding, which is what a structure smaller than a
 day's allowance wants.</p>
 <details>
  <summary>Why both are 0 by default, and why there are two</summary>
  <p class="hint">Both default to <strong>__LIMFILES__ files</strong> and
  <strong>__LIMMB__ MB</strong> &mdash; that is, no limit. They were added
  when the refusals a long run met were believed to be a download allowance,
  and there were two because it was not known which unit the server counted.
  The refusals turned out to be the repository asking whether a person is
  present (see <em>What the refusals mean</em>, below), which the Chrome path
  answers through you. So there is no allowance to size a session for.</p>
  <p class="hint">The measurements they were built from, from one address:
  379 files and 322 MB across four runs, never refused; 158 files and
  766 MB, refused; 167 files and 442 MB, refused; and from another network,
  99 files and 200 MB, refused. A file count did not predict the refusals
  and megabytes only partly did &mdash; which is what an unanswered question
  looks like, rather than an allowance.</p>
  <p class="hint">Set either one to size a session deliberately. The plan
  workbook records what was set, so the run is its own evidence.</p>
 </details>
</fieldset>

<fieldset id="sec4"><legend id="leg4">6 · Destination</legend>
 <label for="dldir">Save downloads to</label>
 <input type="text" id="dldir" value="" autocomplete="off" spellcheck="false">
 <p class="hint">Each run creates DC_FileDownloads_&lt;timestamp&gt;/ here,
 with one subfolder per structure. Files are renamed
 &lt;context&gt;_&lt;article&gt;_&lt;kind&gt;_&lt;visibility&gt;_&lt;version&gt;_&lt;original
 name&gt;.</p>

 <label for="reqdelay">Gap before a file download, in seconds</label>
 <input type="number" id="reqdelay" value="5.0" min="0" max="60" step="0.1"
        inputmode="decimal">
 <p class="hint">Leave this at 5 unless you are fetching a handful of files
 and would rather not wait. On anything substantial a run that finishes beats
 a run that stalls.</p>
 <details><summary>Why 5 seconds</summary>
  <p class="hint">It is the figure that was measured, not a guess. Digital
  Commons rate-limits downloads to roughly one every nine seconds and answers
  a faster run with a ten-minute lockout. At a 0.3s gap a 38-record structure
  met one after 26 to 36 records every time it was tried; at 5s it fetched
  everything with no pushback at all.</p>
 </details>

 <div class="inline">
  <input type="checkbox" id="stopblocked" checked>
  <label for="stopblocked">Stop when the server starts refusing everything
   &mdash; after __BLOCKSTOP__ file requests refused in a row</label>
 </div>
 <p class="hint">Leave this on. Once the server starts refusing, the rest of
 a run is spent being told no &mdash; in one measured run that was 24
 minutes and 285 requests. Stopping writes the same complete report a Stop does, with
 every un-reached record and its plan, so re-running it later picks up
 exactly where this left off.</p>
 <details><summary>What the refusals mean</summary>
  <p class="hint"><strong>This describes the network-layer path.</strong>
  What looked like a download quota is not one. Digital Commons periodically asks a client to confirm a person is
  present, and a request made outside a browser cannot answer that question,
  so it sees the refusal repeated indefinitely and nothing about waiting or
  logging in again changes it. Six days were spent measuring a ceiling that
  does not exist; there is no daily allowance, nothing resets overnight, and
  the figures that appeared to show one were a request that had gone
  unanswered.</p>
  <p class="hint">On the Chrome path the same moment appears as a
  verification prompt, which you clear, and the run continues.</p>
  <p class="hint"><strong>A refusal here is not a permissions problem.</strong>
  Of 80 refusals measured in one run, every one was on a record marked open
  access, while four access-restricted files downloaded successfully in the
  same run. It says nothing about what the records permit &mdash; and it
  never means the file is absent.</p>
 </details>

 <label for="pagedelay">Gap before an admin page, in seconds</label>
 <input type="number" id="pagedelay" value="1.0" min="0" max="60" step="0.1"
        inputmode="decimal">
 <p class="hint">Leave this at 1. Raise it toward the download gap only if
 a run reports pushback on admin pages &mdash; the log says which of the two
 paces provoked a lockout, so you will not have to guess.</p>
 <details><summary>Why admin pages are paced faster than downloads</summary>
  <p class="hint">Digital Commons appears to count <strong>downloads</strong>,
  not admin pages. The Hierarchy Mapping Tool sleeps 1.0s between admin page
  loads and made about 1,152 of them in 28 minutes with no lockout &mdash;
  and a download run two minutes later fetched 37 of 38 files unpenalised.
  Had admin traffic shared the download budget, that map would have exhausted
  it many times over.</p>
  <p class="hint">It matters because a run fetches two admin pages per record
  (the revisions table and the Supplemental content page), so on the
  3,383-record ETD collection this is the difference between roughly 28 and
  20 seconds a record &mdash; about eight hours off a full pass.</p>
  <p class="hint">That is <em>one</em> natural experiment. It shows admin
  pages did not consume the download budget on that occasion &mdash; not that
  they are free when interleaved with downloads, and there may be a page
  limit nobody has reached because 1.0s was under it.</p>
 </details>

 <label for="cooldowns">Wait out server cooldowns, at most this many
 times per run</label>
 <input type="number" id="cooldowns" value="3" min="0" max="20" step="1"
        inputmode="numeric">
 <p class="hint">Digital Commons answers a sustained run with a lockout — a
 fixed interval, typically ten minutes, with one shared expiry for every
 pending file. Waiting it out once therefore recovers the whole backlog, so
 a run that would have finished incomplete in two minutes finishes complete
 in twelve. The wait is announced, counts down, and Stop still works. Set 0
 to never wait: blocked files are then reported as blocked, naming the
 interval, and you re-run later.</p>

 <label for="rptdir">Save reports to <span class="opt">(optional)</span></label>
 <input type="text" id="rptdir" value="" autocomplete="off"
        spellcheck="false" placeholder="leave blank to keep reports with the files">
 <p class="hint">A run pulls hidden, unpublished and access-restricted
 content to disk, while the reports carry only URLs, statuses and counts.
 Point this somewhere shareable and the downloads somewhere controlled, and
 the audit trail can be handed on without the content. Blank keeps both
 together, as before: reports in each structure subfolder plus a master log
 in the run folder.</p>
</fieldset>

<div class="controls">
<fieldset id="secresume"><legend id="legresume">Previous report</legend>
 <p class="hint"><strong>This replaces steps 1 to 3, not step 4.</strong> It
 needs no hierarchy and no scope: the report already names every URL, and
 every record a stopped run never reached. Fill in the destination above,
 point this at the report, and press <strong>Re-run from report</strong>
 rather than Start.</p>
 <div class="inline">
  <input type="checkbox" id="retryunconfirmed">
  <label for="retryunconfirmed">Include rows that could not be confirmed
   from the network they were run on</label>
 </div>
 <p class="hint">Off by default. Those rows were asked to verify that a
 person is present on a path that cannot answer, so re-running them from
 the same network meets the same question. Tick this when running from a
 network the repository does not challenge.</p>
 <div class="inline">
  <input type="checkbox" id="reconcile" checked>
  <label for="reconcile">Check the collection for changes since the plan
   was made</label>
 </div>
 <p class="hint">A plan is a snapshot, and a session may run days after it
 was taken. This re-reads each structure's listing once before fetching
 anything, so a record restricted since the plan is not downloaded and then
 labelled with the plan's old Access value &mdash; and records that have
 been removed are recorded as removed rather than requested.</p>
 <details><summary>What it costs, and what it will not do</summary>
  <p class="hint">One admin page per record state, which is not what the
  server rations &mdash; about a second, and nothing from the download
  allowance.</p>
  <p class="hint">It does not pick up records <em>added</em> since the plan.
  It counts them and says so, and that is deliberate: a plan is a snapshot,
  which is what makes it stable and re-runnable across many sittings.</p>
  <p class="hint">If a listing will not load, the session says which one and
  carries on &mdash; those rows keep the plan's labels. Re-labelling is an
  improvement, not a precondition for downloading.</p>
 </details>

 <label for="retryrpt">Report, or the run folder holding several</label>
 <input type="text" id="retryrpt" value="" autocomplete="off"
        spellcheck="false"
        placeholder="a DC_FileDownload_Report_*.xlsx, or a run folder">
 <p class="hint">Re-attempts every row whose Status did not begin
 &ldquo;Downloaded&rdquo;, and writes a fresh retry report. Rows recorded as
 <strong>Not attempted</strong> are records a stopped run never got to;
 those are planned again from the record, using the plan the interrupted run
 wrote down. Rows the server answered with a definite absence are
 <em>not</em> re-requested &mdash; they do not become present on a second
 ask. This is how a large collection is worked through across several
 sittings, since Digital Commons locks out a sustained run and re-fetching
 what is already on disk only provokes the next lockout.</p>
</fieldset>

 <button type="button" id="go" disabled>Start downloads</button>
 <button type="button" id="retrygo" class="secondary" disabled>Re-run from
 report</button>
 <button type="button" id="pause" class="secondary" disabled>Pause</button>
 <button type="button" id="stop" class="secondary" disabled>Stop</button>
</div>

<div id="status" role="status" aria-live="polite">Idle.</div>
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
const repdir=$('repdir'),scanBtn=$('scan'),reports=$('reports'),loadBtn=$('load'),
      mapBtn=$('mapnow'),hiersum=$('hiersum'),modeall=$('modeall'),modesel=$('modesel'),
      selwrap=$('selwrap'),filter=$('filter'),list=$('list'),selcount=$('selcount'),
      kprimary=$('kprimary'),ksupp=$('ksupp'),knative=$('knative'),
    kembargo=$('kembargo'),pagedelay=$('pagedelay'),
    kinventory=$('kinventory'),stopblocked=$('stopblocked'),sessioncap=$('sessioncap'),sessionmb=$('sessionmb'),
    reconcile=$('reconcile'),
      vpublic=$('vpublic'),vhidden=$('vhidden'),
      vercur=$('vercur'),verall=$('verall'),rpub=$('rpub'),runpub=$('runpub'),
      dldir=$('dldir'), rptdir=$('rptdir'), cooldowns=$('cooldowns'),
      retryrpt=$('retryrpt'), retrygo=$('retrygo'), reqdelay=$('reqdelay'),
      go=$('go'),pauseBtn=$('pause'),stopBtn=$('stop'),
      status=$('status'),prog=$('prog'),logBox=$('log');
let paused=false, hierVersion=0, hierLoaded=false,
    nodes=[], checked=new Set();

async function api(path,body){
  const r=await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  let j={}; try{ j=await r.json(); }catch(e){}
  return {ok:r.ok, j};
}

scanBtn.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  const {ok,j}=await api('/api/reports/scan',{folder:repdir.value.trim()});
  if(!ok){flashErr(j.error||'Scan failed.');return;}
  reports.innerHTML='';
  if(!j.reports.length){
    reports.append(new Option('— no hierarchy reports found —',''));
    reports.disabled=true; loadBtn.disabled=true;
    flashErr('No DC_Hierarchy_Report_*.xlsx files in that folder.');
    return;
  }
  j.reports.forEach(r=>reports.append(new Option(r.name+'  ('+r.modified+')',r.path)));
  reports.disabled=false; loadBtn.disabled=false;
  flash(j.reports.length+' report(s) found — pick one and click Load report.');
});

loadBtn.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  if(!reports.value){flashErr('Pick a report first.');return;}
  const {ok,j}=await api('/api/reports/load',{path:reports.value});
  if(!ok){flashErr(j.error||'Could not load that report.');return;}
  poll();
});

/* ---- Job mode -----------------------------------------------------------
   Until v1.30 the re-run controls sat at the FOOT of the page, after
   four numbered sections none of which a re-run uses. Continuing a job
   meant walking past a required hierarchy source, a scope and a set of
   download options, with nothing saying whether they applied — a question
   the operator asked twice, which makes it the page's fault and not a gap in the
   documentation. So the mode is the first thing asked, and the steps that
   do not apply are hidden rather than left to be guessed at.

   The numbers on the legends are renumbered to match what is actually
   shown, because "1, 4, 5" reads as three missing steps. ------------- */
const jobNew=document.getElementById('jobnew'),
      jobResume=document.getElementById('jobresume');
const NEWSTEPS=['leg1','leg2','leg3','legfetch','leglimits','leg4'];
const RESUMESTEPS=['legresume','legfetch','leglimits','leg4'];
const STEPTEXT={leg1:'Hierarchy source', leg2:'Scope',
                leg3:'What to download', legfetch:'How files are fetched',
                leglimits:'Session limits',
                leg4:'Destination', legresume:'Previous report'};

function applyJobMode(){
  const resume=jobResume.checked;
  ['sec1','sec2','sec3'].forEach(id=>{
    document.getElementById(id).hidden=resume;});
  document.getElementById('secresume').hidden=!resume;
  const steps=resume?RESUMESTEPS:NEWSTEPS;
  Object.keys(STEPTEXT).forEach(id=>{
    const el=document.getElementById(id);
    if(!el) return;
    const n=steps.indexOf(id);
    el.textContent=(n<0?'':(n+1)+' \u00b7 ')+STEPTEXT[id];});
  /* The two start buttons are not interchangeable, and offering the one
     this mode cannot use is how a re-run got started with Start. */
  go.hidden=resume; retrygo.hidden=!resume;
  /* The poller owns enabled/disabled; this only owns which button is
     OFFERED. Leave the rest to the next poll rather than duplicating
     the rules here, which is how two copies of a rule drift. */
}
jobNew.addEventListener('change',applyJobMode);
jobResume.addEventListener('change',applyJobMode);
[jobNew,jobResume].forEach(el=>el.addEventListener('change',()=>{
  if(status.className===''&&status.textContent.indexOf('Idle')===0) idleStatus();}));

retryrpt.addEventListener('input',()=>{
  retrygo.disabled=!retryrpt.value.trim();});
applyJobMode();

/* Every start button holds the same single run slot, so any one of them
   locks all three the instant it is pressed — before the await, not after
   the server answers. `go` did this and `retrygo` did not, which is how a
   re-run got started twice on 2026-09-09. Pressing Start and then Re-run
   would have done the same thing, so guarding each button against only
   itself was never enough. The next poll() restores them from run state. */
function lockStarts(){ go.disabled=true; retrygo.disabled=true;
  if(typeof mapBtn!=='undefined'&&mapBtn) mapBtn.disabled=true; }

/* Read once, by both start buttons. Two copies of "which radio is
   checked" is two things to keep in step, and the re-run is exactly the
   path that keeps being given a feature late. */
function fetchPath(){
  return document.getElementById('fetchnetwork').checked?'network':'browser';
}
/* Minutes on the page, seconds on the wire. The page asks in minutes
   because that is how a person thinks about waiting; the module counts in
   seconds because everything else in it does. */
function stallLimit(){
  const v=parseFloat(document.getElementById('stalllimit').value);
  return isFinite(v)?v:__STALLSEC__;
}
function verifyWindow(){
  const mins=parseFloat(document.getElementById('verifywin').value);
  return (isFinite(mins)&&mins>=0?mins:0)*60;
}

retrygo.addEventListener('click', async ()=>{
  clearErr();
  lockStarts();
  const {ok,j}=await api('/api/retry',{out_dir:dldir.value.trim(),
    report_dir:rptdir.value.trim(), report_path:retryrpt.value.trim(),
    cooldowns:cooldowns.value.trim(), delay:reqdelay.value.trim(),
    page_delay:pagedelay.value.trim(),
    stop_when_blocked:stopblocked.checked,
    max_files:sessioncap.value.trim(),
    max_mb:sessionmb.value.trim(),
    fetch_path:fetchPath(), verify_window:verifyWindow(), stall_limit:stallLimit(),
    reconcile:reconcile.checked,
    retry_unconfirmed:document.getElementById('retryunconfirmed').checked});
  if(!ok){flashErr(j.error||'Could not start the re-run.');poll();return;}
  poll();
});

mapBtn.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  lockStarts();
  const {ok,j}=await api('/api/map',{out_dir:dldir.value.trim(),
    report_dir:rptdir.value.trim()});
  if(!ok){flashErr(j.error||'Could not start mapping.');poll();return;}
  poll();
});

modeall.addEventListener('change',syncScope);
modesel.addEventListener('change',syncScope);
function syncScope(){ selwrap.hidden=!modesel.checked; }

filter.addEventListener('input',renderList);

function renderList(){
  const q=filter.value.trim().toLowerCase();
  list.innerHTML='';
  nodes.filter(n=>!q||n.ctx.toLowerCase().includes(q)).slice(0,400).forEach(n=>{
    const item=document.createElement('div'); item.className='item';
    const cb=document.createElement('input');
    cb.type='checkbox'; cb.id='cb_'+n.ctx; cb.checked=checked.has(n.ctx);
    cb.addEventListener('change',()=>{
      cb.checked?checked.add(n.ctx):checked.delete(n.ctx); updateCount();});
    const lab=document.createElement('label');
    lab.htmlFor=cb.id; lab.style.margin='0'; lab.textContent=n.ctx;
    const meta=document.createElement('span'); meta.className='meta';
    meta.textContent=(n.type||'—')+' · Level '+n.level+
      (n.desc?' · '+n.desc+' beneath':'');
    item.append(cb,lab,meta); list.append(item);
  });
  if(!list.children.length){
    const p=document.createElement('div'); p.className='item';
    p.textContent=nodes.length?'No structures match that filter.':'Load or map a hierarchy first.';
    list.append(p);
  }
  updateCount();
}
function updateCount(){
  selcount.textContent=checked.size+' parent(s) checked.';
  if(typeof saveForm==='function') saveForm();
}

go.addEventListener('click', async ()=>{
  clearErr();   // a fixed problem takes its message down
  const mode=modesel.checked?'selected':'all';
  if(mode==='selected'&&!checked.size){flashErr('Check at least one parent structure.');return;}
  if(!kprimary.checked&&!ksupp.checked&&!knative.checked){flashErr('Check at least one file kind.');return;}
  if(!vpublic.checked&&!vhidden.checked){flashErr('Check at least one visibility.');return;}
  if(!rpub.checked&&!runpub.checked){flashErr('Check at least one record class.');return;}
  const body={out_dir:dldir.value.trim(),
    report_dir:rptdir.value.trim(), mode:mode, parents:[...checked],
    primary:kprimary.checked, supp:ksupp.checked, native:knative.checked,
    embargo:kembargo.checked, inventory:kinventory.checked,
    stop_when_blocked:stopblocked.checked,
    public:vpublic.checked, hidden:vhidden.checked,
    versions:verall.checked?'all':'current',
    published:rpub.checked, unpublished:runpub.checked,
    cooldowns:cooldowns.value.trim(), delay:reqdelay.value.trim(),
    page_delay:pagedelay.value.trim(),
    fetch_path:fetchPath(), verify_window:verifyWindow(), stall_limit:stallLimit()};
  lockStarts();
  const {ok,j}=await api('/api/start',body);
  if(!ok){flashErr(j.error||'Could not start.');poll();return;}
  poll();
});

pauseBtn.addEventListener('click', async ()=>{
  const r=await api('/api/pause',{paused:!paused});
  if(r.ok) poll();
});

stopBtn.addEventListener('click', async ()=>{
  if(!confirm('Stop the job? Files already saved are kept and partial reports will still be written.'))return;
  const r=await api('/api/stop');
  if(r.ok) poll();
});

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
/* The page's real title, captured once before anything overwrites it, so
   restoring cannot restore a previous alarm. */
const PAGETITLE=document.title;
const VERIFYTITLE='⚠ Verify now — '+PAGETITLE;
function applyVerifyTitle(holding){
  const want=holding?VERIFYTITLE:PAGETITLE;
  if(document.title!==want) document.title=want;
}

/* Says what to do NEXT, in the mode the page is in. v1.34.2: it was
   written once and then kept — the poller repainted it only when the line
   was empty — so it still said "load or map a hierarchy first" after one
   was loaded, and said it in re-run mode, which needs no hierarchy. */
function idleStatus(){
  const want=jobResume.checked
    ?'Idle — name the previous report, then Re-run from report.'
    :(hierLoaded
      ?'Idle — choose the scope and options, then Start downloads.'
      :'Idle — load or map a hierarchy first.');
  /* Written only when it changes: #status is a live region, and the
     poller runs every 1.5 s. */
  if(status.className!=='') status.className='';
  if(status.textContent!==want) status.textContent=want;
}

async function refreshHierarchy(){
  const j=await (await fetch('/api/hierarchy')).json();
  nodes=j.nodes||[]; checked.clear();
  /* A reopened page puts back the parents it had checked — those that
     are in this hierarchy, and only once. */
  if(pendingParents){
    const have=new Set(nodes.map(n=>n.ctx));
    pendingParents.forEach(c=>{ if(have.has(c)) checked.add(c); });
    pendingParents=null;
  }
  renderList();
}

/* ---- The form survives a reopen (v1.34.2), and a restart (1.0.1) ------
   2026-10-01: a reopened tab came back with "Save reports to" blank and
   the mode on Start - a silent change of destination for the next run.
   Since 1.0.1 the shared DC-FORM block (above) keeps the fields, in a file
   beside the configuration, for every module that runs a job. The checked
   structures and the job mode are this page's own; DC-FORM calls these. */
let pendingParents=null;
function formParents(){ return [...checked]; }
function formRestoredHook(form){
  if(!form||!form.fields) return;
  pendingParents=form.parents||[];
  applyJobMode(); syncScope();
  if(hierLoaded&&nodes.length) refreshHierarchy();
}

async function poll(){
  const s=await (await fetch('/api/state')).json();
  formSeen(s);
  renderLog(s.log); logStateSeen(s);
  paused=!!s.paused;
  pauseBtn.textContent=paused?'Resume':'Pause';
  const running=['starting','mapping','downloading','writing'].includes(s.phase);
  hierLoaded=!!(s.hier&&s.hier.loaded);
  if(hierLoaded&&s.hier.version!==hierVersion){
    hierVersion=s.hier.version; await refreshHierarchy();
  }
  if(hierLoaded){
    hiersum.className='sum loaded';
    hiersum.textContent=s.hier.count+' structures · deepest level '+
      s.hier.max_level+' · '+s.hier.source;
  }
  go.disabled=running||!hierLoaded;
  // Deliberately not gated on a hierarchy: the report carries the URLs.
  retrygo.disabled=running||!retryrpt.value.trim();
  mapBtn.disabled=running;
  loadBtn.disabled=running||!reports.value;
  scanBtn.disabled=running;
  pauseBtn.disabled=!running; stopBtn.disabled=!running;
  const p=s.progress||{};
  if(running&&p.total>0){prog.hidden=false;prog.max=p.total;prog.value=p.current;}
  else prog.hidden=true;
  /* Whole numbers as "n/N"; a fraction (a single structure's progress)
     as a percentage. v1.34: it printed "(0.22486288848263253/1)". */
  const counts=p.total>0?(Number.isInteger(p.current)
      ?' ('+p.current+'/'+p.total+')'
      :' ('+Math.round(100*p.current/p.total)+'%)'):'';
  const msg=p.msg?' — '+p.msg:'';
  /* The tab title, because a run is usually watched on a second screen
     and the tab title is what gets noticed there. Restored to the page's
     own title the moment the hold ends, so a stale alarm never outlives
     what it was announcing. */
  const v=s.verify||{};
  applyVerifyTitle(!!v.holding);
  let links='';
  if(s.last_file) links+='<br><a href="/download">Download master log</a>';
  if(s.report_file) links+='<br><a href="/download-report">Download hierarchy report</a>';
  if(s.phase==='done'){
    status.className=logTone(s); status.innerHTML='Done: '+esc(s.summary)+links;
  }else if(s.phase==='error'){
    status.className='error'; status.innerHTML='Error: '+esc(s.summary)+links;
  }else if(s.phase==='idle'){
    idleStatus();
  }else if(v.holding){
    /* Its own branch, above the ordinary running one, because this is
       not an error and not the operator's own Pause: it is the run
       waiting to be told a person is here. Worded as the instruction it
       is, and it says what happens if nobody does. */
    status.className='';
    status.textContent='Verification needed — clear it in the Chrome '+
      'window. Waiting '+v.left+'s more, then this run writes out what '+
      'it has.'+counts;
  }else{
    status.className='';
    status.textContent=(paused?'Paused':cap(s.phase))+counts+msg;
  }
}
const esc=t=>t.replace(/&/g,'&amp;').replace(/</g,'&lt;');
const cap=t=>t.charAt(0).toUpperCase()+t.slice(1)+'…';
syncScope(); renderList(); poll(); setInterval(poll,1500);
</script>
</body></html>"""


# What the page says about the terms of downloading when the institution's
# profile says nothing. Institution-neutral and vendor-neutral BY DESIGN:
# the words of a real arrangement belong in that institution's profile
# (branding.download_statement), never in this file, and the generic build
# refuses to ship if they reach it (identity_markers).
NEUTRAL_DOWNLOAD_STATEMENT = (
    "Whether scripted downloading is permitted, and on what terms, depends "
    "on your institution's agreement with its repository provider. Ask "
    "whoever manages that agreement before a large run.")


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
        # Derived from the constant, so the page can never quote a
        # threshold the code does not use.
        "__BLOCKSTOP__": str(BLOCK_STOP_403),
        "__LIMFILES__": str(SESSION_LIMIT_FILES),
        "__LIMMB__": str(SESSION_LIMIT_MB),
        # Minutes, from the constant, so the page cannot advertise a
        # window the code does not wait.
        "__VERIFYMIN__": "{:.0f}".format(VERIFY_WINDOW / 60.0),
        "__VERIFYMAXMIN__": "{:.0f}".format(VERIFY_WINDOW_MAX / 60.0),
        "__STALLSEC__": "{:.0f}".format(BROWSER_STALL_LIMIT),
        "__STALLMAXSEC__": "{:.0f}".format(BROWSER_STALL_LIMIT_MAX),
        # The fields a reopened page is filled from — the server's list,
        # so the page and validate_form_state() name the same fields.
        "__FORM_FIELDS__": json.dumps(list(FORM_FIELDS)),
        # Escaped: it is text from a settings file, shown in a page.
        "__STATEMENT__": _html_escape(
            str(brand.get("download_statement") or "").strip()
            or NEUTRAL_DOWNLOAD_STATEMENT),
    }.items():
        page = page.replace(token, value)
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "")


FIELD_LABELS = {
    "cooldowns": "Wait out server cooldowns",
    "delay": "Gap before a file download",
    "page_delay": "Gap before an admin page",
    "max_files": "Files per session",
    "max_mb": "Megabytes per session",
    "verify_window": "Wait this long for you to verify",
    "stall_limit": "Give up on a download that stops growing",
}
BLANK_MEANS_ZERO = ("max_files", "max_mb")


class BadField(ValueError):
    """A number box that could not be read — named, with what it sent."""


def read_number(req, key, default, whole=False):
    """One reader for every number both endpoints accept (contract 11).

    v1.34. A blank or unreadable box used to come back as a bare "Bad
    request.", naming nothing — and a number box the browser cannot parse
    reports itself as EMPTY, so a field that looked filled in sent
    nothing (2026-09-29). Now the field is named, and so is what arrived.
    """
    raw = req.get(key, default)
    label = FIELD_LABELS.get(key, key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if key in BLANK_MEANS_ZERO:
            return 0
        raise BadField(
            "“{}” is empty, or holds something the browser could not read "
            "as a number (it then sends nothing). Type the number again."
            .format(label))
    try:
        v = float(raw)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError
        if whole:
            if v != int(v):
                raise ValueError
            return int(v)
        return v
    except (TypeError, ValueError):
        raise BadField("“{}” must be {}; it received {!r}.".format(
            label, "a whole number" if whole else "a number", raw))


def _fetch_option_error(opts):
    """Validate the fetch options. Returns "" when they are fine.

    One function, called by BOTH endpoints. /api/start and /api/retry
    have drifted apart before — the re-run reached v1.32 without the
    block detector the download path had since v1.31 — and a validation
    rule that exists on one endpoint only is the same shape of defect
    arriving at the front door instead of in a worker.
    """
    if opts.get("fetch_path") not in FETCH_PATHS:
        return "Choose how files are fetched: through Chrome, or over " \
               "the network layer."
    stall = opts.get("stall_limit", BROWSER_STALL_LIMIT)
    if not 1 <= stall <= BROWSER_STALL_LIMIT_MAX:
        return ("The time to wait for a download that has stopped growing "
                "must be between 1 and {:.0f} seconds.".format(
                    BROWSER_STALL_LIMIT_MAX))
    window = opts.get("verify_window", VERIFY_WINDOW)
    if not 0 <= window <= VERIFY_WINDOW_MAX:
        return ("The time to wait for verification must be between 0 and "
                "{:.0f} minutes.".format(VERIFY_WINDOW_MAX / 60.0))
    return ""


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
    """Reject requests that did not originate locally (DNS rebinding /
    cross-site form/fetch guard — see the module template)."""
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
                return (STATE["phase"] in RUNNING_PHASES
                        or STATE["claimed"])

        # -------------------------------------------------------------
        # Starting a run is a claim, not a question.
        #
        # 2026-09-09: a re-run was started twice and two retry workers ran
        # concurrently — two copies of every log line, both writing into
        # the same run folder, and between them hitting a server measured
        # to allow one download every 8.8s at roughly 2.5s intervals.
        #
        # _running() alone could never have stopped it. Every worker sets
        # phase="starting" as its FIRST statement, which happens inside
        # the new thread — so between the endpoint answering and the
        # thread being scheduled, the phase still reads "idle" and a
        # second request passes the check. Classic check-then-act: the
        # question was asked honestly and the answer was stale before it
        # could be used.
        #
        # The claim is taken under the same lock that reads it, so of two
        # simultaneous requests exactly one wins. It is released in a
        # finally, so a worker that raises on its first line does not
        # wedge the module.
        # -------------------------------------------------------------
        def _claim_and_start(self, target, args):
            self._busy_msg = ""
            with LOCK:
                if STATE["phase"] in RUNNING_PHASES or STATE["claimed"]:
                    return False
                STATE["claimed"] = True
            # And across processes (v1.34.1): the same claim, in a file
            # named for this Chrome. Taken after the in-process claim so
            # two requests to THIS copy never race each other for it, and
            # outside LOCK because judging a stale lock asks another
            # process over HTTP.
            addr = session.get("chrome", {}).get("debugger_address", "")
            port = self.server.server_address[1]
            ok, holder = claim_run_lock(addr, port)
            if not ok:
                with LOCK:
                    STATE["claimed"] = False
                self._busy_msg = run_lock_refusal(holder)
                log(self._busy_msg)
                return False

            begin_run_log()

            def runner():
                try:
                    target(*args)
                finally:
                    release_run_lock(addr)
                    with LOCK:
                        STATE["claimed"] = False

            threading.Thread(target=runner, daemon=True).start()
            return True

        def _busy(self):
            return self._json({"error": getattr(self, "_busy_msg", "")
                               or "A job is already running."}, 409)

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
            if self.path == "/api/state":
                form_for_page()
                with LOCK:
                    return self._json(dict(STATE))
            if self.path == "/api/hierarchy":
                with LOCK:
                    model = MODEL
                if model is None:
                    return self._json({"nodes": []})
                nodes = [{"ctx": ctx,
                          "type": model["types"].get(ctx, ""),
                          "level": model["levels"][ctx],
                          "desc": model["desc_count"].get(ctx, 0)}
                         for ctx in sorted(model["levels"])]
                return self._json({"root": model["root"], "nodes": nodes})
            if self.path == "/api/log":
                # The whole current run, however far the page has
                # scrolled — see DC-LOG.
                return send_run_log(self)
            if self.path == "/download":
                with LOCK:
                    path = STATE["last_file"]
                return self._send_file(path)
            if self.path == "/download-report":
                with LOCK:
                    path = STATE["report_file"]
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
            if self.path == "/api/reports/scan":
                try:
                    folder = os.path.expanduser(
                        str(self._body()["folder"]).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                err_ = folder_error("Reports folder", folder)
                if err_:
                    return self._json({"error": err_}, 400)
                return self._json(
                    {"reports": scan_reports(folder, HIER_GLOB)})

            if self.path == "/api/reports/load":
                if self._running():
                    return self._json({"error": "A job is running."}, 409)
                try:
                    path = os.path.expanduser(
                        str(self._body()["path"]).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                if not path or not os.path.isfile(path):
                    return self._json(
                        {"error": "File does not exist: " + path}, 400)
                try:
                    rows = parse_report(path)
                    set_model(build_model(
                        rows, "loaded from " + os.path.basename(path)))
                    log("Loaded hierarchy report: " + path)
                    with LOCK:
                        return self._json({"ok": True,
                                           "hier": dict(STATE["hier"])})
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)

            if self.path == "/api/map":
                if self._running():
                    return self._json({"error": "A job is already running."}, 409)
                try:
                    body = self._body()
                    out_dir = os.path.expanduser(
                        str(body["out_dir"]).strip())
                    report_dir = os.path.expanduser(
                        str(body.get("report_dir", "")).strip())
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self._json({"error": "Bad request."}, 400)
                err_ = folder_error("Save downloads to", out_dir)
                if err_:
                    return self._json({"error": err_}, 400)
                err_ = folder_error("Save reports to", report_dir, optional=True)
                if err_:
                    return self._json({"error": err_}, 400)
                STOP_EVENT.clear()
                PAUSE_EVENT.clear()
                if not self._claim_and_start(
                        map_worker, (session, out_dir, report_dir)):
                    return self._busy()
                return self._json({"ok": True})

            if self.path == "/api/retry":
                if self._running():
                    return self._json(
                        {"error": "A job is already running."}, 409)
                try:
                    req = self._body()
                    out_dir = os.path.expanduser(
                        str(req["out_dir"]).strip())
                    report_dir = os.path.expanduser(
                        str(req.get("report_dir", "")).strip())
                    report_path = os.path.expanduser(
                        str(req["report_path"]).strip())
                    cooldowns = read_number(req, "cooldowns",
                                            COOLDOWN_MAX_WAITS, whole=True)
                    delay = read_number(req, "delay", REQUEST_DELAY)
                    page_delay_v = read_number(req, "page_delay", PAGE_DELAY)
                    stop_blocked_v = bool(
                        req.get("stop_when_blocked", True))
                    max_files_v = read_number(req, "max_files", 0, whole=True)
                    max_mb_v = read_number(req, "max_mb", 0, whole=True)
                    reconcile_v = bool(req.get("reconcile", True))
                    unconfirmed_v = bool(req.get("retry_unconfirmed", False))
                    fetch_path_v = str(req.get("fetch_path",
                                               FETCH_BROWSER))
                    verify_window_v = read_number(req, "verify_window",
                                                  VERIFY_WINDOW)
                    stall_limit_v = read_number(req, "stall_limit",
                                                BROWSER_STALL_LIMIT)
                except BadField as e:
                    log("Re-run refused: {}".format(e))
                    return self._json({"error": str(e)}, 400)
                except (KeyError, ValueError, json.JSONDecodeError,
                        TypeError) as e:
                    log("Re-run refused: the request could not be read "
                        "({})".format(describe_error(e)))
                    return self._json({"error": "Bad request."}, 400)
                bad = _fetch_option_error(
                    {"fetch_path": fetch_path_v,
                     "verify_window": verify_window_v,
                     "stall_limit": stall_limit_v})
                if bad:
                    return self._json({"error": bad}, 400)
                err_ = folder_error("Save downloads to", out_dir)
                if err_:
                    return self._json({"error": err_}, 400)
                err_ = folder_error("Save reports to", report_dir, optional=True)
                if err_:
                    return self._json({"error": err_}, 400)
                if not report_path or not os.path.exists(report_path):
                    return self._json(
                        {"error": "No such report or folder: "
                                  + report_path}, 400)
                if not 0 <= cooldowns <= 20:
                    return self._json(
                        {"error": "Cooldown waits must be between 0 and "
                                  "20."}, 400)
                if max_files_v < 0 or max_mb_v < 0:
                    return self._json(
                        {"error": "A session limit cannot be negative. Use "
                                  "0 for no limit on files this session or "
                                  "megabytes this session."}, 400)
                STOP_EVENT.clear()
                PAUSE_EVENT.clear()
                if not self._claim_and_start(
                        retry_worker,
                        (session, out_dir, report_path,
                         {"cooldowns": cooldowns, "delay": delay,
                          "page_delay": page_delay_v,
                          "stop_when_blocked": stop_blocked_v,
                          "max_files": max_files_v,
                          "max_mb": max_mb_v,
                          "reconcile": reconcile_v,
                          "retry_unconfirmed": unconfirmed_v,
                          "fetch_path": fetch_path_v,
                          "verify_window": verify_window_v,
                          "stall_limit": stall_limit_v},
                         report_dir)):
                    return self._busy()
                return self._json({"ok": True})

            if self.path == "/api/start":
                if self._running():
                    return self._json({"error": "A job is already running."}, 409)
                try:
                    req = self._body()
                    out_dir = os.path.expanduser(str(req["out_dir"]).strip())
                    report_dir = os.path.expanduser(
                        str(req.get("report_dir", "")).strip())
                    mode = str(req.get("mode", "all"))
                    parents_sel = [str(p) for p in req.get("parents", [])]
                    opts = {
                        "primary": bool(req.get("primary", True)),
                        "supp": bool(req.get("supp", True)),
                        "native": bool(req.get("native", False)),
                        "embargo": bool(req.get("embargo", False)),
                        "inventory": bool(req.get("inventory", False)),
                        "stop_when_blocked": bool(
                            req.get("stop_when_blocked", True)),
                        "public": bool(req.get("public", True)),
                        "hidden": bool(req.get("hidden", True)),
                        "versions": str(req.get("versions", "current")),
                        "published": bool(req.get("published", True)),
                        "unpublished": bool(req.get("unpublished", False)),
                        "cooldowns": read_number(req, "cooldowns",
                                                 COOLDOWN_MAX_WAITS,
                                                 whole=True),
                        "delay": read_number(req, "delay", REQUEST_DELAY),
                        "page_delay": read_number(req, "page_delay",
                                                  PAGE_DELAY),
                        # Both units, because which one the server rations
                        # is not known — see SESSION_LIMITS.
                        "max_files": read_number(req, "max_files", 0,
                                                 whole=True),
                        "max_mb": read_number(req, "max_mb", 0, whole=True),
                        "fetch_path": str(req.get("fetch_path",
                                                  FETCH_BROWSER)),
                        "verify_window": read_number(req, "verify_window",
                                                     VERIFY_WINDOW),
                        "stall_limit": read_number(req, "stall_limit",
                                                   BROWSER_STALL_LIMIT),
                    }
                except BadField as e:
                    log("Start refused: {}".format(e))
                    return self._json({"error": str(e)}, 400)
                except (KeyError, ValueError, json.JSONDecodeError,
                        TypeError) as e:
                    log("Start refused: the request could not be read "
                        "({})".format(describe_error(e)))
                    return self._json({"error": "Bad request."}, 400)
                if mode not in ("all", "selected") \
                        or opts["versions"] not in ("current", "all"):
                    return self._json({"error": "Bad request."}, 400)
                bad = _fetch_option_error(opts)
                if bad:
                    return self._json({"error": bad}, 400)
                if not 0 <= opts["cooldowns"] <= 20:
                    return self._json(
                        {"error": "Cooldown waits must be between 0 and 20."},
                        400)
                if not 0 <= opts["delay"] <= REQUEST_DELAY_MAX:
                    return self._json(
                        {"error": "The gap before a file download must be "
                                  "between 0 and {:.0f} seconds.".format(
                                      REQUEST_DELAY_MAX)}, 400)
                if not 0 <= opts["page_delay"] <= REQUEST_DELAY_MAX:
                    return self._json(
                        {"error": "The gap before an admin page must be "
                                  "between 0 and {:.0f} seconds.".format(
                                      REQUEST_DELAY_MAX)}, 400)
                err_ = folder_error("Save downloads to", out_dir)
                if err_:
                    return self._json({"error": err_}, 400)
                err_ = folder_error("Save reports to", report_dir, optional=True)
                if err_:
                    return self._json({"error": err_}, 400)
                if not (opts["primary"] or opts["supp"] or opts["native"]):
                    return self._json(
                        {"error": "Check at least one file kind."}, 400)
                if not (opts["public"] or opts["hidden"]):
                    return self._json(
                        {"error": "Check at least one visibility."}, 400)
                if not (opts["published"] or opts["unpublished"]):
                    return self._json(
                        {"error": "Check at least one record class."}, 400)
                with LOCK:
                    loaded = MODEL is not None
                if not loaded:
                    return self._json(
                        {"error": "Load or map a hierarchy first."}, 400)
                if mode == "selected" and not parents_sel:
                    return self._json(
                        {"error": "Check at least one parent structure."},
                        400)
                STOP_EVENT.clear()
                PAUSE_EVENT.clear()
                if not self._claim_and_start(
                        download_worker,
                        (session, out_dir, mode, parents_sel, opts,
                         report_dir)):
                    return self._busy()
                return self._json({"ok": True})

            if self.path == "/api/form":
                payload, code = remember_form_request(self)
                return self._json(payload, code)

            if self.path == "/api/clear":
                # Refused while a run holds the slot, paused or not: the
                # log and the summary ARE that run's record until it ends.
                if self._running():
                    return self._json({"error": CLEAR_REFUSED}, 409)
                clear_for_new_run()
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
