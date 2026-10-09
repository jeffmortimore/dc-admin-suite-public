"""Build a shareable copy of the suite.

    python3 docs/make_distribution.py --staff     ← for colleagues here
    python3 docs/make_distribution.py --generic   ← for another institution

WHY THIS EXISTS
---------------
`config/` travels with the suite by design — description profiles, field
templates, the journal registry and the AI endpoint list are all meant to be
copied from machine to machine. Two of those files hold API keys in plain
text, which the UI says plainly and which is fine while the folder stays on
one person's computer.

The moment the folder is zipped and sent, it stops being fine. Copying a
suite folder is the normal way to share it, so the normal way to share it
also ships whatever keys are in it. This script is the safe path: it copies
the suite, strips every secret and every machine-specific path, and leaves
the reference data that makes the copy useful.

It never modifies the source folder.

WHAT GETS COPIED: THE REPO, NOT THE PACKAGE
-------------------------------------------
The unit of distribution is the repository, not `dc_admin_suite/`. A copy
that holds only the package is not something anyone can receive: it has no
license, so nobody may legally use it; no `.gitignore`, so the first person
to commit puts their API keys in history permanently; and no CI, so nothing
re-runs the release checklist — which is precisely how the old fork drifted
for six weeks unnoticed.

So this script builds from the repository root and reproduces its shape:

    <out>/
      LICENSE
      .gitignore
      .github/workflows/ci.yml
      dc_admin_suite/            ← launch.py, app/, modules/, config.example/,
                                   profiles/, docs/

`ci.yml` declares `working-directory: dc_admin_suite`, so this layout is not
cosmetic — flattening the package into the output root would break CI in the
built copy while leaving it green here. The three root files are REQUIRED:
their absence fails the final check rather than passing quietly, so the fix
cannot silently regress.

Until 2026-09-07 the build root was `dc_admin_suite/` and none of those three
files reached a copy at all.

WHAT IS STRIPPED, AND WHY
-------------------------
  secret values       Any api_key / token / secret / password field,
                      anywhere in any JSON file in the copy — matched by
                      field name, not by a config's expected shape, and not
                      only under `config/`. A key is personal and metered;
                      each person adds their own under Settings > AI
                      endpoints.
  config/session.json Rewritten at every launch and full of absolute paths
                      from THIS machine. A stale one is worse than none.
  config/forms/       Each page's remembered settings (1.0.1): this
                      machine's folders and choices, not a recipient's.
  __pycache__, .git   Byte-code for one Python version on one platform, and
                      this machine's history and remotes.
  _backup_*, _removed Working folders, not part of the product.
  DC_*.xlsx, *.log    Run artifacts that may name real records.

`.github/` is NOT skipped. It is a dot-directory like `.git`, but it is
product: the skip list matches names exactly for that reason.

--generic additionally REMOVES dc_admin_suite/config/ in full and any named
institution profile, so the copy carries none of this institution's working
data. The suite rebuilds config/ on first launch from config.example/ and the
neutral profile, which is what a new institution should start from anyway.

  config/          settings, profile, field templates, journal registry,
                   AI endpoints, logo — all of it this institution's.
  profiles/*.json  named setup files; profiles/README.md stays so a
                   recipient can write their own.

THE IDENTITY CHECK (why --generic can now be trusted)
-----------------------------------------------------
A --generic build FAILS, loudly and with a non-zero exit, if any file in
the copy still names this institution — the same guarantee the script
already gave for API keys. Since 2026-10-02 that means EVERY file: a
generic copy is the public release, its `docs/` is replaced by the public
set (README.md at the root, INSTALL.md, RELEASE-NOTES.md, and the two
tools), and the internal CHANGELOG and project brief do not ship, so no
exemption is needed. The match survives line wrapping and case.

A second scan runs beside it: the VENDOR-AND-LICENCE scan, which fails
the build if any file contains a term the institution's profile declares
private (`private_terms`): typically a vendor's name, licence vocabulary,
or the names of one office's folders and disks. In the public copy, ci.yml's last step builds --staff,
because a copy with no named profile has nothing for --generic to check.

What counts as this institution is read from `profiles/`, not compiled in:
derived from the settings there (host, Crossref prefix, brand colors) and
declared in each profile's `identity_markers` for the names and house
conventions nothing else spells out. An institution arms the gate by
writing its profile. No named profile at all is a FAILURE, not a pass — a
scan with nothing to search for cannot call a copy clean.

This replaced a list of settings keys to blank. Blanking a list only ever
resets what someone remembered to enumerate, and it kept missing things:
field templates (identity refactor Step 1), the Crossref deposit identity
(Step 2), the 16-journal registry (Step 4). Removing the directory cannot
go stale.

WRITING INTO A FOLDER THAT ALREADY EXISTS
-----------------------------------------
The distribution copy has a permanent home that is rebuilt every release, so
refusing every existing destination made the documented workflow impossible
to follow — and a workflow nobody can follow is one nobody runs.

  destination absent or empty   built into, no flag needed
  destination has files         refused, unless --replace
  --replace                     clears the destination first, KEEPING .git/

Keeping `.git/` is the point of the flag: the destination is a checkout with
its own history and remote, and a rebuild that deleted it would orphan the
repository on every release. Nothing else survives — a build is the whole
contents of the copy, not a merge into whatever was there.

A destination inside the source tree, or containing it, is refused outright:
the first makes the copy recurse into itself, and the second would delete
the source.

HISTORY: WHY THIS USED TO BE UNSAFE
-----------------------------------
Until 2026-09-04 institutional identity was compiled into ~139 places
across 15 .py files, so --generic could not produce a clean copy at all and
the open-access release was maintained as a hand-edited fork instead. Steps
1-3 of the identity refactor moved that identity into config/ and into one
profile file; Step 4 made the build strip it and prove it. The fork is
no longer needed — see "Distribution vs production" in the project brief.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

# The build root is the REPOSITORY, not the package. `docs/` sits inside
# `dc_admin_suite/`, which sits inside the repo — so two parents up.
SUITE = Path(__file__).resolve().parent.parent   # .../dc_admin_suite
REPO = SUITE.parent                              # the repository root
SUITE_DIR = SUITE.name                           # "dc_admin_suite"

# Files that make the copy a repository rather than a folder of Python, each
# with what its absence costs a recipient. A build missing any of them is not
# shippable, so this is a failure of the final check and not a warning: it is
# what keeps the 2026-09-07 fix from regressing unnoticed the next time the
# layout moves.
REQUIRED_ROOT_FILES = (
    ("LICENSE", "nobody may legally use the copy"),
    (".gitignore", "the first commit puts config/ and its API keys in "
                   "history permanently"),
    (".github/workflows/ci.yml", "nothing re-runs the release checklist — "
                                 "which is how the old fork drifted"),
)

# Directory names that never belong in a distributed copy. Matched EXACTLY,
# which is what keeps `.github/` (product) while dropping `.git/` (this
# machine's history and remotes).
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "_removed"}
SKIP_DIR_PREFIXES = ("_backup", "_to_delete", "DC_ImageDescriptions_",
                     "DC_OCR_Output_", "DC_FileDownloads_")
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".log", ".DS_Store")
SKIP_FILE_PREFIXES = ("DC_",)          # run reports of every module

# Everything a generic copy must NOT carry. config/ is this institution's
# working data in full — settings, profile, field templates, journal
# registry, AI endpoints, logo — so a generic build simply does not ship it.
# The suite recreates config/ on first launch from config.example/ and the
# neutral profile, which is exactly what a new institution should start from.
#
# These are paths INSIDE the package, joined onto the copy's SUITE_DIR. They
# were bare top-level names while the build root was the package itself; a
# bare "config" now points at a repo-root folder that does not exist, which
# would have made --generic ship this institution's configuration in a copy
# the caller believed was clean.
#
# This replaced a list of keys to blank inside config/settings.json. Blanking
# a list can only ever reset the things someone remembered to enumerate, and
# it kept missing: templates in Step 1, the deposit identity in Step 2, the
# journal registry after that. Removing the directory cannot go stale.
GENERIC_DROP_DIRS = ("config",)

# Named institution profiles are opt-in setup files, not defaults — but a
# copy going to another institution has no use for them, and shipping one
# invites loading it by mistake. profiles/README.md stays: it explains what
# a profile is and how to write your own.
GENERIC_PROFILES_DIR = "profiles"
GENERIC_KEEP_IN_PROFILES = ("README.md",)


def _skip(path: Path) -> bool:
    name = path.name
    if path.is_dir():
        if name == "forms" and path.parent.name == "config":
            return True
        return (name in SKIP_DIRS
                or name.startswith(SKIP_DIR_PREFIXES))
    if name.endswith(SKIP_FILE_SUFFIXES):
        return True
    if name.startswith(SKIP_FILE_PREFIXES) and name.endswith(
            (".xlsx", ".xml", ".txt")):
        return True
    return name == "session.json" and path.parent.name == "config"


def _copy(src: Path, dst: Path, report: list) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if _skip(item):
            report.append("skipped   {}".format(item.relative_to(REPO)))
            continue
        if item.is_dir():
            _copy(item, dst / item.name, report)
        else:
            shutil.copy2(item, dst / item.name)


# Field names that hold a secret, wherever they appear in a JSON file.
SECRET_FIELDS = ("api_key", "apikey", "api_token", "token", "secret",
                 "password", "access_key", "client_secret")


def _walk_secrets(node, blank: bool) -> int:
    """Count (and optionally blank) every secret-bearing field in a JSON tree.

    Deliberately recursive and name-based rather than shape-based. The old
    version only looked at `data["endpoints"][*]["api_key"]`, which was true
    of every config file that existed when it was written — and would have
    silently shipped the key of the first module to store one anywhere else.
    """
    found = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if (key.lower() in SECRET_FIELDS
                    and isinstance(value, str) and value.strip()):
                found += 1
                if blank:
                    node[key] = ""
            else:
                found += _walk_secrets(value, blank)
    elif isinstance(node, list):
        for item in node:
            found += _walk_secrets(item, blank)
    return found


def _load_json(path: Path):
    """Parse a JSON file, or return None if it can't be read or parsed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _strip_keys(folder: Path, report: list) -> int:
    """Blank every secret in the copy. Returns how many were cleared.

    Scans every JSON file in the copy, not only `config/`. It used to scan
    `config/` alone while the final check looked everywhere — so a key
    stored outside config would be found by the check and never cleared,
    failing the build with no way to pass it. Strip and verify now cover the
    same ground, which is the only arrangement in which a clean run means
    anything.
    """
    cleared = 0
    for path in sorted(folder.rglob("*.json")):
        data = _load_json(path)
        if data is None:
            report.append("WARNING: could not read {} — check it by hand"
                          .format(path.relative_to(folder)))
            continue
        found = _walk_secrets(data, blank=True)
        if not found:
            continue
        cleared += found
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        report.append("cleared {} secret(s) in {}".format(
            found, path.relative_to(folder)))
    return cleared


# The public docs (2026-10-02). A generic copy ships its own README at the
# repository root, where GitHub shows it, an install guide and release notes
# — and NOT the internal CHANGELOG or the project brief, which are the
# working record of one institution's install. Published path in the copy
# -> source path in this repository.
PUBLIC_DOCS = {
    "README.md": SUITE_DIR + "/docs/public/README.md",
    SUITE_DIR + "/docs/INSTALL.md": SUITE_DIR + "/docs/public/INSTALL.md",
    SUITE_DIR + "/docs/RELEASE-NOTES.md":
        SUITE_DIR + "/docs/public/RELEASE-NOTES.md",
}
# What stays in docs/ beside them: the two tools a recipient runs.
PUBLIC_DOCS_KEEP = ("check_docs.py", "make_distribution.py")

# CI in the public copy. Its last step builds --generic from the copy, and
# a generic copy has no named profile, so that step would fail by design
# ("a scan with nothing to search for cannot call a copy clean"). In the
# public repository it builds --staff instead, which still proves no key,
# session file or cache can ship. An institution that adds its own profile
# can put the generic step back.
CI_GENERIC_STEP = re.compile(
    r"      - name: A generic build is still clean\n"
    r"(?:        #[^\n]*\n)*"
    r"        run: python docs/make_distribution\.py --generic [^\n]*\n")
CI_PUBLIC_STEP = (
    '      - name: A shareable build is still clean\n'
    '        # In the public copy: no named profile, so --staff, which still\n'
    '        # fails on any API key, session file or cache in the copy.\n'
    '        run: python docs/make_distribution.py --staff --out '
    '"${{ runner.temp }}/staff-${{ matrix.python-version }}"\n')


def _public_docs(folder: Path, report: list) -> None:
    """Replace docs/ with the public set and adapt CI. Refuses rather
    than half-doing it: a public copy with the internal record in it is
    the one thing this exists to prevent."""
    docs = folder / SUITE_DIR / "docs"
    for item in sorted(docs.iterdir()):
        if item.name in PUBLIC_DOCS_KEEP:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    for dest, src in PUBLIC_DOCS.items():
        source = REPO / src
        if not source.is_file():
            raise SystemExit("FAILED: the public document {} is missing — "
                             "DO NOT SEND THIS COPY.".format(src))
        shutil.copy2(source, folder / dest)
    report.append("replaced {}/docs/ with the public set ({}) and wrote "
                  "README.md at the root; the CHANGELOG and the project "
                  "brief do not ship".format(SUITE_DIR, ", ".join(
                      sorted(Path(d).name for d in PUBLIC_DOCS))))
    ci = folder / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    text, n = CI_GENERIC_STEP.subn(lambda m: CI_PUBLIC_STEP, text)
    if n != 1:
        raise SystemExit("FAILED: ci.yml's generic-build step was not found "
                         "exactly once, so the public copy's CI would fail "
                         "on its own build. DO NOT SEND THIS COPY.")
    ci.write_text(text, encoding="utf-8")
    report.append("ci.yml: the last step builds --staff, because a public "
                  "copy has no named profile for --generic to check against")


def _make_generic(folder: Path, report: list) -> None:
    """Strip the copy down to what any institution can use."""
    _public_docs(folder, report)
    suite = folder / SUITE_DIR
    for name in GENERIC_DROP_DIRS:
        target = suite / name
        if target.is_dir():
            # Not ignore_errors=True: a drop that silently fails leaves this
            # institution's working data in a copy the caller believes is
            # generic, and only the identity check would catch it. Fail here
            # instead, in the same voice as every other refusal.
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise SystemExit(
                    "FAILED to remove {}/{}/ — {}\n"
                    "DO NOT SEND THIS COPY: it still contains this "
                    "institution's configuration.".format(
                        SUITE_DIR, name, exc))
            if target.exists():
                raise SystemExit(
                    "FAILED to remove {}/{}/ — the directory is still there "
                    "after rmtree reported success.\n"
                    "DO NOT SEND THIS COPY: it still contains this "
                    "institution's configuration.".format(SUITE_DIR, name))
            report.append("removed {}/{}/ in full — settings, profile, field "
                          "templates, journal registry, endpoints and logo. "
                          "The suite rebuilds it on first launch from "
                          "config.example/ and the neutral profile."
                          .format(SUITE_DIR, name))
    profiles = suite / GENERIC_PROFILES_DIR
    if profiles.is_dir():
        dropped = []
        for item in sorted(profiles.iterdir()):
            if item.is_file() and item.name not in GENERIC_KEEP_IN_PROFILES:
                item.unlink()
                dropped.append(item.name)
        if dropped:
            report.append("removed named institution profile(s): "
                          + ", ".join(dropped))


# Markers of THIS institution, read from its profile rather than compiled
# in. Until 2026-09-21 this was a hand-maintained tuple, and it had the two
# faults a hand-maintained list always has.
#
# It went stale by construction. Three of its eight entries — the Crossref
# prefix and the three brand colors — were COPIES of values that already
# live in `profiles/`, so changing the setting left the gate guarding the
# old one.
#
# And it forced a third copy. `_verify_file_downloader.py` searches the
# module for the institution's name, so it had to contain that name — and
# a --generic build then failed on the very test that exists to protect
# it. The gate and the check it serves were in direct conflict for two
# weeks (open item 8). Both now call this function, so there is one list
# and it cannot drift from itself.
#
# Two kinds, from one place:
#
#   DERIVED from the profile's own configuration — the instance host, the
#   Crossref prefix, the brand colors. These cannot go stale, because the
#   setting IS the marker.
#
#   DECLARED in the profile's `identity_markers`: the names, and the house
#   conventions that are identity without being proper nouns. One
#   institution's element id for nesting a structure's introduction — a
#   house standard; no part of Digital Commons requires it — shipped in an
#   example template for another institution to follow. It was found by eye, which
#   is the argument for declaring conventions and not just proper nouns.
#   (`<magic ignore>` is deliberately absent: that IS the platform.)
#
# Deliberately NOT derived: the short institution token, e.g. the
# distinctive label inside the host. Splitting a host into "generic" and
# "identifying" labels produces markers like "state" or "example", and a
# gate that fails a build because a file contains the word "state" is a
# gate somebody switches off. Judgment goes in the declared list, where a
# person can see it.
def identity_markers(repo=None):
    """Every string that names THIS institution, from its profile(s).

    Returns a sorted tuple, or () when no named profile is present — which
    is exactly what a generic copy looks like. **() means "cannot check",
    never "nothing to find"**, and every caller has to say which it means.
    """
    root = Path(repo) if repo else REPO
    folder = root / SUITE_DIR / "profiles"
    found = set()
    if not folder.is_dir():
        return ()
    for item in sorted(folder.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An unreadable profile is not an absence of identity. Say so
            # rather than quietly returning a shorter list.
            raise SystemExit(
                "cannot read {} — refusing to guess what names this "
                "institution".format(item))
        if not isinstance(data, dict):
            continue
        for declared in data.get("identity_markers") or ():
            if str(declared).strip():
                found.add(str(declared).strip())
        for key in ("crossref_prefix", "prefix"):
            val = str(data.get(key) or "").strip()
            if val:
                found.add(val)
        base = str(data.get("base_url") or "").strip()
        if base:
            host = urlparse(base).netloc
            if host:
                found.add(host)
        colors = ((data.get("branding") or {}).get("colors") or {})
        for val in colors.values():
            val = str(val or "").strip()
            if val:
                found.add(val)
                found.add(val.lstrip("#"))
    return tuple(sorted(found))

# Where naming the institution is legitimate: the project's own history and
# authorship. A recipient reading the changelog SHOULD see who wrote this and
# why; what they must not receive is identity that configures their install.
#
# These name directories INSIDE the package. Repo-root files are NOT listed:
# LICENSE, .gitignore and ci.yml are scanned like source, because they are
# new surface and there is no reason for an institution's name to be in them.
#
# `claude` was removed on 2026-09-07: no such directory exists in the repo,
# so the entry exempted nothing while standing ready to exempt anything
# later dropped there. An exemption for a folder that does not exist is a
# hole waiting for content, not a harmless leftover.
IDENTITY_DOC_ALLOWLIST = ()
# Until 2026-10-02 this was ("docs", "profiles"): "a recipient reading the
# changelog should see who wrote this and why". That reasoning was about
# INSTITUTIONAL identity in a copy sent to a colleague, and it was never
# weighed against VENDOR statements in a PUBLIC copy (open item 5). The
# public release settled it by design: a generic copy no longer ships the
# internal CHANGELOG or the project brief at all (PUBLIC_DOCS, below), so
# there is nothing left that needs the exemption, and every file in the
# copy is scanned.

# Binary and generated files the scan cannot read as text.
_SCAN_SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
                       ".zip", ".xlsx", ".xls", ".pyc")


def _normalized(text: str) -> str:
    """Text as a scan should see it: case folded, and every run of
    whitespace and comment leaders (# * / and the like) collapsed to one
    space.

    2026-10-02: `app/settings.py` named this institution in a comment
    wrapped across two lines — the first word of the name at the end of
    one, the second after the "# " that starts the next — and the
    substring scan passed it in every generic build since 2026-09-21. A marker is a phrase, and a phrase
    survives a line break; the scan has to as well.
    """
    return re.sub(r"[\s#*/;<>!-]+", " ", text).casefold()


def _identity_problems(folder: Path) -> list:
    """Every file outside the documentation allowlist that still names this
    institution. This is to identity what _walk_secrets is to API keys: the
    build must refuse to call itself generic while any of it survives."""
    found = []
    markers = identity_markers()
    # Say how many were enforced, every time. Nothing can check that the
    # DECLARED list is COMPLETE — the profile is the source of truth, and
    # an institution may edit its own list — so a marker quietly dropped
    # from it quietly weakens this gate and no test can see that. Printing
    # the count makes the weakening visible in the build report and in
    # every CI log, which is the most an automated check can honestly do
    # about a judgment call.
    print("  identity: {} marker(s) enforced".format(len(markers)))
    if not markers:
        # A gate that cannot see is not a gate that passed. Without a named
        # profile there is nothing to search FOR, and reporting a clean
        # scan would be the most dangerous sentence this script can print.
        return ["no institution profile in {}/profiles — the identity scan "
                "has nothing to search for, so this copy cannot be called "
                "clean".format(SUITE_DIR)]
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _SCAN_SKIP_SUFFIXES:
            continue
        rel = path.relative_to(folder)
        parts = rel.parts
        # The allowlist names directories inside the package, and the copy
        # now has the package one level down. Step past that prefix before
        # matching, or every exemption silently stops applying and a build
        # fails on its own changelog.
        if parts and parts[0] == SUITE_DIR:
            parts = parts[1:]
        if parts and parts[0] in IDENTITY_DOC_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            found.append("could not read {} — cannot confirm it names no "
                         "institution".format(rel))
            continue
        flat = _normalized(text)
        hits = sorted({m for m in markers if _normalized(m).strip() in flat})
        if hits:
            found.append("{} still names this institution ({})"
                         .format(rel, ", ".join(hits)))
    return found


# ---------------------------------------------------------------------------
# The vendor-and-licence scan (2026-10-02, for the public release).
#
# The identity scan asks "does this copy name THIS institution?". This one
# asks a different question: "does it say anything an institution has
# declared private?" Typically that is a vendor's name, the vocabulary of
# a licence, or the names of one office's disks and folders. Whatever
# text an institution may show about such things lives in its profile and
# is removed with it.
#
# The words themselves are declared in the institution's profile, under
# `private_terms`, and nowhere in source (2026-10-03). Until then they
# sat in a list in this file, and this file ships in the public copy, so
# the list told every reader what was being kept out of it. A profile is
# removed by the generic build, so the declaration leaves with it.
#
# Patterns, case-insensitive, on word boundaries, over the normalized text
# (so a phrase wrapped across lines is still a phrase). The one exception
# is the HTTP header `Authorization`, spelled exactly so: the AI clients
# send it, and a scan that fails on a header name is a scan somebody
# switches off. The product the suite is for is never a private term: a
# recipient needs to know it.
# ---------------------------------------------------------------------------
_HTTP_HEADER = re.compile(r"\bAuthorization\b")


def private_terms(repo=None):
    """Every (pattern, why) pair the institution profile(s) declare under
    `private_terms`, as a tuple.

    () when no profile declares any, which is what a public copy looks
    like. **() means "cannot check", never "nothing to find"**, exactly as
    for identity_markers(). An unreadable profile, a malformed entry or a
    pattern that does not compile raises: a scan that quietly skips a term
    is weaker than it says.
    """
    root = Path(repo) if repo else REPO
    folder = root / SUITE_DIR / "profiles"
    found = []
    if not folder.is_dir():
        return ()
    for item in sorted(folder.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SystemExit("cannot read {} — refusing to guess what it "
                             "declares private".format(item))
        if not isinstance(data, dict):
            continue
        for entry in data.get("private_terms") or ():
            if not (isinstance(entry, dict)
                    and str(entry.get("pattern") or "").strip()):
                raise SystemExit("{}: every private_terms entry needs a "
                                 "\"pattern\" (and a \"why\"): {!r}"
                                 .format(item.name, entry))
            pat = str(entry["pattern"]).strip().lower()
            if "-" in pat:
                # The scan reads normalized text, in which every hyphen is
                # a space (_normalized). A pattern with a hyphen can never
                # match, so it would be a term declared and never enforced
                # (1.0.1: a hyphenated collection name in a planted comment
                # passed the build). Refused.
                raise SystemExit("{}: private_terms pattern {!r} contains a "
                                 "hyphen, which the scan never sees - write "
                                 "it as a space".format(item.name, pat))
            try:
                re.compile(pat)
            except re.error as e:
                raise SystemExit("{}: private_terms pattern {!r} does not "
                                 "compile ({})".format(item.name, pat, e))
            pair = (pat, str(entry.get("why") or "declared private").strip())
            if pair not in found:
                found.append(pair)
    return tuple(found)


def private_term_hits(text: str, terms) -> list:
    """The (pattern, why) pairs of `terms` found in text."""
    flat = _normalized(_HTTP_HEADER.sub(" ", text))
    return [(pat, why) for pat, why in terms
            if re.search(r"\b" + pat + r"\b", flat)]


def _private_problems(folder: Path) -> list:
    found = []
    terms = private_terms()
    print("  vendor and licence: {} pattern(s) enforced over every file"
          .format(len(terms)))
    if not terms:
        # The same rule as the identity scan: a scan with nothing to search
        # for has not found the copy clean.
        return ["no profile in {}/profiles declares private_terms — the "
                "vendor-and-licence scan has nothing to search for, so this "
                "copy cannot be called clean".format(SUITE_DIR)]
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _SCAN_SKIP_SUFFIXES:
            continue
        rel = path.relative_to(folder)
        if rel.parts and rel.parts[0] == ".git":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            found.append("could not read {} — cannot confirm it says nothing "
                         "private".format(rel))
            continue
        hits = private_term_hits(text, terms)
        if hits:
            found.append("{} contains {}".format(rel, "; ".join(
                "'{}' ({})".format(pat, why) for pat, why in hits)))
    return found


def _verify(folder: Path, generic: bool = False) -> list:
    """Last look before anyone sends this. Returns a list of problems."""
    problems = []
    if generic:
        problems.extend(_identity_problems(folder))
        problems.extend(_private_problems(folder))
    problems.extend(_stray_problems(folder, generic))

    # A copy that is not shaped like the repository is not shippable, however
    # clean its contents. Checked before anything else about the contents.
    if not (folder / SUITE_DIR).is_dir():
        problems.append("{}/ is missing — the copy is not shaped like the "
                        "repository".format(SUITE_DIR))
    for rel, cost in REQUIRED_ROOT_FILES:
        if not (folder / rel).is_file():
            problems.append("{} is missing — {}".format(rel, cost))

    for path in folder.rglob("*.json"):
        data = _load_json(path)
        if data is None:
            # An unreadable file is not proof of safety. Say so rather than
            # skipping it silently, as the previous version did.
            problems.append("could not parse {} — cannot confirm it holds "
                            "no secret".format(path.relative_to(folder)))
            continue
        found = _walk_secrets(data, blank=False)
        if found:
            problems.append("{} secret(s) still present in {}".format(
                found, path.relative_to(folder)))
    forms = folder / SUITE_DIR / "config" / "forms"
    if forms.exists():
        problems.append("{} was not removed — it holds this machine's "
                        "remembered page settings".format(
                            forms.relative_to(folder)))
    session = folder / SUITE_DIR / "config" / "session.json"
    if session.exists():
        problems.append("{} was not removed".format(
            session.relative_to(folder)))
    for path in folder.rglob("__pycache__"):
        problems.append("byte-code cache left at {}".format(
            path.relative_to(folder)))
    return problems


def _stray_problems(folder: Path, generic: bool = False) -> list:
    """Anything in the copy that has no counterpart in the source.

    A build is exactly the source minus what was skipped, so every path in
    the destination must exist in REPO. The destination's own `.git/` is the
    one exception — --replace preserves it deliberately.

    This exists because a run on 2026-09-07 left empty `dc_admin_suite 2`,
    `.git 2` and `.github 2` directories in three built copies: two
    processes were writing the same folders at once, and the build reported
    success with junk beside its output. Anything that can put a file in a
    copy the caller believes is clean belongs in the final check.
    """
    problems = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.parts and rel.parts[0] == ".git":
            continue
        source = PUBLIC_DOCS.get(rel.as_posix()) if generic else None
        if not (REPO / (source or rel)).exists():
            kind = "directory" if path.is_dir() else "file"
            empty = " (empty)" if path.is_dir() and not any(
                path.iterdir()) else ""
            problems.append(
                "{} is in the copy but not in the source — a stray {}{}"
                .format(rel, kind, empty))
    return problems


def _destination_contents(out: Path) -> list:
    """What is in the destination that a build would have to displace.

    `.git/` is not counted: it is the destination's own identity as a
    checkout, not build output, and --replace preserves it.
    """
    return [p for p in sorted(out.iterdir()) if p.name != ".git"]


def _clear_destination(out: Path, report: list) -> None:
    """Empty the destination, keeping .git/. Raises rather than half-clearing.

    A build is the entire contents of the copy. Merging into whatever was
    there is how a distribution ends up holding files no current release
    produces — which is the failure mode this whole script exists to make
    impossible.
    """
    for item in _destination_contents(out):
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            raise SystemExit(
                "FAILED to clear {} — {}\n"
                "The destination is now part cleared and part not. Fix the "
                "cause and run again; do not ship what is there."
                .format(item, exc))
    leftover = _destination_contents(out)
    if leftover:
        raise SystemExit(
            "FAILED to clear the destination — {} item(s) survived: {}\n"
            "Do not ship what is there.".format(
                len(leftover), ", ".join(p.name for p in leftover)))
    report.append("cleared the destination before building"
                  + (" (kept its .git/)" if (out / ".git").exists() else ""))


def _refuse_unsafe_destination(out: Path) -> None:
    """Refuse a destination that overlaps the source tree.

    Inside the repo, the copy recurses into itself; containing the repo,
    --replace would delete the source. Both are typos with consequences, so
    they are refused before a single file moves.
    """
    if out == REPO:
        raise SystemExit(
            "Refusing to build into the source tree itself:\n  {}".format(out))
    if REPO in out.parents:
        raise SystemExit(
            "Refusing to build into a folder inside the source tree — the "
            "copy would recurse into itself:\n  {}\nPick a destination "
            "outside {}".format(out, REPO))
    if out in REPO.parents:
        raise SystemExit(
            "Refusing to build into a folder that CONTAINS the source tree — "
            "clearing it would delete the source:\n  {}".format(out))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staff", action="store_true",
                      help="keep this institution's branding and settings")
    mode.add_argument("--generic", action="store_true",
                      help="also reset branding, base URL and DOI prefix")
    parser.add_argument("--out", default=None,
                        help="destination folder (default: beside the repo)")
    parser.add_argument("--replace", action="store_true",
                        help="clear a non-empty destination first, keeping "
                             "its .git/ directory")
    args = parser.parse_args()

    kind = "generic" if args.generic else "staff"
    out = (Path(args.out).expanduser() if args.out else
           REPO.parent / "{}_{}".format(REPO.name, kind)).resolve()
    _refuse_unsafe_destination(out)

    report = []
    if out.exists():
        if not out.is_dir():
            print("Destination exists and is not a folder:\n  {}".format(out))
            return 1
        existing = _destination_contents(out)
        if existing and not args.replace:
            print("Destination is not empty — pass --replace to clear it "
                  "first (its .git/ is kept), or choose another --out:\n"
                  "  {}\n  {} item(s) there, including: {}".format(
                      out, len(existing),
                      ", ".join(p.name for p in existing[:5])))
            return 1
        if existing:
            _clear_destination(out, report)

    _copy(REPO, out, report)
    cleared = _strip_keys(out, report)
    if args.generic:
        _make_generic(out, report)

    problems = _verify(out, generic=args.generic)
    print("Built the {} copy at:\n  {}\n".format(kind, out))
    for line in report:
        print("  " + line)
    print("\n  {} API key(s) cleared.".format(cleared))
    if problems:
        print("\nDO NOT SEND THIS COPY — the final check found:")
        for line in problems:
            print("  ! " + line)
        return 2
    print("\nFinal check passed: no API keys, no session file, no caches.")
    print("Repository shape confirmed: {}.".format(
        ", ".join([rel for rel, _ in REQUIRED_ROOT_FILES]
                  + [SUITE_DIR + "/"])))
    if args.generic:
        print("Identity check passed: no file in the copy names this "
              "institution.")
        print("Vendor and licence check passed: no file in the copy "
              "contains a term the profile declares private.")
    print("Each recipient adds their own key under Settings > AI endpoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
