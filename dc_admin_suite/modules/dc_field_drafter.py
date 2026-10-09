#!/usr/bin/env python3
"""
Structure HTML Drafting Tool — Module 06 of the DC Admin Suite.

Guided drafting of field HTML (intro text, policies, submission agreements,
mastheads, …) for Digital Commons structures, driven by an editable library
of annotated templates.

  Draft tab      pick status (Active/Archived) → structure type → field;
                 the matching template renders as a guided document: fixed
                 HTML dimmed, pink regions editable in place, CONDITIONAL /
                 OPTIONAL sections toggleable. Copy/Download strips the
                 guide comments (inline comments like <!-- Begin parent -->
                 are kept — they are part of the production HTML).
  Templates tab  add / edit / duplicate / export / upload / remove
                 templates; import/export the whole library as .zip;
                 restore this install's own factory set.

Templates are plain .html files in config/templates/ (next to session.json,
so the library travels with the suite). Institutional content is never
embedded in this file: config/templates/ is gitignored, and a fresh
config/templates.factory/ (also gitignored) holds this install's own
restore-to set — adopted automatically, on first launch, from whichever of
config/templates/ (an existing install upgrading) or the generic examples in
config.example/templates/ (a brand-new install) is available; see
ensure_seed(). The Draft tab pickers are generated from whatever templates
exist.

TEMPLATE FILE FORMAT
--------------------
    <!-- DC-TEMPLATE
    status: Active
    structures: Book Gallery | Community | ETD
    field: Introduction
    -->
    ...body html...

  * {{...}} marks an editable region (single-line inline, or multi-line on
    its own lines).
  * A region containing only an HTML comment ({{<!-- Intro text here. -->}})
    becomes a free-text rich editor (WYSIWYG with an HTML source toggle).
  * A full-line comment beginning REQUIRED / CONDITIONAL / OPTIONAL starts a
    new section. Conditional/required sections default to included, optional
    to excluded. Every marked section gets an Include toggle (so any section
    can be dropped from the output); required and conditional sections also
    get an Override toggle that turns the whole section into a raw-HTML editor.
    A tag-balance check warns when an Include selection leaves unbalanced HTML
    (e.g. excluding an opening <div> section but keeping its closer).
  * The Live HTML output has a "Remove all comments" toggle that also strips
    inline comments (e.g. <!-- Begin parent -->) on Copy / Download.

No Chrome attach, no third-party packages — this module never touches the
DC admin session; staff paste the finished HTML into Digital Commons
themselves.

v1.3 (2026-07) — hardening pass for public distribution: local HTTP
endpoints now reject non-local requests (Host/Origin guard against DNS
rebinding and cross-site POSTs).

v1.5 (2026-09-04) — identity refactor Step 1: factory templates are no
longer Python source. The ~20-template factory set (submission agreements,
journal policies, mastheads, …) used to live as a TEMPLATE_SEED dict
embedded in this file — institutional legal and policy text committed to
git. It now lives only as plain .html files on disk: an existing install's
real library is adopted, on first launch of this version, into a new
gitignored config/templates.factory/ restore baseline (see ensure_seed());
a brand-new install seeds — and restores — from the generic, git-tracked
examples in config.example/templates/ instead. No behavior changes for an
existing install; "Restore Factory Defaults" on a brand-new one now offers
generic examples rather than another institution's real agreements.

One workflow did change: shipping a revised module no longer updates
anyone's factory set, because that set is now the institution's own data.
Bulk-update by importing a .zip from the Templates tab, or by replacing the
files in config/templates.factory/ and clicking Restore Factory Defaults.

Copied out of the suite and run on its own, with no template library and no
config.example/templates/ to seed from, the module still starts: it falls
back to the one built-in STARTER_TEMPLATE scaffold below and says so. A
Restore Factory Defaults with nothing to restore from still refuses rather
than emptying a real library.

v1.6 (2026-09-04) — identity refactor Step 3: this module no longer
carries an institution. The standalone session fallback (used only when
config/session.json is missing) is now blank rather than one institution's
URL and name, and the branding color fallbacks read the neutral
FALLBACK_BRAND constant. Real values come from the shell, which seeds them
from config/profile.json — see profiles/README.md.
"""

# ---------------------------------------------------------------------------
# 1. MANIFEST — the shell reads ONLY this during a scan.
# ---------------------------------------------------------------------------
MANIFEST = {
    "id": "field-html-drafter",
    "name": "Structure HTML Drafting Tool",
    "description": ("Draft standards-based structure HTML using editable "
                    "context-appropriate templates."),
    "version": "1.6.3",
    "requires": [],
}

import argparse
import base64
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import webbrowser
import zipfile
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# 2. Session context (suite pattern — see _template_module.py)
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 3. Template library storage.
#    TEMPLATES_DIR is set in main() from the session path (config/templates/,
#    so the library travels with the suite). The fallback keeps helpers
#    usable when imported by tests before main() runs.
# ---------------------------------------------------------------------------
TEMPLATES_DIR = DEFAULT_SESSION.parent / "templates"
TEMPLATES_LOCK = threading.Lock()

_FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]*\.html$")

EXAMPLE_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "config.example" / "templates"
# Relative to __file__, so safe as a module-level constant — unlike anything
# derived from TEMPLATES_DIR, which main() rebinds from --session at launch.

# LAST-RESORT starter, used only when there is no template library AND no
# config.example/templates/ to seed from — i.e. this file was copied out of
# the suite and is being run on its own. It exists so the tool always starts
# with something to show instead of refusing to launch.
#
# It is deliberately ONE minimal scaffold demonstrating the file format and
# NOTHING ELSE: no institution name, no policy or legal text, no contact
# details, no links. That is the line identity refactor Step 1 draws (see
# docs/identity-refactor-plan.md) — the real example set lives in
# config.example/templates/ and belongs there, not here. Do not grow this.
STARTER_TEMPLATE = {
    "starter-template.html": """<!-- DC-TEMPLATE
status: Active
structures: Book Gallery | Community | ETD | Event Community | Image Gallery | Journal | Series
field: Introduction
-->
<!-- REQUIRED : Heading and introduction. -->
<h2>{{About <cite><macro publication.title></macro></cite>}}</h2>
{{<!-- Write the introduction for this structure here. -->}}

<!-- OPTIONAL : Excluded from the output until you tick Include. -->
{{<h3>Section heading</h3>
<p>Text inside a pair of braces is editable. A brace pair holding only an
HTML comment becomes a free-text editor.</p>}}
""",
}


def _html_files(folder: Path) -> list:
    """Sorted .html FILES in folder — never directories, and an empty list
    for a folder that does not exist. Every read path uses this, so a stray
    directory named `something.html` can't turn a launch into a traceback.
    """
    try:
        return sorted(f for f in folder.glob("*.html") if f.is_file())
    except OSError:
        return []


def _read_paths(paths: list) -> dict:
    """{filename: content} for a list of template files."""
    return {f.name: f.read_text(encoding="utf-8") for f in paths}


def _read_templates(folder: Path) -> dict:
    """{filename: content} for every .html file in folder."""
    return _read_paths(_html_files(folder))


def _first_population_content(live_html: list) -> dict:
    """Content for the FIRST population of templates.factory/ — and, on a
    brand-new install, of templates/ itself — in priority order:

      1. this install's own existing library (an install upgrading from a
         version that shipped its templates embedded in source),
      2. the generic examples shipped in config.example/templates/,
      3. the built-in starter scaffold, so a copy of this file running on
         its own outside the suite still starts.

    Called only when a first population is actually needed, so a normal
    launch reads nothing it doesn't have to.
    """
    content = _read_paths(live_html) or _read_templates(EXAMPLE_TEMPLATES_DIR)
    if content:
        return content
    print("{}: no template library and no {} — starting from the built-in "
          "starter template. Import a .zip or add templates from the "
          "Templates tab.".format(MANIFEST["name"], EXAMPLE_TEMPLATES_DIR))
    return dict(STARTER_TEMPLATE)


def _factory_source() -> Path:
    """Where ensure_seed() reads from: this install's own adopted library
    (config/templates.factory/) once one exists, else the generic example
    set shipped with the suite. Computed from the CURRENT TEMPLATES_DIR at
    call time — never cached — since main() rebinds that global.
    """
    factory_dir = TEMPLATES_DIR.parent / "templates.factory"
    if _html_files(factory_dir):
        return factory_dir
    return EXAMPLE_TEMPLATES_DIR


def _adopt_factory_baseline(factory_dir: Path, sources: dict) -> None:
    """Populate factory_dir from a {filename: content} map ATOMICALLY, or
    not at all.

    The copy is built in a temporary directory alongside and renamed into
    place only once every file is written, so templates.factory/ is always
    either absent or COMPLETE. That matters because "populated" is how
    ensure_seed() decides the baseline already exists: an adoption
    interrupted half-way — a crash, a kill, a full disk — would otherwise
    leave a partial baseline that looks finished forever, and the next
    "Restore Factory Defaults" would delete the whole live library and put
    only that fraction back.
    """
    if not sources:
        return
    parent = factory_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".templates.factory.",
                                    dir=str(parent)))
    try:
        for name, content in sources.items():
            (staging / name).write_text(content, encoding="utf-8")
        if factory_dir.is_dir():
            try:
                factory_dir.rmdir()      # succeeds only while it is empty
            except OSError:
                return                   # someone filled it first — theirs wins
        try:
            staging.rename(factory_dir)
        except OSError:
            return                       # lost the race; keep what is there
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def ensure_seed(force: bool = False) -> int:
    """Create TEMPLATES_DIR and populate it. Also self-heals the
    config/templates.factory/ restore baseline so it is ALWAYS populated
    after this runs, with a stable meaning fixed at first population:

      - templates.factory/ empty, templates/ already populated (an install
        upgrading from a version that shipped templates embedded in source)
        -> adopt the live library as this install's own restore baseline.
      - templates.factory/ empty, templates/ also empty (a brand-new
        install) -> seed templates.factory/ from the generic examples too,
        so "Restore Factory Defaults" means "the shipped examples" from the
        first launch on, not whatever templates/ happens to hold by the
        second one.

    Both cases happen before any force-restore can act, only the first time
    (once templates.factory/ is populated it is never rewritten here), and
    atomically — see _adopt_factory_baseline.

    force=True then replaces existing .html files (Restore Defaults); the
    seed source is read and confirmed non-empty BEFORE anything existing is
    deleted, and with no source it RAISES rather than silently wiping the
    library and writing nothing back.

    Seeding an empty library never raises, though: with no source at all it
    falls back to the built-in starter (see _first_population_content), so
    a copy of this file running outside the suite still starts.
    """
    with TEMPLATES_LOCK:
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

        factory_dir = TEMPLATES_DIR.parent / "templates.factory"
        live_html = _html_files(TEMPLATES_DIR)

        if not _html_files(factory_dir):
            _adopt_factory_baseline(
                factory_dir, _first_population_content(live_html))

        if live_html and not force:
            return 0

        src = _factory_source()
        seed = _read_templates(src)
        if not seed:
            if force:
                # Restore Factory Defaults with nothing to restore FROM. The
                # starter is never allowed to rescue this path: replacing a
                # real library with one placeholder file is exactly the
                # data loss this guard exists to prevent.
                raise RuntimeError(
                    "No factory templates found under {} — refusing to wipe "
                    "the existing library.".format(src))
            # Seeding an EMPTY library (the early return above proves it was
            # empty), so there is nothing to lose: fall back rather than
            # refusing to launch.
            seed = _first_population_content([])
        if force:
            for f in _html_files(TEMPLATES_DIR):
                f.unlink()
        for name, content in seed.items():
            (TEMPLATES_DIR / name).write_text(content, encoding="utf-8")
        return len(seed)


def list_templates() -> list:
    with TEMPLATES_LOCK:
        out = []
        for f in sorted(TEMPLATES_DIR.glob("*.html")):
            try:
                out.append({"filename": f.name,
                            "content": f.read_text(encoding="utf-8")})
            except OSError:
                pass
        return out


def valid_filename(name: str) -> bool:
    return bool(_FNAME_RE.match(name)) and "/" not in name and "\\" not in name


def save_template(filename: str, content: str, rename_from: str = "") -> None:
    if not valid_filename(filename):
        raise ValueError("Invalid filename (letters/digits/.-_() only, "
                         "must end in .html).")
    if "DC-TEMPLATE" not in content[:400]:
        raise ValueError("Content must begin with a "
                         "<!-- DC-TEMPLATE ... --> header.")
    with TEMPLATES_LOCK:
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        (TEMPLATES_DIR / filename).write_text(content, encoding="utf-8")
        if rename_from and rename_from != filename and valid_filename(rename_from):
            old = TEMPLATES_DIR / rename_from
            if old.exists():
                old.unlink()


def delete_template(filename: str) -> None:
    if not valid_filename(filename):
        raise ValueError("Invalid filename.")
    with TEMPLATES_LOCK:
        f = TEMPLATES_DIR / filename
        if f.exists():
            f.unlink()


def export_zip_bytes() -> bytes:
    with TEMPLATES_LOCK:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(TEMPLATES_DIR.glob("*.html")):
                z.writestr(f.name, f.read_text(encoding="utf-8"))
        return buf.getvalue()


def import_zip_bytes(data: bytes) -> int:
    """Extract .html members (flattened to basename) into TEMPLATES_DIR."""
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if not base.lower().endswith(".html") or not valid_filename(base):
                continue
            try:
                text = z.read(info).decode("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "DC-TEMPLATE" not in text[:400]:
                continue
            with TEMPLATES_LOCK:
                TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                (TEMPLATES_DIR / base).write_text(text, encoding="utf-8")
            n += 1
    return n


# ---------------------------------------------------------------------------
# 4. UI — branded page per suite conventions (WCAG 2.1 AA). Tokens are
#    substituted at startup (no str.format, so CSS/JS braces stay untouched).
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


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ — __SUITE__</title>
<style>
 :root{
   --primary:__PRIMARY__; --accent:__ACCENT__; --neutral:__NEUTRAL__;
   --onprimary:__ONPRIMARY__;
   --req:#a33a12; --cond:#6b4a8f; --opt:#4a6741;
   --pink-bg:#fbeaf6; --pink-line:#d8a0c8; --pink-ink:#8f2a6f;
   --card-line:#d7d4cc;
 }
 *{box-sizing:border-box}
 body{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f7f6f3;color:#1b1b1f;padding-bottom:4rem}
 header{background:var(--primary);color:var(--onprimary);padding:1rem 1.25rem}
 header h1{margin:0;font-size:1.2rem}
 header p{margin:.25rem 0 0;font-size:.9rem}
 header a{color:var(--onprimary)}
 .bar{height:5px;background:var(--accent)}
 main{max-width:64rem;margin:0 auto;padding:1.25rem}
 :focus-visible{outline:3px solid #1a5dc8;outline-offset:2px}

 /* Tabs */
 .tabs{display:flex;gap:.4rem;border-bottom:2px solid var(--card-line);margin-bottom:1rem}
 .tab{font:inherit;font-weight:600;font-size:.9rem;padding:.5rem 1.1rem;background:#e7e3da;
      border:1px solid var(--card-line);border-bottom:none;border-radius:6px 6px 0 0;cursor:pointer;color:#4a4a50}
 .tab[aria-selected=true]{background:#fff;color:var(--primary);position:relative;top:2px;padding-bottom:.62rem}

 /* Cards */
 .card{background:#fff;border:1px solid var(--card-line);border-radius:6px;padding:1.1rem 1.25rem;margin:0 0 1rem}
 .card h2{margin:0 0 .3rem;font-size:1.05rem;color:var(--primary)}
 .card .desc{margin:0 0 .8rem;font-size:.9rem;color:#4a4a50}
 label{display:block;font-size:.85rem;font-weight:600;margin-bottom:.25rem}
 select,input[type=text],textarea{width:100%;padding:.45rem .55rem;border:1px solid #a9a396;border-radius:5px;font:inherit;background:#fff}
 .picker-row{display:grid;grid-template-columns:11rem 1fr 1fr;gap:.9rem;align-items:end}
 .hint{font-size:.85rem;color:#5a5a60;margin:.4rem 0 0}
 .hint code,.legend code{font:.85em ui-monospace,monospace;background:#eef1f5;padding:1px 4px;border-radius:3px}

 /* Status pill */
 .pill-toggle{display:flex;border:1px solid #a9a396;border-radius:5px;overflow:hidden;height:2.4rem}
 .pill-opt{flex:1;display:flex;align-items:center;justify-content:center;padding:0 .8rem;cursor:pointer;
           background:#fff;font-size:.8rem;font-weight:600;color:#4a4a50;white-space:nowrap;user-select:none}
 .pill-opt input{position:absolute;opacity:0;pointer-events:none}
 .pill-opt:has(input:checked){background:var(--primary);color:var(--onprimary)}
 .pill-opt:has(input:focus-visible){outline:3px solid #1a5dc8;outline-offset:-3px}
 .pill-opt.disabled{opacity:.45;cursor:not-allowed}

 /* Guided template document */
 .tpl-doc{display:flex;flex-direction:column;gap:.8rem}
 .tpl-sec{border:1px solid var(--card-line);border-left:5px solid var(--card-line);border-radius:5px;background:#fbfaf7}
 .tpl-sec.kind-required{border-left-color:var(--req)}
 .tpl-sec.kind-conditional{border-left-color:var(--cond)}
 .tpl-sec.kind-optional{border-left-color:var(--opt)}
 .tpl-sec.off{opacity:.55}
 .tpl-sec.off .sec-body{display:none}
 .sec-head{display:flex;align-items:center;gap:.6rem;padding:.5rem .8rem;border-bottom:1px dashed var(--card-line)}
 .tpl-sec.off .sec-head{border-bottom:none}
 .sec-badge{font-size:.68rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
            padding:.1rem .55rem;border-radius:99px;color:#fff;flex-shrink:0}
 .kind-required .sec-badge{background:var(--req)}
 .kind-conditional .sec-badge{background:var(--cond)}
 .kind-optional .sec-badge{background:var(--opt)}
 .sec-label{font-size:.85rem;color:#4a4a50;flex:1}
 .sec-controls{display:flex;align-items:center;gap:1rem;flex-shrink:0}
 .sec-toggle{display:flex;align-items:center;gap:.35rem;flex-shrink:0;font-size:.78rem;font-weight:600;
             color:#4a4a50;cursor:pointer;user-select:none}
 .sec-toggle input{width:1rem;height:1rem;accent-color:var(--primary);cursor:pointer}
 .sec-toggle.override input{accent-color:var(--accent)}
 .sec-body{padding:.7rem .8rem}
 textarea.override-src{width:100%;min-height:8rem}
 .draft-warn{margin:0 0 1rem}
 .tpl-line{font:.8rem/1.9 ui-monospace,monospace;color:#5a5a60;word-break:break-word;white-space:pre-wrap}
 .tpl-blank{height:.6em}
 .edit-inline{display:inline;background:var(--pink-bg);border:1px solid var(--pink-line);border-radius:3px;
              padding:1px 4px;color:var(--pink-ink);outline:none;white-space:pre-wrap}
 .edit-inline:focus{border-color:var(--pink-ink);box-shadow:0 0 0 2px rgba(143,42,111,.18)}
 .region-block{margin:.5rem 0}
 .region-cap{font-size:.72rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
             color:var(--pink-ink);margin-bottom:.25rem}
 textarea.src-only{font:.8rem/1.7 ui-monospace,monospace;background:var(--pink-bg);
                   border:1px solid var(--pink-line);color:#4a2a40;min-height:6rem;resize:vertical}
 textarea.src-only:focus{border-color:var(--pink-ink)}

 /* Rich text editor */
 .wysiwyg-wrap{border:1px solid var(--pink-line);border-radius:5px;overflow:hidden;background:#fff}
 .wysiwyg-wrap:focus-within{border-color:var(--pink-ink);box-shadow:0 0 0 2px rgba(143,42,111,.18)}
 .wysiwyg-toolbar{display:flex;flex-wrap:wrap;gap:2px;padding:.25rem .4rem;background:var(--pink-bg);
                  border-bottom:1px solid var(--pink-line);align-items:center}
 .tb-btn{display:inline-flex;align-items:center;justify-content:center;min-width:1.8rem;height:1.7rem;
         padding:0 .4rem;font:.78rem ui-monospace,monospace;font-weight:700;background:transparent;
         border:1px solid transparent;border-radius:4px;cursor:pointer;color:#6a3a5a}
 .tb-btn:hover{background:#fff;border-color:var(--pink-line)}
 .tb-btn.active{background:var(--pink-ink);color:#fff}
 .tb-btn svg{width:13px;height:13px;fill:currentColor}
 .tb-sep{width:1px;height:1.2rem;background:var(--pink-line);margin:0 3px;flex-shrink:0}
 .wysiwyg-body{min-height:6rem;max-height:20rem;overflow-y:auto;padding:.7rem .85rem;font-size:.9rem;outline:none}
 .wysiwyg-body:empty::before{content:attr(data-placeholder);color:#c0a0b5;pointer-events:none}
 .wysiwyg-body h3{font-size:1rem;margin:.5rem 0 .25rem;color:var(--primary)}
 .wysiwyg-body h4{font-size:.93rem;margin:.45rem 0 .2rem}
 .wysiwyg-body h5{font-size:.85rem;text-transform:uppercase;letter-spacing:.04em;margin:.45rem 0 .2rem;color:#4a4a50}
 .wysiwyg-body p{margin:0 0 .4rem}
 .wysiwyg-body ul,.wysiwyg-body ol{padding-left:1.4rem;margin:0 0 .4rem}
 .wysiwyg-body a{color:#0563C1;text-decoration:underline}
 .wysiwyg-source{display:none;width:100%;min-height:6rem;max-height:20rem;font:.78rem/1.7 ui-monospace,monospace;
                 color:#2a3a2a;background:#f0f4f0;border:none;outline:none;resize:vertical;padding:.7rem .85rem}
 .wysiwyg-wrap.source-mode .wysiwyg-body{display:none}
 .wysiwyg-wrap.source-mode .wysiwyg-source{display:block}
 .wysiwyg-wrap.source-mode .tb-btn:not(.tb-src){opacity:.35;pointer-events:none}
 .tb-btn.html-active{background:var(--primary);color:var(--onprimary)}

 /* Link modal */
 .modal-backdrop{display:none;position:fixed;inset:0;background:rgba(10,14,28,.55);z-index:300;
                 align-items:center;justify-content:center}
 .modal-backdrop.open{display:flex}
 .modal{background:#fff;border-radius:8px;padding:1.3rem 1.4rem;width:min(28rem,92vw);
        box-shadow:0 20px 60px rgba(0,0,0,.3)}
 .modal h3{margin:0 0 .8rem;font-size:1rem;color:var(--primary)}
 .modal .field{margin-bottom:.8rem}
 .modal-actions{display:flex;gap:.6rem;justify-content:flex-end;margin-top:.9rem}
 .mini-pill{display:flex;border:1px solid #a9a396;border-radius:5px;overflow:hidden;height:2.1rem;width:fit-content}
 .mini-pill .pill-opt{flex:none}

 /* Output panel */
 .output-card{background:#1b1b1f;border-radius:6px;margin:0 0 1rem;overflow:hidden;border:1px solid #3a3a42}
 .output-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem;
                padding:.55rem .9rem;background:#121216;border-bottom:1px solid #3a3a42}
 .output-header-left{display:flex;align-items:center;gap:.6rem}
 .output-title{font:.72rem ui-monospace,monospace;letter-spacing:.1em;color:#9aa8c0;text-transform:uppercase}
 .live-dot{width:8px;height:8px;background:#3cb97a;border-radius:50%}
 @media (prefers-reduced-motion: no-preference){
   .live-dot{animation:pulse 2s ease-in-out infinite}
   @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
 }
 .out-actions{display:flex;gap:.4rem;align-items:center}
 .out-toggle{display:flex;align-items:center;gap:.35rem;font:.68rem ui-monospace,monospace;letter-spacing:.05em;
             color:#aebcd8;text-transform:uppercase;cursor:pointer;user-select:none;margin-right:.4rem}
 .out-toggle input{accent-color:var(--accent);cursor:pointer}
 .out-btn{font:.72rem ui-monospace,monospace;letter-spacing:.07em;padding:.3rem .8rem;border-radius:4px;
          cursor:pointer;text-transform:uppercase;border:1px solid #5a6a8a;color:#aebcd8;background:transparent}
 .out-btn:hover{background:var(--accent);border-color:var(--accent);color:#000}
 .out-btn.primary{background:var(--accent);border-color:var(--accent);color:#000;font-weight:700}
 .pre-flight{padding:.6rem .9rem;background:#22222a;border-bottom:1px solid #3a3a42}
 .pre-flight summary{font:.72rem ui-monospace,monospace;letter-spacing:.08em;color:var(--accent);
                     text-transform:uppercase;cursor:pointer;user-select:none}
 .pre-flight ul{list-style:none;margin:.5rem 0 0;padding:0;display:flex;flex-direction:column;gap:.3rem}
 .pre-flight li{font-size:.82rem;color:#aebcd8;display:flex;gap:.5rem;line-height:1.5}
 .pf-icon{flex-shrink:0;color:#3cb97a}
 .output-pre{font:.8rem/1.75 ui-monospace,monospace;color:#d8e2f2;padding:1rem;white-space:pre-wrap;
             word-break:break-word;min-height:5rem;margin:0}
 .hl-cmt{color:#7a8496;font-style:italic}
 .hl-tag{color:#7fbbdd}
 .hl-attr{color:#c0ace8}
 .hl-val{color:#98d0a0}

 /* Buttons */
 .btn{background:var(--primary);color:var(--onprimary);border:0;border-radius:6px;
      padding:.5rem 1.1rem;font:inherit;font-size:.88rem;font-weight:600;cursor:pointer}
 .btn.secondary{background:#e7e3da;color:#1b1b1f}
 .btn.danger{background:#fff;color:#a33a12;border:1px solid #a33a12}
 .btn.danger:hover{background:#a33a12;color:#fff}
 .bottom-actions{display:flex;justify-content:flex-end;gap:.6rem;margin:0 0 1rem}

 /* Templates tab */
 .mgr-toolbar{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.6rem}
 .tpl-group-title{font-size:.78rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
                  color:var(--accent);margin:1rem 0 .4rem;filter:brightness(.75)}
 .tpl-row{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;background:#fff;
          border:1px solid var(--card-line);border-radius:5px;padding:.55rem .8rem;margin-bottom:.4rem}
 .tpl-row .t-field{font-weight:700;font-size:.92rem;min-width:11rem}
 .tpl-row .t-structs{display:flex;gap:.3rem;flex-wrap:wrap;flex:1}
 .badge{font-size:.72rem;padding:.05rem .5rem;border-radius:99px;background:#eef1f5;
        border:1px solid var(--card-line);color:#4a4a50}
 .tpl-row .t-file{font:.72rem ui-monospace,monospace;color:#8a8a90;width:100%}
 .row-btn{font-size:.76rem;font-weight:600;padding:.2rem .6rem;border-radius:4px;
          border:1px solid var(--card-line);background:#fff;color:#4a4a50;cursor:pointer}
 .row-btn:hover{border-color:var(--primary);color:var(--primary)}
 .row-btn.danger:hover{border-color:#a33a12;color:#a33a12}
 #tpl-editor textarea#ed-body{font:.8rem/1.7 ui-monospace,monospace;min-height:26rem;resize:vertical}
 .ed-grid{display:grid;grid-template-columns:1fr 19rem;gap:1rem}
 .legend{background:#eef1f5;border:1px solid var(--card-line);border-radius:5px;
         padding:.7rem .85rem;font-size:.82rem;line-height:1.6;color:#3a3a40}
 .legend h4{margin:0 0 .4rem;font-size:.76rem;letter-spacing:.07em;text-transform:uppercase;color:var(--primary)}
 .legend p{margin:0 0 .5rem}
 #ed-check{margin-top:.7rem;font-size:.82rem;line-height:1.6}
 #ed-check .ok{color:var(--opt)}
 #ed-check .warn{color:var(--req)}
 .notice{position:relative;padding:.6rem 2.4rem .6rem .8rem;border-radius:5px;
         font-size:.86rem;margin:.7rem 0;
         background:#e8f0e6;border-left:4px solid var(--opt)}
 .notice.err{background:#f7e8e2;border-left-color:var(--req)}
 /* Close button on a contextual error: the error stays put until it is
    clicked. Only confirmations are allowed to fade on a timer. */
 .msg-x{position:absolute;top:.3rem;right:.35rem;background:none;
        border:1px solid transparent;border-radius:4px;color:inherit;font:inherit;
        font-size:1.05rem;font-weight:700;line-height:1;cursor:pointer;
        padding:.1rem .4rem}
 .msg-x:hover{background:rgba(60,60,70,.12)}
 .empty-msg{padding:1.6rem;text-align:center;color:#8a8a90;font-size:.9rem}

 @media (max-width:48rem){
   .picker-row,.ed-grid{grid-template-columns:1fr}
 }
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

<div class="tabs" role="tablist" aria-label="Drafter sections">
  <button class="tab" id="tab-draft" role="tab" aria-selected="true" onclick="showTab('draft')">Draft</button>
  <button class="tab" id="tab-manage" role="tab" aria-selected="false" onclick="showTab('manage')">Templates</button>
</div>

<!-- ═══════════ DRAFT TAB ═══════════ -->
<section id="pane-draft" role="tabpanel" aria-labelledby="tab-draft">

  <div class="card">
    <h2>1 · Choose a template</h2>
    <p class="desc">Select the collection status, structure type, and the Digital Commons
    field you are preparing HTML for. The pickers reflect the current template library.</p>
    <div class="picker-row">
      <div>
        <label id="status-label">Status</label>
        <div class="pill-toggle" id="status-pills" role="radiogroup" aria-labelledby="status-label"></div>
      </div>
      <div>
        <label for="sel-structure">Structure type</label>
        <select id="sel-structure"></select>
      </div>
      <div>
        <label for="sel-field">Field</label>
        <select id="sel-field"></select>
      </div>
    </div>
    <p class="hint" id="tpl-info"></p>
  </div>

  <div class="card" id="draft-card" style="display:none;">
    <h2>2 · Make your edits</h2>
    <p class="desc">Fixed HTML is dimmed and cannot be changed.
    <strong style="color:var(--pink-ink);">Pink regions</strong> are editable — click and type.
    Every section has an <strong>Include</strong> toggle so you can drop it from the output.
    <span class="sec-badge" style="background:var(--req);">Required</span> and
    <span class="sec-badge" style="background:var(--cond);">Conditional</span> sections also offer an
    <strong>Override</strong> toggle that makes the entire section editable as raw HTML.</p>
    <div class="draft-warn" id="draft-warn" role="alert" aria-live="assertive"></div>
    <div class="tpl-doc" id="tpl-doc"></div>
  </div>

  <div class="output-card" id="output-card" style="display:none;">
    <div class="output-header">
      <div class="output-header-left">
        <div class="live-dot" role="presentation"></div>
        <span class="output-title">Live HTML output</span>
      </div>
      <div class="out-actions">
        <label class="out-toggle" title="Also remove inline comments (e.g. &lt;!-- Begin parent --&gt;) when copying or downloading">
          <input type="checkbox" id="strip-all-comments" onchange="scheduleUpdate()"><span>Remove all comments on copy/save</span></label>
        <button class="out-btn" id="copy-btn" onclick="copyOutput()">Copy</button>
        <button class="out-btn primary" onclick="downloadOutput()">Save .html</button>
      </div>
    </div>
    <details class="pre-flight">
      <summary>Pre-flight checklist (expand before pasting into Digital Commons)</summary>
      <ul>
        <li><span class="pf-icon">&#x2726;</span>Switch the DC field to <strong>HTML / Source mode</strong> before pasting.</li>
        <li><span class="pf-icon">&#x2726;</span>Copy removes the REQUIRED / CONDITIONAL / OPTIONAL guide comments automatically; inline comments (e.g. <code style="color:#98d0a0;">&lt;!-- Begin parent --&gt;</code>) are kept unless you tick <strong>Remove all comments</strong> above.</li>
        <li><span class="pf-icon">&#x2726;</span>Replace any remaining <strong>[bracketed placeholders]</strong> before publishing.</li>
        <li><span class="pf-icon">&#x2726;</span>Preview the public page and verify headings, links, and macros render correctly.</li>
        <li><span class="pf-icon">&#x2726;</span>If uncertain about required settings or edits, consult the IRM or DSL.</li>
      </ul>
    </details>
    <pre class="output-pre" id="output-pre" aria-live="polite" aria-label="Generated HTML"></pre>
  </div>

  <div class="bottom-actions" id="draft-actions" style="display:none;">
    <button class="btn secondary" onclick="if(confirm('Discard your edits and reload the template?')) renderDraft();">Reset draft</button>
  </div>
</section>

<!-- ═══════════ TEMPLATES TAB ═══════════ -->
<section id="pane-manage" role="tabpanel" aria-labelledby="tab-manage" style="display:none;">

  <div class="card">
    <h2>Template library</h2>
    <p class="desc">Templates are plain <code>.html</code> files stored in the suite's
    <code>config/templates/</code> folder. Add, revise, or remove templates here — the Draft
    tab pickers update automatically. For a bulk update, import a .zip of template files or
    restore this install's own factory set.</p>
    <div class="mgr-toolbar">
      <button class="btn" onclick="newTemplate()">+ New template</button>
      <button class="btn secondary" onclick="document.getElementById('up-file').click()">Upload .html</button>
      <button class="btn secondary" onclick="document.getElementById('up-zip').click()">Import .zip</button>
      <button class="btn secondary" onclick="window.location='/api/export-zip'">Export all (.zip)</button>
      <button class="btn danger" onclick="resetDefaults()">Restore factory defaults</button>
      <input type="file" id="up-file" accept=".html" multiple style="display:none;"
             aria-label="Upload template files" onchange="uploadFiles(this.files)">
      <input type="file" id="up-zip" accept=".zip" style="display:none;"
             aria-label="Import template zip" onchange="uploadZip(this.files[0])">
    </div>
    <div id="mgr-msg" role="status" aria-live="polite"></div>
    <div id="tpl-list"></div>
  </div>

  <div class="card" id="tpl-editor" style="display:none;">
    <h2 id="ed-title">Edit template</h2>
    <p class="desc">Edit the raw template. The metadata header drives the Draft tab pickers;
    <code>{{...}}</code> marks editable regions. The check panel updates as you type.</p>
    <div style="margin-bottom:.8rem;">
      <label for="ed-filename">Filename</label>
      <input type="text" id="ed-filename" placeholder="active--journal--policies.html">
    </div>
    <div class="ed-grid">
      <div>
        <label for="ed-body">Template source</label>
        <textarea id="ed-body" spellcheck="false"></textarea>
      </div>
      <div>
        <div class="legend">
          <h4>Template format</h4>
          <p>Start with a metadata header:<br>
          <code>&lt;!-- DC-TEMPLATE</code><br>
          <code>status: Active</code><br>
          <code>structures: A | B | C</code><br>
          <code>field: Introduction</code><br>
          <code>--&gt;</code></p>
          <p><code>{{text}}</code> &mdash; editable region. May span multiple lines
          (placed on its own lines).</p>
          <p><code>{{&lt;!-- Intro text here. --&gt;}}</code> &mdash; a region containing
          only a comment becomes a free-text rich editor.</p>
          <p>A full-line comment starting <code>REQUIRED</code>, <code>CONDITIONAL</code>,
          or <code>OPTIONAL</code> begins a section. Conditional and Optional sections get
          an Include toggle in the Draft tab.</p>
        </div>
        <div id="ed-check" role="status" aria-live="polite"></div>
      </div>
    </div>
    <div class="bottom-actions" style="margin-top:.8rem;">
      <button class="btn secondary" onclick="closeEditor()">Cancel</button>
      <button class="btn" onclick="saveEditor()">Save template</button>
    </div>
  </div>
</section>

</main>

<!-- Link modal (shared by all rich-text editors) -->
<div class="modal-backdrop" id="link-modal" role="dialog" aria-modal="true" aria-labelledby="modal-heading">
  <div class="modal">
    <h3 id="modal-heading">Insert link</h3>
    <div class="field">
      <label for="modal-url">URL</label>
      <input type="text" id="modal-url" placeholder="https://">
    </div>
    <div class="field">
      <label for="modal-text">Link text (leave blank to keep selection)</label>
      <input type="text" id="modal-text" placeholder="Optional display text">
    </div>
    <div class="field">
      <label id="target-label">Opens in</label>
      <div class="mini-pill pill-toggle" role="radiogroup" aria-labelledby="target-label">
        <label class="pill-opt"><input type="radio" name="modal-target" value="new" checked><span>New tab</span></label>
        <label class="pill-opt"><input type="radio" name="modal-target" value="same"><span>Same window</span></label>
      </div>
      <p class="hint">New-tab links get &ldquo;(opens in new tab)&rdquo; appended automatically.</p>
    </div>
    <p id="modal-err" class="dc-bad" role="alert" hidden style="font-weight:600"></p>
    <div class="modal-actions">
      <button type="button" class="btn secondary" onclick="closeLinkModal()">Cancel</button>
      <button type="button" class="btn" onclick="insertLinkFromModal()">Insert</button>
    </div>
  </div>
</div>

<script>
'use strict';
// ══════════════════════════════════════════════
//  STATE
// ══════════════════════════════════════════════
let TPLS = [];        // [{filename, content, meta, regions, sections}]
let CUR = null;       // template currently loaded in Draft tab
let REGIONS = [];     // per-region UI state for CUR
let SECTIONS = [];    // per-section UI state for CUR
let editingFile = null; // filename being edited in Templates tab (null = new)

const $ = s => document.querySelector(s);

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ══════════════════════════════════════════════
//  TEMPLATE PARSING
// ══════════════════════════════════════════════
const SEC_RE = /^\s*<!--\s*(required|conditional|optional)\b\s*:?\s*([\s\S]*?)\s*-->\s*$/i;

function parseTemplate(content) {
  const meta = { status: '', structures: [], field: '', notes: '' };
  let body = content;
  const hm = content.match(/^﻿?\s*<!--\s*DC-TEMPLATE([\s\S]*?)-->\s*\r?\n?/);
  if (hm) {
    body = content.slice(hm[0].length);
    hm[1].split(/\r?\n/).forEach(l => {
      const kv = l.match(/^\s*([\w-]+)\s*:\s*(.*)$/);
      if (!kv) return;
      const k = kv[1].toLowerCase(), v = kv[2].trim();
      if (k === 'structures') meta.structures = v.split('|').map(x => x.trim()).filter(Boolean);
      else if (k in meta) meta[k] = v;
    });
  }
  // Tokenize editable regions -> placeholders \x00N\x00
  const regions = [];
  const bodyTok = body.replace(/\{\{([\s\S]*?)\}\}/g, (m, inner) => {
    regions.push(inner);
    return '\x00' + (regions.length - 1) + '\x00';
  });
  const leftover = /\{\{|\}\}/.test(bodyTok);
  // Split into sections on REQUIRED/CONDITIONAL/OPTIONAL marker comments
  const sections = [];
  let cur = { kind: 'plain', label: '', comment: '', lines: [] };
  bodyTok.split(/\r?\n/).forEach(line => {
    const sm = line.match(SEC_RE);
    if (sm) {
      sections.push(cur);
      cur = { kind: sm[1].toLowerCase(), label: sm[2], comment: line.trim(), lines: [] };
      return;
    }
    cur.lines.push(line);
  });
  sections.push(cur);
  sections.forEach(s => {
    while (s.lines.length && !s.lines[0].trim()) s.lines.shift();
    while (s.lines.length && !s.lines[s.lines.length - 1].trim()) s.lines.pop();
  });
  return { meta, regions, sections: sections.filter(s => s.lines.length || s.kind !== 'plain'), leftover };
}

function regionType(src) {
  const t = src.trim();
  if (/^<!--[\s\S]*-->$/.test(t) && t.indexOf('-->') === t.length - 3) return 'free';
  if (t.includes('\n')) return 'block';
  return 'inline';
}

// Visual mode is only safe when the fragment round-trips through the
// serializer without loss: no comments, only allowed top-level blocks.
function visualSafe(src) {
  if (src.includes('<!--')) return false;
  const div = document.createElement('div');
  div.innerHTML = src;
  const allowed = ['P','H3','H4','H5','UL','OL','BLOCKQUOTE','BR'];
  for (const el of div.children) if (!allowed.includes(el.tagName)) return false;
  return true;
}

// ══════════════════════════════════════════════
//  LOAD + PICKERS
// ══════════════════════════════════════════════
async function loadTemplates(keepSelection) {
  const prev = keepSelection ? currentSelection() : null;
  const r = await fetch('/api/templates');
  const j = await r.json();
  TPLS = j.templates.map(t => Object.assign({ filename: t.filename, content: t.content }, parseTemplate(t.content)));
  buildStatusPills(prev);
  renderList();
}

function currentSelection() {
  return {
    status: document.querySelector('input[name=status]:checked')?.value || null,
    structure: $('#sel-structure').value || null,
    field: $('#sel-field').value || null,
  };
}

function statuses() {
  const known = ['Active', 'Archived'];
  const present = [...new Set(TPLS.map(t => t.meta.status).filter(Boolean))];
  const extra = present.filter(s => !known.includes(s));
  return known.concat(extra);
}

function buildStatusPills(prev) {
  const holder = $('#status-pills');
  holder.innerHTML = '';
  const have = new Set(TPLS.map(t => t.meta.status));
  let chosen = prev && prev.status && have.has(prev.status) ? prev.status : null;
  if (!chosen) chosen = statuses().find(s => have.has(s)) || 'Active';
  statuses().forEach(s => {
    const lab = document.createElement('label');
    lab.className = 'pill-opt' + (have.has(s) ? '' : ' disabled');
    const inp = document.createElement('input');
    inp.type = 'radio'; inp.name = 'status'; inp.value = s;
    inp.checked = (s === chosen);
    inp.disabled = !have.has(s);
    inp.addEventListener('change', () => buildStructurePicker());
    const span = document.createElement('span');
    span.textContent = have.has(s) ? s : s + ' (none)';
    lab.appendChild(inp); lab.appendChild(span);
    holder.appendChild(lab);
  });
  buildStructurePicker(prev);
}

function buildStructurePicker(prev) {
  const status = document.querySelector('input[name=status]:checked')?.value;
  const sel = $('#sel-structure');
  const structs = [...new Set(TPLS.filter(t => t.meta.status === status)
                                  .flatMap(t => t.meta.structures))].sort();
  sel.innerHTML = structs.map(s => '<option>' + esc(s) + '</option>').join('');
  if (prev && prev.structure && structs.includes(prev.structure)) sel.value = prev.structure;
  buildFieldPicker(prev);
}

function buildFieldPicker(prev) {
  const status = document.querySelector('input[name=status]:checked')?.value;
  const structure = $('#sel-structure').value;
  const sel = $('#sel-field');
  const matches = TPLS.filter(t => t.meta.status === status && t.meta.structures.includes(structure));
  sel.innerHTML = matches.map(t => '<option value="' + esc(t.filename) + '">' + esc(t.meta.field) + '</option>').join('');
  if (prev && prev.field && matches.some(t => t.filename === prev.field)) sel.value = prev.field;
  pickTemplate();
}

$('#sel-structure').addEventListener('change', () => buildFieldPicker());
$('#sel-field').addEventListener('change', () => pickTemplate());

function pickTemplate() {
  const fn = $('#sel-field').value;
  CUR = TPLS.find(t => t.filename === fn) || null;
  if (!CUR) {
    $('#tpl-info').textContent = 'No template available for this selection.';
    $('#draft-card').style.display = 'none';
    $('#output-card').style.display = 'none';
    $('#draft-actions').style.display = 'none';
    return;
  }
  $('#tpl-info').innerHTML = 'Template: <code>' + esc(CUR.filename) + '</code> &middot; applies to: ' +
    CUR.meta.structures.map(s => '<code>' + esc(s) + '</code>').join(' ');
  renderDraft();
}

// ══════════════════════════════════════════════
//  RICH TEXT EDITOR (instantiable)
// ══════════════════════════════════════════════
let activeRTE = null;   // editor that opened the link modal
let savedRange = null;

const TB_ICONS = {
  bold: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.6 10.79c.97-.67 1.65-1.77 1.65-2.79 0-2.26-1.75-4-4-4H7v14h7.04c2.09 0 3.71-1.7 3.71-3.79 0-1.52-.86-2.82-2.15-3.42zM10 6.5h3c.83 0 1.5.67 1.5 1.5s-.67 1.5-1.5 1.5h-3v-3zm3.5 9H10v-3h3.5c.83 0 1.5.67 1.5 1.5s-.67 1.5-1.5 1.5z"/></svg>',
  italic: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4v3h2.21l-3.42 8H6v3h8v-3h-2.21l3.42-8H18V4z"/></svg>',
  ul: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10.5c-.83 0-1.5.67-1.5 1.5s.67 1.5 1.5 1.5 1.5-.67 1.5-1.5-.67-1.5-1.5-1.5zm0-6c-.83 0-1.5.67-1.5 1.5S3.17 7.5 4 7.5 5.5 6.83 5.5 6 4.83 4.5 4 4.5zm0 12c-.83 0-1.5.68-1.5 1.5s.68 1.5 1.5 1.5 1.5-.68 1.5-1.5-.67-1.5-1.5-1.5zM7 19h14v-2H7v2zm0-6h14v-2H7v2zm0-8v2h14V5H7z"/></svg>',
  ol: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 17h2v.5H3v1h1v.5H2v1h3v-4H2v1zm1-9h1V4H2v1h1v3zm-1 3h1.8L2 13.1v.9h3v-1H3.2L5 10.9V10H2v1zm5-6v2h14V5H7zm0 14h14v-2H7v2zm0-6h14v-2H7v2z"/></svg>',
  link: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/></svg>',
  unlink: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 7h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1 0 1.43-.98 2.63-2.31 3.0l1.43 1.43C20.76 15.26 22 13.77 22 12c0-2.76-2.24-5-5-5zm-1 4h-2.19l2 2H16v-2zM2 4.27l3.11 3.11A4.991 4.991 0 002 12c0 2.76 2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1 0-1.59 1.21-2.9 2.76-3.07L8.73 11H8v2h2.73l2 2H8v1.9h4.27L15.73 20 17 18.73 3.27 5 2 4.27z"/></svg>'
};

function serializeInline(node) {
  let out = '';
  node.childNodes.forEach(child => {
    if (child.nodeType === Node.TEXT_NODE) { out += esc(child.textContent); return; }
    if (child.nodeType !== Node.ELEMENT_NODE) return;
    const tag = child.tagName.toLowerCase();
    if (tag === 'b' || tag === 'strong') { out += '<strong>' + serializeInline(child) + '</strong>'; return; }
    if (tag === 'i' || tag === 'em')     { out += '<em>' + serializeInline(child) + '</em>'; return; }
    if (tag === 'u')                     { out += '<u>' + serializeInline(child) + '</u>'; return; }
    if (tag === 'code')                  { out += '<code>' + serializeInline(child) + '</code>'; return; }
    if (tag === 'cite')                  { out += '<cite>' + serializeInline(child) + '</cite>'; return; }
    if (tag === 'a') {
      const href = esc(child.getAttribute('href') || '#');
      const isNew = child.target === '_blank' || child.dataset?.newtab === '1';
      const innerText = child.textContent.replace(/ \(opens in new tab\)$/, '').trim();
      if (isNew) out += '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + esc(innerText) + ' (opens in new tab)</a>';
      else out += '<a href="' + href + '">' + esc(innerText) + '</a>';
      return;
    }
    if (tag === 'br') { out += '<br>'; return; }
    out += serializeInline(child);
  });
  return out;
}

function collectBlocks(parent) {
  const blocks = [];
  let inlineBuf = document.createDocumentFragment();
  function flushInline() {
    if (inlineBuf.textContent.trim()) {
      const p = document.createElement('p');
      p.appendChild(inlineBuf);
      blocks.push(p);
    }
    inlineBuf = document.createDocumentFragment();
  }
  parent.childNodes.forEach(node => {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.textContent.trim()) inlineBuf.appendChild(node.cloneNode(true));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    if (['div','section','article','header','footer'].includes(tag)) {
      flushInline();
      collectBlocks(node).forEach(b => blocks.push(b));
      return;
    }
    if (['p','h3','h4','h5','ul','ol','blockquote'].includes(tag)) {
      flushInline();
      blocks.push(node);
      return;
    }
    if (tag === 'br') return;
    inlineBuf.appendChild(node.cloneNode(true));
  });
  flushInline();
  return blocks;
}

function rootToLines(rootHTML) {
  const root = document.createElement('div');
  root.innerHTML = rootHTML;
  root.querySelectorAll('*').forEach(el => {
    [...el.attributes].map(a => a.name).forEach(name => {
      const keep = (el.tagName === 'A' && ['href','target','rel'].includes(name)) || name === 'data-newtab';
      if (!keep) el.removeAttribute(name);
    });
  });
  const lines = [];
  collectBlocks(root).forEach(block => {
    if (!block.textContent.trim()) return;
    const tag = block.tagName.toLowerCase();
    if (tag === 'ul' || tag === 'ol') {
      lines.push('<' + tag + '>');
      block.childNodes.forEach(child => {
        if (child.nodeType === Node.ELEMENT_NODE && child.tagName.toLowerCase() === 'li')
          lines.push('<li>' + serializeInline(child) + '</li>');
      });
      lines.push('</' + tag + '>');
    } else if (['p','h3','h4','h5','blockquote'].includes(tag)) {
      const inner = serializeInline(block);
      if (inner.trim()) lines.push('<' + tag + '>' + inner + '</' + tag + '>');
    }
  });
  return lines;
}

function makeRTE(opts) {
  // opts: { initialHTML, placeholder, onChange }
  const wrap = document.createElement('div');
  wrap.className = 'wysiwyg-wrap';
  const tb = document.createElement('div');
  tb.className = 'wysiwyg-toolbar';
  tb.setAttribute('role', 'toolbar');
  tb.setAttribute('aria-label', 'Text formatting');
  const body = document.createElement('div');
  body.className = 'wysiwyg-body';
  body.contentEditable = 'true';
  body.setAttribute('role', 'textbox');
  body.setAttribute('aria-multiline', 'true');
  if (opts.placeholder) {
    body.dataset.placeholder = opts.placeholder;
    body.setAttribute('aria-label', opts.placeholder);
  }
  body.innerHTML = opts.initialHTML || '';
  const src = document.createElement('textarea');
  src.className = 'wysiwyg-source';
  src.spellcheck = false;
  src.setAttribute('aria-label', 'HTML source');
  wrap.appendChild(tb); wrap.appendChild(body); wrap.appendChild(src);

  const rte = {
    root: wrap, body, src,
    sourceMode: false,
    getHTML() {
      if (this.sourceMode) return this.src.value.trim();
      if (!body.innerHTML.trim() || body.innerHTML === '<br>') return '';
      return rootToLines(body.innerHTML).join('\n');
    }
  };

  function tbBtn(html, title, fn, cls) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'tb-btn' + (cls ? ' ' + cls : ''); b.title = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = html;
    b.addEventListener('mousedown', e => { e.preventDefault(); fn(b); });
    b.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(b); } });
    tb.appendChild(b);
    return b;
  }
  function sep() { const d = document.createElement('div'); d.className = 'tb-sep'; tb.appendChild(d); }

  tbBtn(TB_ICONS.bold, 'Bold', () => { document.execCommand('bold'); body.focus(); opts.onChange(); });
  tbBtn(TB_ICONS.italic, 'Italic', () => { document.execCommand('italic'); body.focus(); opts.onChange(); });
  sep();
  tbBtn(TB_ICONS.ul, 'Bullet list', () => { document.execCommand('insertUnorderedList'); body.focus(); opts.onChange(); });
  tbBtn(TB_ICONS.ol, 'Numbered list', () => { document.execCommand('insertOrderedList'); body.focus(); opts.onChange(); });
  sep();
  ['h3','h4','h5'].forEach(level => {
    tbBtn(level.toUpperCase(), 'Heading ' + level, () => {
      const sel = window.getSelection();
      if (!sel.rangeCount) return;
      const anchor = sel.anchorNode;
      const el = anchor?.nodeType === 3 ? anchor.parentElement : anchor;
      document.execCommand('formatBlock', false, el?.closest(level) ? 'p' : level);
      body.focus(); opts.onChange();
    });
  });
  sep();
  tbBtn(TB_ICONS.link, 'Insert or edit link', () => {
    const sel = window.getSelection();
    if (sel.rangeCount) savedRange = sel.getRangeAt(0).cloneRange();
    activeRTE = rte;
    const anchor = sel.anchorNode?.parentElement?.closest('a');
    $('#modal-url').value = anchor ? anchor.href : '';
    $('#modal-text').value = anchor ? anchor.textContent.replace(/ \(opens in new tab\)$/, '') : sel.toString();
    const tgt = (anchor && anchor.target === '_blank') ? 'new' : 'same';
    document.querySelector('input[name="modal-target"][value="' + tgt + '"]').checked = true;
    $('#modal-err').textContent = ''; $('#modal-err').hidden = true;
    $('#link-modal').classList.add('open');
    $('#modal-url').focus();
  });
  tbBtn(TB_ICONS.unlink, 'Remove link', () => { document.execCommand('unlink'); body.focus(); opts.onChange(); });
  sep();
  const srcBtn = tbBtn('&lt;/&gt;', 'Toggle HTML source view', () => {
    rte.sourceMode = !rte.sourceMode;
    if (rte.sourceMode) {
      src.value = rootToLines(body.innerHTML).join('\n');
      wrap.classList.add('source-mode');
      srcBtn.classList.add('html-active');
    } else {
      // Sanitize source back into the visual editor
      const allowed = ['p','h3','h4','h5','ul','ol','li','strong','em','u','code','cite','a','br','blockquote'];
      const tmp = document.createElement('div');
      tmp.innerHTML = src.value;
      tmp.querySelectorAll('*').forEach(el => {
        if (!allowed.includes(el.tagName.toLowerCase())) el.replaceWith(...el.childNodes);
      });
      tmp.querySelectorAll('*').forEach(el => {
        [...el.attributes].map(a => a.name).forEach(n => {
          if (!(el.tagName === 'A' && ['href','target','rel'].includes(n))) el.removeAttribute(n);
        });
      });
      body.innerHTML = tmp.innerHTML;
      wrap.classList.remove('source-mode');
      srcBtn.classList.remove('html-active');
      body.focus();
    }
    opts.onChange();
  }, 'tb-src');

  body.addEventListener('input', opts.onChange);
  src.addEventListener('input', opts.onChange);
  body.addEventListener('paste', e => {
    e.preventDefault();
    document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
  });
  return rte;
}

function closeLinkModal() { $('#link-modal').classList.remove('open'); }
$('#link-modal').addEventListener('click', e => { if (e.target === e.currentTarget) closeLinkModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLinkModal(); });

function insertLinkFromModal() {
  if (!activeRTE) { closeLinkModal(); return; }
  const url = $('#modal-url').value.trim();
  /* Said, in the dialog, rather than a focus move with no word of why
     (2026-10-02: a guard that quietly does nothing). */
  const err = $('#modal-err');
  if (!url) {
    err.textContent = 'Enter the address to link to.'; err.hidden = false;
    $('#modal-url').focus(); return;
  }
  err.textContent = ''; err.hidden = true;
  const text = $('#modal-text').value.trim();
  const isNew = document.querySelector('input[name="modal-target"]:checked').value === 'new';
  activeRTE.body.focus();
  const sel = window.getSelection();
  sel.removeAllRanges();
  if (savedRange) sel.addRange(savedRange);
  const displayText = text || sel.toString().trim() || url;
  const a = document.createElement('a');
  a.href = url;
  if (isNew) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  a.dataset.newtab = isNew ? '1' : '0';
  a.textContent = displayText + (isNew ? ' (opens in new tab)' : '');
  if (savedRange) {
    savedRange.deleteContents();
    savedRange.insertNode(a);
    const r = document.createRange();
    r.setStartAfter(a); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  }
  closeLinkModal();
  scheduleUpdate();
}

// ══════════════════════════════════════════════
//  DRAFT TAB RENDERING
// ══════════════════════════════════════════════
const PH_RE_G = /\x00(\d+)\x00/g;

function renderDraft() {
  if (!CUR) return;
  const doc = $('#tpl-doc');
  doc.innerHTML = '';
  REGIONS = CUR.regions.map(src => ({ src, type: regionType(src), get: () => src }));
  SECTIONS = CUR.sections.map(s => ({ kind: s.kind, label: s.label, comment: s.comment, lines: s.lines,
                                      included: s.kind !== 'optional',
                                      canOverride: (s.kind === 'required' || s.kind === 'conditional'),
                                      override: false, overrideText: null }));

  SECTIONS.forEach(s => {
    const sec = document.createElement('div');
    sec.className = 'tpl-sec kind-' + s.kind + (s.included ? '' : ' off');
    const bodyEl = document.createElement('div');
    bodyEl.className = 'sec-body';
    const renderBody = () => {
      bodyEl.innerHTML = '';
      if (s.override) renderOverrideBody(bodyEl, s);
      else renderGuidedBody(bodyEl, s);
    };

    if (s.kind !== 'plain') {
      const head = document.createElement('div');
      head.className = 'sec-head';
      const badge = document.createElement('span');
      badge.className = 'sec-badge';
      badge.textContent = s.kind;
      const lab = document.createElement('span');
      lab.className = 'sec-label';
      lab.textContent = s.label || '';
      head.appendChild(badge); head.appendChild(lab);

      const controls = document.createElement('div');
      controls.className = 'sec-controls';

      // Include toggle — every marked section (required, conditional, optional)
      const tg = document.createElement('label');
      tg.className = 'sec-toggle';
      tg.title = 'Exclude this section from the HTML output';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = s.included;
      cb.addEventListener('change', () => {
        s.included = cb.checked;
        sec.classList.toggle('off', !s.included);
        scheduleUpdate();
      });
      const t = document.createElement('span');
      t.textContent = 'Include';
      tg.appendChild(cb); tg.appendChild(t);
      controls.appendChild(tg);

      // Override toggle — required & conditional sections only (follows Include)
      if (s.canOverride) {
        const og = document.createElement('label');
        og.className = 'sec-toggle override';
        og.title = 'Make the entire section editable as raw HTML';
        const ocb = document.createElement('input');
        ocb.type = 'checkbox';
        ocb.checked = s.override;
        ocb.addEventListener('change', () => {
          if (ocb.checked) { s.overrideText = sectionBodyText(s); s.override = true; }
          else { s.override = false; }
          renderBody();
          scheduleUpdate();
        });
        const ot = document.createElement('span');
        ot.textContent = 'Override';
        og.appendChild(ocb); og.appendChild(ot);
        controls.appendChild(og);
      }

      head.appendChild(controls);
      sec.appendChild(head);
    }

    renderBody();
    sec.appendChild(bodyEl);
    doc.appendChild(sec);
  });

  $('#draft-card').style.display = '';
  $('#output-card').style.display = '';
  $('#draft-actions').style.display = '';
  scheduleUpdate();
}

// Guided rendering: fixed HTML dimmed, {{regions}} editable in place.
function renderGuidedBody(bodyEl, s) {
  s.lines.forEach(line => {
    const lone = line.trim().match(/^\x00(\d+)\x00$/);
    if (lone) { appendRegionEditor(bodyEl, parseInt(lone[1], 10)); return; }
    if (!line.trim()) {
      const sp = document.createElement('div');
      sp.className = 'tpl-blank';
      bodyEl.appendChild(sp);
      return;
    }
    appendHtmlLine(bodyEl, line);
  });
}

// Override rendering: the whole section as one raw-HTML textarea.
function renderOverrideBody(bodyEl, s) {
  const cap = document.createElement('div');
  cap.className = 'region-cap';
  cap.textContent = 'Override — entire section editable as raw HTML';
  bodyEl.appendChild(cap);
  const ta = document.createElement('textarea');
  ta.className = 'src-only override-src';
  ta.spellcheck = false;
  ta.setAttribute('aria-label', 'Editable HTML for the entire ' + s.kind + ' section');
  ta.value = s.overrideText || '';
  ta.rows = Math.min(28, ta.value.split('\n').length + 1);
  ta.addEventListener('input', () => { s.overrideText = ta.value; scheduleUpdate(); });
  bodyEl.appendChild(ta);
}

// Current body text of a section (fixed HTML + live region values), no comment.
// Seeds the Override textarea so it starts from what the user already sees.
function sectionBodyText(s) {
  const out = [];
  s.lines.forEach(line => {
    const lone = line.trim().match(/^\x00(\d+)\x00$/);
    if (lone) {
      const R = REGIONS[parseInt(lone[1], 10)];
      const v = R.get ? R.get() : R.src;
      if (v && v.trim()) out.push(v.trim());
      else if (R.type === 'free') out.push(R.src.trim());
    } else {
      out.push(line.replace(PH_RE_G, (m, i) => {
        const R = REGIONS[parseInt(i, 10)];
        return R.get ? R.get() : R.src;
      }));
    }
  });
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

function appendHtmlLine(parent, line) {
  const div = document.createElement('div');
  div.className = 'tpl-line';
  const parts = line.split(PH_RE_G);
  // split with a capture group alternates: [text, idx, text, idx, ...]
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) {
      if (parts[i]) {
        const sp = document.createElement('span');
        sp.className = 'code';
        sp.textContent = parts[i];
        div.appendChild(sp);
      }
    } else {
      const ri = parseInt(parts[i], 10);
      const R = REGIONS[ri];
      const sp = document.createElement('span');
      sp.className = 'edit-inline';
      try { sp.contentEditable = 'plaintext-only'; } catch (_) { sp.contentEditable = 'true'; }
      if (!sp.isContentEditable) sp.contentEditable = 'true';
      sp.spellcheck = false;
      sp.setAttribute('role', 'textbox');
      sp.setAttribute('aria-label', 'Editable text');
      sp.textContent = R.src;
      sp.addEventListener('input', scheduleUpdate);
      sp.addEventListener('keydown', e => { if (e.key === 'Enter') e.preventDefault(); });
      sp.addEventListener('paste', e => {
        e.preventDefault();
        document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
      });
      R.get = () => sp.textContent;
      div.appendChild(sp);
    }
  }
  parent.appendChild(div);
}

function appendRegionEditor(parent, ri) {
  const R = REGIONS[ri];
  const holder = document.createElement('div');
  holder.className = 'region-block';
  const cap = document.createElement('div');
  cap.className = 'region-cap';

  if (R.type === 'free') {
    const ph = R.src.trim().replace(/^<!--\s*/, '').replace(/\s*-->$/, '');
    cap.textContent = 'Free text — leave empty to omit';
    holder.appendChild(cap);
    const rte = makeRTE({ initialHTML: '', placeholder: ph, onChange: scheduleUpdate });
    holder.appendChild(rte.root);
    R.get = () => rte.getHTML();
  } else if (visualSafe(R.src)) {
    cap.textContent = 'Editable block';
    holder.appendChild(cap);
    const rte = makeRTE({ initialHTML: R.src, placeholder: '', onChange: scheduleUpdate });
    holder.appendChild(rte.root);
    R.get = () => rte.getHTML();
  } else {
    cap.textContent = 'Editable block (HTML source — contains structural markup)';
    holder.appendChild(cap);
    const ta = document.createElement('textarea');
    ta.className = 'src-only';
    ta.spellcheck = false;
    ta.setAttribute('aria-label', 'Editable HTML block');
    ta.value = R.src.trim();
    ta.rows = Math.min(14, R.src.trim().split('\n').length + 1);
    ta.addEventListener('input', scheduleUpdate);
    holder.appendChild(ta);
    R.get = () => ta.value.trim();
  }
  parent.appendChild(holder);
}

// ══════════════════════════════════════════════
//  OUTPUT ASSEMBLY
// ══════════════════════════════════════════════
function buildOutput() {
  if (!CUR) return '';
  const L = [];
  SECTIONS.forEach(s => {
    if (!s.included) return;
    if (s.kind !== 'plain' && s.comment) L.push(s.comment);
    if (s.override) {
      L.push((s.overrideText || '').replace(/\s+$/, ''));
    } else {
      s.lines.forEach(line => {
        const lone = line.trim().match(/^\x00(\d+)\x00$/);
        if (lone) {
          const R = REGIONS[parseInt(lone[1], 10)];
          const v = R.get();
          if (v && v.trim()) L.push(v.trim());
          else if (R.type === 'free') L.push(R.src.trim()); // keep placeholder comment as a reminder
        } else {
          L.push(line.replace(PH_RE_G, (m, i) => REGIONS[parseInt(i, 10)].get()));
        }
      });
    }
    L.push('');
  });
  return L.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

// Heuristic HTML tag-balance check. Excluding a section that opens a
// container (e.g. a wrapping <div>) without excluding the section that
// closes it leaves the output unbalanced — surfaced as a Draft warning.
const VOID_TAGS = new Set(['area','base','br','col','embed','hr','img','input',
                           'link','meta','param','source','track','wbr']);
function tagBalanceProblems(html) {
  const clean = html.replace(/<!--[\s\S]*?-->/g, '');
  const stack = [];
  const problems = [];
  const re = /<(\/?)([a-zA-Z][\w-]*)\b[^>]*?(\/?)>/g;
  let m;
  while ((m = re.exec(clean))) {
    const closing = m[1] === '/';
    const tag = m[2].toLowerCase();
    const selfClose = m[3] === '/';
    if (!closing && (VOID_TAGS.has(tag) || selfClose)) continue;
    if (!closing) { stack.push(tag); continue; }
    let idx = -1;
    for (let i = stack.length - 1; i >= 0; i--) { if (stack[i] === tag) { idx = i; break; } }
    if (idx === -1) problems.push('unexpected </' + tag + '>');
    else stack.splice(idx, 1);
  }
  stack.forEach(t => problems.push('unclosed <' + t + '>'));
  return [...new Set(problems)];
}

function updateBalanceWarning(raw) {
  const warn = $('#draft-warn');
  if (!warn) return;
  const probs = tagBalanceProblems(raw);
  if (!probs.length) { warn.innerHTML = ''; return; }
  warn.innerHTML = '<div class="notice err">&#9888; Unbalanced HTML: ' +
    esc(probs.join('; ')) + '. Excluding a section that opens a container (such as a wrapping ' +
    '&lt;div&gt;) usually means you must also exclude the section that closes it — or edit a ' +
    'related section with Override. Review your Include choices before pasting into Digital Commons.</div>';
}

function syntaxHighlight(raw) {
  let s = raw.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s = s.replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="hl-cmt">$1</span>');
  s = s.replace(/(&lt;\/?[\w][\w.-]*(?:\s[^&]*?)?\/?\s*&gt;)/g, m => {
    const inner = m.replace(/([\w-]+)=(&quot;[^&]*&quot;)/g,
      '<span class="hl-attr">$1</span>=<span class="hl-val">$2</span>');
    return '<span class="hl-tag">' + inner + '</span>';
  });
  return s;
}

let _timer = null;
function scheduleUpdate() {
  clearTimeout(_timer);
  _timer = setTimeout(() => {
    const raw = buildOutput();
    const pre = $('#output-pre');
    pre.innerHTML = syntaxHighlight(raw);
    pre.dataset.raw = raw;
    updateBalanceWarning(raw);
  }, 90);
}

function stripComments(raw) {
  // Strip full-line comments (the guide markers); keep inline comments
  return raw
    .replace(/^[ \t]*<!--[\s\S]*?-->[ \t]*\r?\n?/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim() + '\n';
}

// Strip EVERY comment, including inline ones (e.g. <!-- Begin parent -->).
function stripAllComments(raw) {
  return raw
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/^[ \t]+$/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim() + '\n';
}

// Final text for copy/download, honoring the "Remove all comments" toggle.
function finalOutput() {
  let clean = stripComments($('#output-pre').dataset.raw || '');
  if ($('#strip-all-comments') && $('#strip-all-comments').checked) clean = stripAllComments(clean);
  return clean;
}

function copyOutput() {
  const clean = finalOutput();
  navigator.clipboard.writeText(clean).then(() => {
    const btn = $('#copy-btn');
    btn.textContent = 'Copied ✓';
    setTimeout(() => btn.textContent = 'Copy', 2200);
  });
}

function downloadOutput() {
  const clean = finalOutput();
  const blob = new Blob([clean], { type: 'text/html' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const slug = s => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const status = document.querySelector('input[name=status]:checked')?.value || '';
  a.download = 'dc-' + slug(status) + '-' + slug($('#sel-structure').value) + '-' + slug(CUR ? CUR.meta.field : 'field') + '.html';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ══════════════════════════════════════════════
//  TEMPLATES TAB
// ══════════════════════════════════════════════
function showTab(which) {
  $('#tab-draft').setAttribute('aria-selected', which === 'draft' ? 'true' : 'false');
  $('#tab-manage').setAttribute('aria-selected', which === 'manage' ? 'true' : 'false');
  $('#pane-draft').style.display = which === 'draft' ? '' : 'none';
  $('#pane-manage').style.display = which === 'manage' ? '' : 'none';
}

// Contextual feedback for the Templates tab. A confirmation fades after a
// few seconds; an ERROR stays on screen until the user clears it with the X,
// so a message the user needs to act on can never scroll past unread. The
// sequence guard stops an earlier confirmation's timer from wiping a later
// error out from under the user.
let _mgrSeq = 0;
function mgrMsg(html, ok) {
  const box = $('#mgr-msg');
  const seq = ++_mgrSeq;
  box.innerHTML = '';
  if (!html) return;
  box.innerHTML = '<div class="notice' + (ok ? '' : ' err') + '">' + html +
    (ok ? '' : '<button type="button" class="msg-x" ' +
                'aria-label="Dismiss this message">&times;</button>') + '</div>';
  if (ok) {
    setTimeout(() => { if (_mgrSeq === seq) box.innerHTML = ''; }, 6000);
  } else {
    box.querySelector('.msg-x')
       .addEventListener('click', () => { box.innerHTML = ''; });
  }
}

function renderList() {
  const holder = $('#tpl-list');
  holder.innerHTML = '';
  if (!TPLS.length) {
    holder.innerHTML = '<div class="empty-msg">No templates in the library. Upload templates or restore factory defaults.</div>';
    return;
  }
  const groupNames = statuses().filter(st => TPLS.some(t => t.meta.status === st));
  if (TPLS.some(t => !statuses().includes(t.meta.status))) groupNames.push('');
  groupNames.forEach(st => {
    const group = st === ''
      ? TPLS.filter(t => !statuses().includes(t.meta.status))
      : TPLS.filter(t => t.meta.status === st);
    if (!group.length) return;
    const h = document.createElement('div');
    h.className = 'tpl-group-title';
    h.textContent = (st || 'Uncategorized') + ' (' + group.length + ')';
    holder.appendChild(h);
    group.sort((a, b) => a.meta.field.localeCompare(b.meta.field)).forEach(t => {
      const row = document.createElement('div');
      row.className = 'tpl-row';
      const f = document.createElement('span');
      f.className = 't-field';
      f.textContent = t.meta.field || '(no field)';
      const ss = document.createElement('span');
      ss.className = 't-structs';
      t.meta.structures.forEach(s => {
        const b = document.createElement('span');
        b.className = 'badge';
        b.textContent = s;
        ss.appendChild(b);
      });
      row.appendChild(f); row.appendChild(ss);
      [['Edit', () => openEditor(t.filename)],
       ['Export', () => exportOne(t)],
       ['Duplicate', () => duplicateTpl(t)],
       ['Delete', () => deleteTpl(t.filename)]].forEach(([txt, fn]) => {
        const b = document.createElement('button');
        b.className = 'row-btn' + (txt === 'Delete' ? ' danger' : '');
        b.textContent = txt;
        b.addEventListener('click', fn);
        row.appendChild(b);
      });
      const file = document.createElement('span');
      file.className = 't-file';
      file.textContent = t.filename;
      row.appendChild(file);
      holder.appendChild(row);
    });
  });
}

function exportOne(t) {
  const blob = new Blob([t.content], { type: 'text/html' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = t.filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function deleteTpl(filename) {
  if (!confirm('Delete template "' + filename + '"? This cannot be undone (unless it is a factory template, which Restore factory defaults will bring back).')) return;
  const r = await fetch('/api/delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ filename }) });
  const j = await r.json();
  mgrMsg(j.ok ? 'Deleted ' + esc(filename) + '.' : esc(j.error || 'Delete failed.'), j.ok);
  loadTemplates(true);
}

async function duplicateTpl(t) {
  const name = prompt('Filename for the copy:', t.filename.replace(/\.html$/, '-copy.html'));
  if (!name) return;
  const r = await fetch('/api/save', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ filename: name, content: t.content }) });
  const j = await r.json();
  mgrMsg(j.ok ? 'Created ' + esc(name) + '.' : esc(j.error || 'Save failed.'), j.ok);
  loadTemplates(true);
}

const NEW_TEMPLATE_STUB = '<!-- DC-TEMPLATE\nstatus: Active\nstructures: Community\nfield: New Field\n-->\n<!-- REQUIRED : Describe the first section here. -->\n<h2>{{Editable heading}}</h2>\n{{<!-- Free text here. -->}}\n';

function newTemplate() { openEditor(null); }

function openEditor(filename) {
  editingFile = filename;
  const t = filename ? TPLS.find(x => x.filename === filename) : null;
  $('#ed-title').textContent = t ? 'Edit template — ' + t.meta.field : 'New template';
  $('#ed-filename').value = t ? t.filename : '';
  $('#ed-body').value = t ? t.content : NEW_TEMPLATE_STUB;
  $('#tpl-editor').style.display = '';
  edCheck();
  if ($('#tpl-editor').scrollIntoView) $('#tpl-editor').scrollIntoView({ behavior: 'smooth' });
}

function closeEditor() {
  $('#tpl-editor').style.display = 'none';
  editingFile = null;
}

function edCheck() {
  const content = $('#ed-body').value;
  const p = parseTemplate(content);
  const rows = [];
  const ok = (msg) => rows.push('<div class="ok">&#x2713; ' + msg + '</div>');
  const warn = (msg) => rows.push('<div class="warn">&#x26A0; ' + msg + '</div>');
  if (/^﻿?\s*<!--\s*DC-TEMPLATE/.test(content)) ok('DC-TEMPLATE header found'); else warn('Missing DC-TEMPLATE header');
  if (p.meta.status) ok('status: ' + esc(p.meta.status)); else warn('No status in header');
  if (p.meta.structures.length) ok('structures: ' + esc(p.meta.structures.join(', '))); else warn('No structures in header');
  if (p.meta.field) ok('field: ' + esc(p.meta.field)); else warn('No field in header');
  if (p.leftover) warn('Unbalanced {{ or }} markers detected');
  const kinds = p.sections.reduce((a, s) => { a[s.kind] = (a[s.kind] || 0) + 1; return a; }, {});
  ok(p.sections.length + ' section(s): ' + (Object.entries(kinds).map(([k, v]) => v + ' ' + k).join(', ') || 'none'));
  const types = p.regions.map(r => regionType(r)).reduce((a, t) => { a[t] = (a[t] || 0) + 1; return a; }, {});
  ok(p.regions.length + ' editable region(s): ' + (Object.entries(types).map(([k, v]) => v + ' ' + k).join(', ') || 'none'));
  $('#ed-check').innerHTML = rows.join('');
}
$('#ed-body').addEventListener('input', () => { clearTimeout(window._edT); window._edT = setTimeout(edCheck, 250); });

async function saveEditor() {
  const filename = $('#ed-filename').value.trim();
  const content = $('#ed-body').value;
  if (!filename) { alert('Enter a filename ending in .html'); return; }
  const payload = { filename, content };
  if (editingFile && editingFile !== filename) payload.rename_from = editingFile;
  const r = await fetch('/api/save', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  const j = await r.json();
  if (j.ok) {
    mgrMsg('Saved ' + esc(filename) + '.', true);
    closeEditor();
    loadTemplates(true);
  } else {
    alert(j.error || 'Save failed.');
  }
}

async function uploadFiles(files) {
  let saved = 0, failed = [];
  for (const f of files) {
    const text = await f.text();
    const r = await fetch('/api/save', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ filename: f.name, content: text }) });
    const j = await r.json();
    if (j.ok) saved++; else failed.push(f.name + ' (' + (j.error || 'error') + ')');
  }
  $('#up-file').value = '';
  mgrMsg('Uploaded ' + saved + ' template(s).' + (failed.length ? ' Failed: ' + esc(failed.join(', ')) : ''), !failed.length);
  loadTemplates(true);
}

async function uploadZip(file) {
  if (!file) return;
  const buf = await file.arrayBuffer();
  let bin = '';
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  const b64 = btoa(bin);
  const r = await fetch('/api/import-zip', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ data: b64 }) });
  const j = await r.json();
  $('#up-zip').value = '';
  mgrMsg(j.ok ? 'Imported ' + j.count + ' template(s) from zip.' : esc(j.error || 'Import failed.'), j.ok);
  loadTemplates(true);
}

async function resetDefaults() {
  if (!confirm('Replace ALL templates in config/templates/ with this install\'s factory set (config/templates.factory/)? Custom templates will be deleted.')) return;
  const r = await fetch('/api/reset', { method: 'POST' });
  const j = await r.json();
  mgrMsg(j.ok ? 'Restored ' + j.count + ' factory template(s).' : esc(j.error || 'Reset failed.'), j.ok);
  loadTemplates(false);
}

// ══════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════
loadTemplates(false);
</script>
</body></html>"""


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
        "__NEUTRAL__": colors.get("neutral", FALLBACK_BRAND["neutral"]),
        "__ONPRIMARY__": contrast_text(primary),
        "__HUB__": session.get("hub_url", "#"),
        "__BASE_URL__": session.get("base_url", ""),
    }.items():
        page = page.replace(token, value)
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# 5. HTTP server (JSON over HTTP, per suite conventions)
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


def make_handler(page_bytes: bytes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body: bytes, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self._send(code, "application/json; charset=utf-8",
                       json.dumps(obj).encode("utf-8"))

        def read_json(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self):
            if not request_allowed(self):
                return self.send_json({"error": "Forbidden (non-local request)."}, 403)
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page_bytes)
            elif self.path == "/api/templates":
                self.send_json({"templates": list_templates()})
            elif self.path == "/api/export-zip":
                self._send(200, "application/zip", export_zip_bytes(),
                           {"Content-Disposition":
                            "attachment; filename=dc-templates.zip"})
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self):
            if not request_allowed(self):
                return self.send_json({"error": "Forbidden (non-local request)."}, 403)
            body = self.read_json()
            try:
                if self.path == "/api/save":
                    save_template(body.get("filename", ""),
                                  body.get("content", ""),
                                  body.get("rename_from", ""))
                    self.send_json({"ok": True})
                elif self.path == "/api/delete":
                    delete_template(body.get("filename", ""))
                    self.send_json({"ok": True})
                elif self.path == "/api/import-zip":
                    data = base64.b64decode(body.get("data", ""))
                    self.send_json({"ok": True, "count": import_zip_bytes(data)})
                elif self.path == "/api/reset":
                    self.send_json({"ok": True, "count": ensure_seed(force=True)})
                else:
                    self.send_json({"ok": False, "error": "Unknown endpoint"}, 404)
            except (ValueError, OSError, RuntimeError, zipfile.BadZipFile,
                    base64.binascii.Error) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)

    return Handler


def main() -> None:
    global TEMPLATES_DIR
    parser = argparse.ArgumentParser(description=MANIFEST["name"])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--session", default=str(DEFAULT_SESSION))
    args = parser.parse_args()

    session = load_session(Path(args.session))
    TEMPLATES_DIR = Path(args.session).resolve().parent / "templates"
    ensure_seed()
    port = args.port or free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 make_handler(build_page(session)))
    url = "http://127.0.0.1:{}".format(port)
    print("{} running at {}".format(MANIFEST["name"], url))
    print("Template library: {}".format(TEMPLATES_DIR))
    if args.port is None:  # standalone launch → open a browser ourselves
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
