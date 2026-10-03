"""
Settings — persistent suite configuration.

Settings are stored INSIDE the suite directory (config/settings.json) so the
whole suite stays transportable: copy the folder to another machine and your
base URL, branding, and preferences travel with it.

Files in config/:
  settings.json      — persistent settings (survive restarts)
  session.json       — the per-session context handed to modules at launch
                       time (base URL, Chrome debugger address, branding,
                       and the PATH to the AI endpoint registry). Regenerated
                       every time a module is launched so modules always
                       start with the latest settings.
  ai_endpoints.json  — the suite-level AI endpoint registry (see
                       app/ai_endpoints.py). Deliberately separate from
                       settings.json: that file is transportable and is what
                       the generic distribution build ships, and API keys
                       must not travel with it.
"""

import json
import copy
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # dc_admin_suite/
CONFIG_DIR = ROOT / "config"
BRANDING_DIR = CONFIG_DIR / "branding"
MODULES_DIR = ROOT / "modules"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
SESSION_FILE = CONFIG_DIR / "session.json"
# The suite-level AI endpoint registry (v1.3). Kept OUT of settings.json
# because that file is transportable and is what the generic distribution
# build ships — an API key must never travel with it. See app/ai_endpoints.py.
AI_ENDPOINTS_FILE = CONFIG_DIR / "ai_endpoints.json"

# ---------------------------------------------------------------------------
# The institution PROFILE (identity refactor Step 3).
#
# base_url, crossref_prefix and branding are what makes this install "ours".
# They used to be typed straight into DEFAULT_SETTINGS below, which meant a
# first launch with no settings.json — or one click of Reset — put the
# first institution's name, URL and colors on somebody else's install. They are now
# read from one file, resolved in this order:
#
#   1. config/profile.json          this install's own (gitignored)
#   2. config.example/profile.json  the shipped, deliberately NEUTRAL example
#   3. NEUTRAL_PROFILE below        last-resort skeleton, so a missing or
#                                   corrupt file can never crash the shell
#
# To set an institution up, drop ONE file at config/profile.json — see
# profiles/README.md for how to write one. The profile is read
# at import, so restart the shell after editing it.
#
# NOTE the placeholder palette is nobody's brand: it exists so an unbranded
# install still renders legibly at WCAG AA, not to represent an institution.
# ---------------------------------------------------------------------------
PROFILE_FILE = CONFIG_DIR / "profile.json"
EXAMPLE_PROFILE_FILE = ROOT / "config.example" / "profile.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` onto `base` (returns a new dict).
    Defined up here because the profile below is merged at import time."""
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out

NEUTRAL_PROFILE = {
    "base_url": "",
    "crossref_prefix": "",
    "branding": {
        "suite_name": "DC Admin Suite",
        "institution": "",
        "logo": "",
        "colors": {"primary": "#1F4E79", "accent": "#946B2D",
                   "neutral": "#9AA5AD"},
        # What this institution's arrangement says about scripted
        # downloading, in its own words, shown by the modules that download.
        # Empty here: a statement names an institution and a vendor, so it
        # belongs in that institution's profile and nowhere in source.
        "download_statement": "",
    },
}


def load_profile() -> dict:
    """The active institution profile, merged over NEUTRAL_PROFILE so every
    key is always present and correctly shaped. An unreadable or malformed
    file is ignored rather than fatal — the shell must always start."""
    for path in (PROFILE_FILE, EXAMPLE_PROFILE_FILE):
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _deep_merge(NEUTRAL_PROFILE, data)
        except (json.JSONDecodeError, OSError):
            continue
    return copy.deepcopy(NEUTRAL_PROFILE)


_PROFILE = load_profile()

DEFAULT_SETTINGS = {
    "base_url": _PROFILE["base_url"],
    # Crossref DOI prefix for this institution's registered account. A
    # shell-level identifier so any module that mints or deposits DOIs
    # (dc_doi_xml, future reporting tools) reads one authoritative value
    # from session.json instead of hard-coding it. Comes from the profile;
    # empty until an institution sets one, and dc_doi_xml refuses to mint
    # without a valid prefix rather than inventing one.
    "crossref_prefix": _PROFILE["crossref_prefix"],
    # Crossref DEPOSIT IDENTITY — who a DOI deposit is credited to. Empty by
    # design (identity refactor Step 2): these name a specific institution,
    # so shipping a default would credit another institution's deposits to
    # whoever's name was compiled in. dc_doi_xml refuses to build a Crossref
    # deposit until they are filled in here (or, for an existing install,
    # present in its own config/journals.json) — an empty required field is
    # a much better failure than a silent wrong one.
    "crossref": {
        "depositor_name": "",     # <depositor_name> on the deposit head
        "depositor_email": "",    # <email_address>; Crossref mails results here
        "registrant": "",         # <registrant>: the organization registering
        "publisher": "",          # <publisher_name>; also DOAJ <publisher>
    },
    "chrome": {
        "host": "127.0.0.1",
        "port": 9222,
    },
    "hub_port": 8750,          # shell UI port; modules get hub_port+1, +2, ...
    # Main Menu display order: a list of module ids in the order they should
    # appear. Any installed module whose id is NOT in this list sorts AFTER
    # the listed ones (alphabetically by name), so adding a module on the fly
    # never disturbs a saved order — it simply lands at the end. Ids of
    # modules that are removed are kept here harmlessly (they match nothing);
    # if the module is restored later it returns to its saved slot.
    "module_order": [],
    # Suite name, institution name, logo filename ("" = none) and the three
    # colors the whole UI is painted from — all from the profile, so
    # "Reset to default branding" restores THIS institution's look, not the
    # one whose values happened to be typed into this file.
    "branding": _PROFILE["branding"],
}


# A Crossref DOI prefix is "10." followed by the account's numeric id.
CROSSREF_PREFIX_RE = re.compile(r"^10\.\d{3,9}$")

# A usable instance base URL: http(s) scheme + a dotted host, no spaces.
BASE_URL_RE = re.compile(r"^https?://[^\s/]+\.[^\s/]+", re.IGNORECASE)


def validate_base_url(value: str) -> str:
    """Return "" if value looks like a usable instance URL, else a message."""
    v = str(value or "").strip()
    if not BASE_URL_RE.match(v):
        return ("Enter a full URL starting with https:// — for example "
                "https://digitalcommons.example.edu")
    return ""


def normalize_crossref_prefix(value: str) -> str:
    """Trim and drop a leading 'doi:' or trailing slash a user might paste."""
    p = str(value or "").strip()
    if p.lower().startswith("doi:"):
        p = p[4:].strip()
    return p.rstrip("/")


def validate_crossref_prefix(value: str) -> str:
    """Return "" if value is a valid Crossref prefix, else a user-facing msg."""
    p = normalize_crossref_prefix(value)
    if not CROSSREF_PREFIX_RE.match(p):
        return ("Enter a Crossref DOI prefix like 10.12345 (\"10.\" followed "
                "by your account's digits).")
    return ""


# The Crossref deposit identity fields, in the order the UI shows them.
CROSSREF_FIELDS = ("depositor_name", "depositor_email", "registrant",
                   "publisher")

# Deliberately permissive: enough to catch a typo, not enough to reject a
# valid-but-unusual address.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_crossref_identity(block) -> dict:
    """Trim the four Crossref identity fields; unknown keys are dropped and
    missing ones become "". Always returns all four keys."""
    src = block if isinstance(block, dict) else {}
    return {f: str(src.get(f, "") or "").strip() for f in CROSSREF_FIELDS}


def validate_crossref_identity(block) -> str:
    """Return "" if the identity block is saveable, else a user-facing msg.

    EMPTY IS ALLOWED here — an install that has not set up DOI depositing yet
    must still be able to save its other settings. What is not allowed is a
    malformed address, which would fail at Crossref rather than here. The
    "you must fill these in" rule belongs to the deposit itself and lives in
    dc_doi_xml, which refuses to build one until they are present.
    """
    ident = normalize_crossref_identity(block)
    email = ident["depositor_email"]
    if email and not EMAIL_RE.match(email):
        return ("Enter the depositor email as a plain address — for example "
                "repository@example.edu. Crossref sends deposit results to it.")
    return ""


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    MODULES_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    """Load settings, merging saved values over defaults (so new keys added
    in future versions of the suite pick up sane defaults automatically)."""
    ensure_dirs()
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return _deep_merge(DEFAULT_SETTINGS, saved)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt file → fall back to defaults (never crash the shell)
    return copy.deepcopy(DEFAULT_SETTINGS)


def _overrides(current: dict, defaults: dict) -> dict:
    """The part of `current` that actually differs from `defaults`."""
    out = {}
    for key, val in current.items():
        base = defaults.get(key)
        if isinstance(val, dict) and isinstance(base, dict):
            sub = _overrides(val, base)
            if sub:
                out[key] = sub
        elif key not in defaults or val != base:
            out[key] = copy.deepcopy(val)
    return out


def save_settings(settings: dict) -> dict:
    """Persist settings and return the full merged result.

    Only what DIFFERS from the profile-backed defaults is written, so
    settings.json is this install's overrides rather than a frozen copy of
    every default. That is what makes a profile meaningful after the fact:
    write the whole blob instead and the first save — even just launching
    with --port — would pin every value forever, so dropping in
    config/profile.json later would silently do nothing. Values a user did
    set are still theirs and still win; see load_settings.
    """
    ensure_dirs()
    merged = _deep_merge(DEFAULT_SETTINGS, settings)
    SETTINGS_FILE.write_text(
        json.dumps(_overrides(merged, DEFAULT_SETTINGS), indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return merged


def update_settings(patch: dict) -> dict:
    """Apply a partial update (deep merge) and persist. Returns new settings."""
    current = load_settings()
    return save_settings(_deep_merge(current, patch))


def get_module_order() -> list:
    """The saved Main Menu display order (list of module ids)."""
    order = load_settings().get("module_order", [])
    return [str(x) for x in order] if isinstance(order, list) else []


def set_module_order(order) -> dict:
    """
    Persist the Main Menu display order.

    `order` is the desired sequence of the CURRENTLY PRESENT module ids. Any
    ids already saved but not in `order` (e.g. a module that is temporarily
    removed) are preserved at the end, so a removed-then-restored module keeps
    a remembered position instead of being forgotten. Duplicates are dropped,
    first occurrence wins. Returns the merged settings.
    """
    if not isinstance(order, (list, tuple)):
        order = []
    prior = get_module_order()
    clean, seen = [], set()
    for mid in list(order) + prior:      # posted order first, stale ids after
        mid = str(mid).strip()
        if mid and mid not in seen:
            clean.append(mid)
            seen.add(mid)
    return update_settings({"module_order": clean})


def order_modules(modules: list) -> list:
    """
    Return `modules` sorted for display: ids present in module_order first, in
    that order; everything else after, alphabetically by name. Stable and
    non-destructive — a new module (absent from the saved order) always lands
    at the end without shifting the saved sequence. `modules` are dicts with
    at least "id" and "name" keys (as produced by module_registry.scan_modules).
    """
    order = get_module_order()
    rank = {mid: i for i, mid in enumerate(order)}
    tail = len(order)
    return sorted(
        modules,
        key=lambda m: (rank.get(m.get("id", ""), tail),
                       str(m.get("name", "")).lower()),
    )


def write_session_context(settings: dict, extra: dict = None) -> Path:
    """
    Write config/session.json — the context handed to modules so they can run
    standalone after launch: Chrome debugger address, base URL, branding.
    """
    ensure_dirs()
    ctx = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "base_url": settings["base_url"].rstrip("/"),
        "crossref_prefix": normalize_crossref_prefix(
            settings.get("crossref_prefix",
                         DEFAULT_SETTINGS["crossref_prefix"])),
        # Who deposits are credited to. Like crossref_prefix, this is a
        # shell-level value more than one module could depend on, so it is
        # resolved here once and handed to modules rather than being asked
        # for again per module. Empty fields are passed through as empty —
        # the module decides what it cannot do without them.
        "crossref": normalize_crossref_identity(settings.get("crossref", {})),
        "chrome": {
            "host": settings["chrome"]["host"],
            "port": settings["chrome"]["port"],
            "debugger_address": "{}:{}".format(
                settings["chrome"]["host"], settings["chrome"]["port"]
            ),
        },
        "branding": settings["branding"],
        "hub_url": "http://127.0.0.1:{}".format(settings["hub_port"]),
        # The PATH to the AI endpoint registry, never its contents: keys are
        # not copied into a file that is rewritten at every module launch.
        # Modules read that file per request, so a key added or rotated in
        # Settings reaches an already-running module immediately.
        "ai_endpoints_path": str(AI_ENDPOINTS_FILE),
    }
    if extra:
        ctx.update(extra)
    SESSION_FILE.write_text(
        json.dumps(ctx, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return SESSION_FILE
