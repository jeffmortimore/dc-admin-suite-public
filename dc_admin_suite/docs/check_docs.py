#!/usr/bin/env python3
"""
Documentation-integrity check — run by CI, and before any promotion.

Why this exists. Every document that describes this suite is regenerated at
the end of a session rather than amended, because patching is how the project
accumulated a set of documents that each described a slightly different
suite, and how a distribution copy sat frozen for six weeks without anyone
noticing. That discipline has been enforced by hand. Enforcing it by hand is
how, on 2026-09-07, the brief's directory block was found listing ten modules
at v1.2/v1.4/v1.5/v1.7 against actual v1.3/v1.6/v1.8/v1.9, and how the shell
served v1.5 on every page of every install for a whole release after v1.6
shipped.

Four checks, each of which has caught a real defect:

  1. Every module's MANIFEST["version"] agrees with the brief's Current State
     table.
  2. The shell's footer version agrees with that table. The shell has no
     MANIFEST; that string IS its version, and nothing else compares it to
     anything.
  3. Every source file in app/ and modules/ is named by at least one
     document. A file nobody documents is a file nobody maintains.
  4. No banned stale string survives outside the CHANGELOG.

On the fourth: the CHANGELOG is exempt in full. Its entries are dated and
they RECORD retirements — an entry saying "the fork was retired" must keep
the word, and rewriting history to satisfy a grep would be worse than the
drift this guards against. Everywhere else, a retired thing that is still
described is worse than one that is undocumented, because it gets followed.

Exit status is 0 when everything agrees and 1 otherwise, so CI fails and a
promotion stops. Run it from anywhere:

    python3 docs/check_docs.py
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # dc_admin_suite/
DOCS = ROOT / "docs"
BRIEF = DOCS / "dc_admin_suite_project_brief.md"
CHANGELOG = DOCS / "CHANGELOG.md"
UI = ROOT / "app" / "ui.py"
# The public documents (2026-10-02). In this repository they live in
# docs/public/ and are checked here against the code, so they cannot drift
# before a build ships them. In a generic copy built from it they are the
# ONLY documents — README.md at the repository root, INSTALL.md and
# RELEASE-NOTES.md in docs/ — and there is no brief, so this check runs in
# public mode against them alone.
PUBLIC_DIR = DOCS / "public"

# Retired things. Add to this list whenever something is retired — that is
# the point of it. A string here must not appear outside the CHANGELOG.
BANNED = (
    "~/Desktop/Projects/",
    "jeffmortimore-ai/dc-admin-suite",
    "8796",
    "8850",
    "Testing checkout",
    "the fork",
    "xmake_distribution",
    "_verify_file_downloader_v12",
    "_verify_image_describer_v15",
    "_verify_ocr_toolkit_v19",
)

# Files that legitimately appear in no prose: the checker itself, and package
# markers. Everything else in app/ and modules/ must be documented somewhere.
UNDOCUMENTED_OK = {"__init__.py", "check_docs.py"}

problems = []


def problem(check, detail):
    problems.append("{}: {}".format(check, detail))


def manifest_versions():
    """{module name: version} read with ast — a module is never executed."""
    out = {}
    for path in sorted((ROOT / "modules").glob("dc_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            problem("manifest", "{} does not parse: {}".format(path.name, e))
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", "") == "MANIFEST"):
                try:
                    data = ast.literal_eval(node.value)
                except ValueError:
                    problem("manifest",
                            "{}'s MANIFEST is not a dict of literals"
                            .format(path.name))
                    break
                out[data["name"]] = (data["version"], path.name)
                break
        else:
            problem("manifest", "{} has no MANIFEST".format(path.name))
    return out


def current_state_table(text):
    """The brief's Current State table as {component: version}."""
    marker = "| Component | Version | Notes |"
    if marker not in text:
        problem("brief", "no Current State table (looked for {!r})"
                .format(marker))
        return {}
    table = text[text.index(marker):]
    end = table.find("\n\n")
    table = table[:end] if end > 0 else table
    rows = {}
    for line in table.splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*([0-9][0-9.]*)\s*\|", line)
        if m:
            rows[m.group(1).strip()] = m.group(2).strip()
    return rows


def check_versions(brief_text):
    table = current_state_table(brief_text)
    if not table:
        return table
    for name, (version, filename) in sorted(manifest_versions().items()):
        if name not in table:
            problem("versions",
                    "{} (from {}) is in no Current State row"
                    .format(name, filename))
        elif table[name] != version:
            problem("versions",
                    "{}: MANIFEST says v{}, the brief says v{}"
                    .format(name, version, table[name]))
    return table


def check_shell_version(table):
    """The shell has no MANIFEST. Its footer string is its version."""
    if not UI.exists():
        problem("shell", "app/ui.py is missing")
        return
    found = re.findall(r"shell v([0-9][0-9.]*)", UI.read_text(encoding="utf-8"))
    if not found:
        problem("shell", "no 'shell vN.N' string in app/ui.py — the footer "
                         "is the only place the shell's version exists")
        return
    if len(set(found)) > 1:
        problem("shell", "app/ui.py names more than one shell version: {}"
                .format(", ".join(sorted(set(found)))))
        return
    if "Shell" not in table:
        problem("shell", "no Shell row in the Current State table")
    elif table["Shell"] != found[0]:
        problem("shell",
                "the footer serves v{} and the brief says v{}"
                .format(found[0], table["Shell"]))


def documents():
    return sorted(DOCS.glob("*.md"))


def check_every_file_is_documented(all_prose):
    for folder in ("app", "modules"):
        for path in sorted((ROOT / folder).glob("*.py")):
            if path.name in UNDOCUMENTED_OK:
                continue
            # A verification suite is covered by the `_verify_*.py` wildcard
            # the brief uses to describe all four at once.
            if path.name.startswith("_verify_") and "_verify_*.py" in all_prose:
                continue
            if path.name not in all_prose:
                problem("undocumented",
                        "{}/{} is named by no document"
                        .format(folder, path.name))


def check_banned_strings():
    targets = []
    for folder in ("app", "modules", "docs"):
        targets += sorted((ROOT / folder).glob("*.py"))
        targets += sorted((ROOT / folder).glob("*.md"))
    for path in targets:
        if path.resolve() == CHANGELOG.resolve():
            continue                      # dated prose that records retirements
        if path.resolve() == Path(__file__).resolve():
            continue                      # this file lists them by definition
        text = path.read_text(encoding="utf-8", errors="replace")
        for banned in BANNED:
            if banned in text:
                line = next((i for i, l in enumerate(text.splitlines(), 1)
                             if banned in l), 0)
                problem("stale",
                        "{}:{} still refers to {!r}"
                        .format(path.relative_to(ROOT), line, banned))


def public_documents():
    """{role: path} for the public set, wherever this copy keeps it."""
    if BRIEF.exists():
        return {"README": PUBLIC_DIR / "README.md",
                "INSTALL": PUBLIC_DIR / "INSTALL.md",
                "NOTES": PUBLIC_DIR / "RELEASE-NOTES.md"}
    return {"README": ROOT.parent / "README.md",
            "INSTALL": DOCS / "INSTALL.md",
            "NOTES": DOCS / "RELEASE-NOTES.md"}


def _table_rows(text, header_start, name_col, version_col):
    """{first cell: version cell} for the first table whose header row
    starts with header_start. Empty when there is none (said by caller)."""
    at = text.find(header_start)
    if at < 0:
        return {}
    block = text[at:]
    end = block.find("\n\n")
    rows = {}
    for line in (block[:end] if end > 0 else block).splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > max(name_col, version_col):
            rows[cells[name_col]] = cells[version_col]
    return rows


def check_public_documents():
    """The README's module table and the release notes' table agree with
    every MANIFEST and with the shell's footer, row for row."""
    docs = public_documents()
    for role, path in docs.items():
        if not path.is_file():
            problem("public", "{} is missing ({})".format(
                path.relative_to(ROOT.parent), role))
    if any(not path.is_file() for path in docs.values()):
        return ""
    readme = docs["README"].read_text(encoding="utf-8")
    notes = docs["NOTES"].read_text(encoding="utf-8")
    tables = {
        "README": _table_rows(readme, "| Module | File | Version |", 0, 2),
        "release notes": _table_rows(notes, "| Module | Version |", 0, 1),
    }
    code = {name: version for name, (version, _f)
            in manifest_versions().items()}
    footer = re.findall(r"shell v([0-9][0-9.]*)",
                        UI.read_text(encoding="utf-8")) if UI.exists() else []
    for where, rows in tables.items():
        if not rows:
            problem("public", "the {} has no module table".format(where))
            continue
        shell = {k: v for k, v in rows.items() if k.startswith("Shell")}
        mods = {k: v for k, v in rows.items() if not k.startswith("Shell")}
        for name, version in sorted(code.items()):
            if name not in mods:
                problem("public", "{} is not in the table in the {}".format(
                    name, where))
            elif mods[name] != version:
                problem("public", "{}: MANIFEST says {}, the {} says {}"
                        .format(name, version, where, mods[name]))
        for name in sorted(set(mods) - set(code)):
            problem("public", "the table in the {} lists {!r}, which is no module here"
                    .format(where, name))
        if where == "release notes":
            if not shell:
                problem("public", "the release notes have no Shell row")
            elif footer and list(shell.values())[0] != footer[0]:
                problem("public", "the footer serves shell v{} and the "
                        "release notes say {}".format(
                            footer[0], list(shell.values())[0]))
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in docs.values())


def main():
    public_prose = check_public_documents()
    if BRIEF.exists():
        brief_text = BRIEF.read_text(encoding="utf-8")
        all_prose = "\n".join(p.read_text(encoding="utf-8",
                                          errors="replace")
                              for p in documents())
        table = check_versions(brief_text)
        check_shell_version(table)
        mode = "development"
    else:
        # A public copy: the public documents are all there is.
        all_prose = public_prose + "\n" + (
            (ROOT / "modules" / "README.md").read_text(encoding="utf-8")
            if (ROOT / "modules" / "README.md").is_file() else "")
        mode = "public"
    check_every_file_is_documented(all_prose)
    check_banned_strings()

    print("Documentation integrity — check ({} copy)".format(mode))
    if not problems:
        if mode == "development":
            print("  every MANIFEST agrees with the brief's Current State table")
            print("  the shell's footer agrees with it")
        print("  the public README and release notes agree with every "
              "MANIFEST and the footer")
        print("  every file in app/ and modules/ is documented")
        print("  no retired name survives outside the CHANGELOG")
        print("  problems: 0")
        return 0
    print("  problems: {}".format(len(problems)))
    for p in problems:
        print("  FAIL  " + p)
    return 1


if __name__ == "__main__":
    sys.exit(main())
