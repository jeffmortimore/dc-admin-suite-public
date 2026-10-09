#!/usr/bin/env python3
"""Verify every page this suite serves — the shell's and every module's.

    python3 modules/_verify_pages.py

Each module carries its whole UI in one PAGE string, and the shell carries
its own in APP_HTML. Nothing compiles those: a stray brace or a typo'd
element id is invisible to `python -m compileall` and only shows up as a
dead button in front of a member of staff. Two checks close that gap:

  node --check          the page's <script> block must parse as JavaScript.
  getElementById pass   every id the script asks for by $("...") or
                        getElementById("...") must exist in the markup it
                        is served with.

Both were run by hand at every module release; this is that routine made
repeatable so CI can run it on every push. Needs `node` on PATH.

Exit status is 0 when every page passes, 1 otherwise — with the offending
page, the check that failed, and node's own message.
"""

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SUITE = Path(__file__).resolve().parent.parent

# (file, name of the module-level string holding the served HTML)
PAGE_SOURCES = [("app/ui.py", "APP_HTML")] + [
    ("modules/{}".format(p.name), "PAGE")
    for p in sorted((SUITE / "modules").glob("dc_*.py"))
]

SCRIPT_RE = re.compile(r"<script(?:\s+[^>]*)?>(.*?)</script>", re.S | re.I)
# $("someId") — the shell and every module use this helper — and the plain
# DOM call, so a page that skips the helper is still covered.
ID_RE = re.compile(r'\$\(\s*"([A-Za-z0-9_-]+)"\s*\)'
                   r'|getElementById\(\s*"([A-Za-z0-9_-]+)"\s*\)')


def page_html(rel_path, var_name):
    """The served HTML string, read without importing the module (importing
    would run its top-level code and, for some, bind a socket)."""
    src = (SUITE / rel_path).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    return ast.literal_eval(node.value)
    return None


def _node_error(done):
    """The useful line of node's output. node prints the offending source,
    a caret, the error, then its own version — so the last line is the
    version banner, which tells a reader nothing."""
    lines = [l.rstrip() for l in
             (done.stderr or done.stdout).splitlines() if l.strip()]
    for line in lines:
        if "Error" in line:                     # SyntaxError, ReferenceError…
            return line.strip()
    return lines[0].strip() if lines else "unknown error"


def check_scripts(node_bin, label, html, problems):
    scripts = SCRIPT_RE.findall(html)
    if not scripts:
        problems.append("{}: no <script> block found".format(label))
        return
    for i, script in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as fh:
            fh.write(script)
            tmp = fh.name
        try:
            done = subprocess.run([node_bin, "--check", tmp],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if done.returncode != 0:
                problems.append("{}: script #{} failed node --check: {}".format(
                    label, i, _node_error(done)))
        finally:
            Path(tmp).unlink(missing_ok=True)


def check_ids(label, html, problems):
    scripts = "\n".join(SCRIPT_RE.findall(html))
    wanted = {a or b for a, b in ID_RE.findall(scripts)}
    missing = sorted(i for i in wanted if 'id="{}"'.format(i) not in html)
    if missing:
        problems.append("{}: script asks for element id(s) the markup does "
                        "not define: {}".format(label, ", ".join(missing)))


# ---------------------------------------------------------------------------
# The log-box contract, checked across every page that has one.
#
# Reported 2026-09-08: the log snapped back to the tail every few seconds
# and destroyed any selection mid-copy. The cause was one line, copied
# verbatim into nine modules:
#
#     logBox.textContent=s.log.join(NL); logBox.scrollTop=logBox.scrollHeight;
#
# It was fixed in all nine at once. This check is what stops a tenth module,
# started from a copy of an older one, from quietly bringing it back - which
# is how it reached nine in the first place.
# ---------------------------------------------------------------------------
LOG_BANNED = (
    "logBox.textContent=",
    "lb.textContent=",
    "logBox.scrollTop=logBox.scrollHeight",
    "lb.scrollTop=lb.scrollHeight",
)

LOG_REQUIRED = (
    ("renderLog(", "render the log through renderLog()"),
    ("logSelectionLive()", "hold off while a selection is live"),
    ("logAtTail()", "follow the tail only from the tail"),
    ('id="logcopy"', "offer a Copy log button"),
)


def check_log_box(label, html, problems):
    """A page with a log must render it the shared way, or not at all."""
    if 'id="log"' not in html:
        return                          # no log on this page; nothing to hold
    for bad in LOG_BANNED:
        if bad in html:
            problems.append(
                "{}: reintroduces the log defect — `{}` rewrites the whole "
                "log and follows the tail unconditionally, which discards "
                "the reader's selection. Render through renderLog()."
                .format(label, bad))
    for needle, what in LOG_REQUIRED:
        if needle not in html:
            problems.append(
                "{}: has a log but does not {} (missing `{}`)"
                .format(label, what, needle))


def check_shell_returns_to_a_running_module(problems):
    """Shell v1.11: opening a module that is running returns to it.

    Not a page check, but the fix is half in the shell's page and half in
    the process that serves it, and this is the suite that owns the shell.
    2026-09-30: the shell started a second, idle copy on the next port
    while the first was mid-run. Driven with a fake process so nothing is
    actually launched.
    """
    sys.path.insert(0, str(SUITE))
    from app import module_registry as R
    saved = (R.scan_modules, R.settings_mod.write_session_context,
             R.subprocess.Popen, R._free_port, R.time.sleep,
             dict(R.RUNNING), dict(R.RUNNING_URLS))
    started, ports = [], iter([8815, 8816, 8817])

    class Proc:
        def __init__(self, *a, **k):
            self.pid, self.code = 1000 + len(started), None
            started.append(self)

        def poll(self):
            return self.code

    try:
        R.scan_modules = lambda: [{"id": "dl", "name": "Downloader",
                                   "file": "dc_x.py", "error": ""}]
        R.settings_mod.write_session_context = lambda s: "session.json"
        R.subprocess.Popen = Proc
        R._free_port = lambda start: next(ports)
        R.time.sleep = lambda s: None
        R.RUNNING.clear()
        R.RUNNING_URLS.clear()
        first = R.launch_module("dl", {"hub_port": 8750})
        again = R.launch_module("dl", {"hub_port": 8750})
        if len(started) != 1 or again.get("url") != first.get("url") \
                or not again.get("reused"):
            problems.append(
                "shell: opening a running module started another copy "
                "({} process(es); first {}, then {})".format(
                    len(started), first.get("url"), again.get("url")))
        started[0].code = 0             # the module's process has ended
        later = R.launch_module("dl", {"hub_port": 8750})
        if len(started) != 2 or later.get("reused"):
            problems.append(
                "shell: a module whose process ended was not started again "
                "({} process(es), reused={})".format(
                    len(started), later.get("reused")))
    finally:
        (R.scan_modules, R.settings_mod.write_session_context,
         R.subprocess.Popen, R._free_port, R.time.sleep) = saved[:5]
        R.RUNNING.clear(); R.RUNNING.update(saved[5])
        R.RUNNING_URLS.clear(); R.RUNNING_URLS.update(saved[6])


# ---------------------------------------------------------------------------
# One notification palette (v1.34.2 / shell v1.12):
# instructions gray, success green, errors and warnings red, across the
# shell and every module. The block is duplicated verbatim — modules are
# standalone — so the check is that every copy is the same copy.
# ---------------------------------------------------------------------------
PALETTE_RE = re.compile(r"/\* DC-PALETTE (\d+) .*?/\* /DC-PALETTE \*/", re.S)
PALETTE_PAIRS = (("--dc-info-ink", "--dc-info-bg"),
                 ("--dc-ok-ink", "--dc-ok-bg"),
                 ("--dc-bad-ink", "--dc-bad-bg"),
                 ("--dc-amber-ink", "--dc-amber-bg"))


def _luminance(hexcolor):
    h = hexcolor.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
         for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check_palette(pages, problems):
    """Every page carries the one palette block, identical, last in its
    <style>; every ink meets 4.5:1 on its own background and on white."""
    blocks = {}
    for label, html in pages:
        full = [m.group(0) for m in PALETTE_RE.finditer(html)]
        if len(full) != 1:
            problems.append("{}: carries {} DC-PALETTE block(s), not one"
                            .format(label, len(full)))
            continue
        tail = html[html.index(full[0]) + len(full[0]):]
        if not tail.lstrip().startswith("</style>"):
            problems.append("{}: the DC-PALETTE block is not the last thing "
                            "in its <style>, so later rules can overrule "
                            "it".format(label))
        blocks[label] = full[0]
    if len(set(blocks.values())) > 1:
        first = next(iter(blocks))
        for label, block in blocks.items():
            if block != blocks[first]:
                problems.append("{}: its DC-PALETTE block differs from {}'s"
                                .format(label, first))
    if not blocks:
        return
    block = next(iter(blocks.values()))
    var = dict(re.findall(r"(--dc-[a-z-]+):(#[0-9a-fA-F]{6})", block))
    for ink, bg in PALETTE_PAIRS:
        if ink not in var or bg not in var:
            problems.append("palette: {} or {} is not defined".format(ink, bg))
            continue
        for ground, name in ((var[bg], bg), ("#ffffff", "white")):
            r = contrast(var[ink], ground)
            if r < 4.5:
                problems.append("palette: {} on {} is {:.2f}:1, below AA"
                                .format(ink, name, r))


HIDDEN_TAG_RE = re.compile(r"<([a-z]+)\b([^>]*\shidden\b[^>]*)>", re.I)
STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)


def check_hidden_stays_hidden(label, html, problems):
    """An element the markup hides must not be shown by its own class.

    v1.34.2, found live: the Clear panel carried `hidden` and the class
    `clearask`, whose rule set display:flex, so the panel was on screen
    from page load. A script check cannot see this — only the styles say
    it. Any rule giving `display` to a hidden element's class or id must
    be overruled by a `[hidden]{display:none!important}` rule.
    """
    css = " ".join(STYLE_RE.findall(html))
    guarded = re.search(r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important",
                        css) is not None
    for _tag, attrs in HIDDEN_TAG_RE.findall(html):
        names = []
        m = re.search(r'\bclass="([^"]+)"', attrs)
        if m:
            names += ["." + c for c in m.group(1).split()]
        m = re.search(r'\bid="([^"]+)"', attrs)
        if m:
            names.append("#" + m.group(1))
        for name in names:
            rule = re.search(re.escape(name) + r"(?![\w-])[^{]*\{[^}]*\bdisplay\s*:",
                             css)
            if rule and not guarded:
                problems.append(
                    "{}: {} is marked hidden in the markup but its CSS sets "
                    "display, which shows it anyway — no [hidden] rule "
                    "overrules it".format(label, name))


def check_shell_steps_are_toned(node_bin, problems):
    """Behavioral: paintSteps() runs against a stub page and the Chrome
    line comes out green when that step is done, gray when it is not."""
    html = page_html("app/ui.py", "APP_HTML")
    script = SCRIPT_RE.findall(html or "")
    src = "\n".join(script)
    start = src.find("function paintSteps()")
    end = src.find("\n}\n", start)
    esc_at = src.find("const esc = ")
    if start < 0 or end < 0 or esc_at < 0:
        problems.append("shell: paintSteps() or esc() not found")
        return
    esc = src[esc_at:src.find(";\n", esc_at) + 1]
    js = (esc + "\n" + src[start:end + 2] + """
const els = {};
function $(id){ return els[id] || (els[id] = {classList:{toggle(){}}}); }
let painted = "";
function setStatus(el, kind, html){ painted = kind + "|" + html; }
let splashDone = {url:true, deps:false, chrome:true};
paintSteps();
console.log(painted);
""")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(js)
        tmp = fh.name
    try:
        done = subprocess.run([node_bin, tmp], capture_output=True,
                              text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    finally:
        Path(tmp).unlink()
    out = done.stdout.strip()
    if done.returncode != 0:
        problems.append("shell: paintSteps() did not run: " +
                        _node_error(done))
        return
    if '<span class="dc-ok">✓ Connected to Chrome</span>' not in out:
        problems.append("shell: a connected Chrome is not shown in the "
                        "success tone ({})".format(out[:160]))
    if '<span class="dc-info">• Dependencies not verified yet</span>' \
            not in out:
        problems.append("shell: a step still to do is not shown in the "
                        "instruction tone ({})".format(out[:160]))


# ---------------------------------------------------------------------------
# DC-LOG 1 and DC-RUN 1 (for the public release). Two operator
# requests — Clear for a new run, and a Copy log that is
# exactly the current run — went into the downloader in v1.34.2 and into
# every module with a log here. Count the group, not the pair: every page
# with a log is in it, found by its markup rather than by a list someone
# keeps. The blocks are duplicated verbatim, as the palette is, so the
# first checks are agreement; the rest DRIVE each module in-process —
# its log(), its endpoints, its run claim — because a block that agrees
# with its siblings and is never called is the adjacent-check failure.
# ---------------------------------------------------------------------------
LOG_JS_RE = re.compile(r"/\* DC-LOG (\d+):.*?/\* /DC-LOG \*/", re.S)
LOG_HTML_RE = re.compile(r"<!-- DC-LOG (\d+):.*?<!-- /DC-LOG -->", re.S)
LOG_CSS_RE = re.compile(r"/\* DC-LOG (\d+) styles \*/.*?/\* /DC-LOG styles \*/",
                        re.S)
LOG_PY_RE = re.compile(r"# ---- DC-LOG (\d+) -+\n.*?# ---- /DC-LOG -+\n", re.S)
RUN_PY_RE = re.compile(r"# ---- DC-RUN (\d+) -+\n.*?# ---- /DC-RUN -+\n", re.S)
# The downloader claims its runs in _claim_and_start(), which also takes
# the cross-process lock (v1.34.1); every other module claims through
# DC-RUN. Named here, with the reason, rather than skipped by a pattern.
OWN_CLAIM = {"modules/dc_file_downloader.py":
             "claims in _claim_and_start(), with the cross-process lock"}
NOT_RUNNING = ("idle", "done", "error")


def _one_block(label, text, rx, what, problems):
    found = [m.group(0) for m in rx.finditer(text)]
    if len(found) != 1:
        problems.append("{}: carries {} {} block(s), not one"
                        .format(label, len(found), what))
        return None
    return found[0]


def _agree(blocks, what, problems):
    if len(set(blocks.values())) > 1:
        first = next(iter(blocks))
        for label, block in blocks.items():
            if block != blocks[first]:
                problems.append("{}: its {} block differs from {}'s"
                                .format(label, what, first))


def log_pages(served):
    """The group: every served module page with a log box."""
    return [(label, html) for label, html in served
            if label.startswith("modules/") and 'id="log"' in html]


def check_log_blocks(served, problems):
    """Every page with a log carries the one DC-LOG script, markup and
    styles, and its module the one DC-LOG Python block; all identical."""
    group = log_pages(served)
    js, mk, css, py, run = {}, {}, {}, {}, {}
    for label, html in group:
        src = (SUITE / label).read_text(encoding="utf-8")
        for store, text, rx, what in ((js, html, LOG_JS_RE, "DC-LOG script"),
                                      (mk, html, LOG_HTML_RE, "DC-LOG markup"),
                                      (css, html, LOG_CSS_RE, "DC-LOG styles"),
                                      (py, src, LOG_PY_RE, "DC-LOG Python")):
            block = _one_block(label, text, rx, what, problems)
            if block is not None:
                store[label] = block
        if label in OWN_CLAIM:
            if RUN_PY_RE.search(src):
                problems.append("{}: carries DC-RUN beside its own claim"
                                .format(label))
        else:
            block = _one_block(label, src, RUN_PY_RE, "DC-RUN Python",
                               problems)
            if block is not None:
                run[label] = block
        # The page's copy is the SERVED one; a block with a backslash would
        # serve different bytes from a raw PAGE and from a plain one.
        for store, what in ((js, "script"), (mk, "markup"), (css, "styles")):
            if label in store and "\\" in store[label]:
                problems.append("{}: its DC-LOG {} contains a backslash"
                                .format(label, what))
    for store, what in ((js, "DC-LOG script"), (mk, "DC-LOG markup"),
                        (css, "DC-LOG styles"), (py, "DC-LOG Python"),
                        (run, "DC-RUN Python")):
        _agree(store, what, problems)
    return group


def _strip_js_comments(src):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"(?m)^\s*//.*$", "", src), flags=re.S)


def check_poll_reports_state(group, problems):
    """Each page's poll() hands the state it read to logStateSeen().

    A SOURCE check, said so: running each page's whole script against a
    stub DOM is more machinery than this guarantee is worth, and the
    block's own behaviour is driven in node below. Comments are stripped
    first, and the call must sit inside poll()'s own body.
    """
    for label, html in group:
        src = _strip_js_comments("\n".join(SCRIPT_RE.findall(html)))
        at = src.find("async function poll(")
        if at < 0:
            problems.append("{}: has a log but no poll()".format(label))
            continue
        depth, i, body = 0, src.index("{", at), None
        for j in range(i, len(src)):
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            if depth == 0:
                body = src[i:j]
                break
        if not body or "logStateSeen(s)" not in body:
            problems.append("{}: poll() does not call logStateSeen(s), so "
                            "Clear is never enabled or disabled".format(label))


def check_log_block_in_node(node_bin, group, problems):
    """Behavioral: the DC-LOG script against a stub page. Clear is
    disabled during every running phase and enabled otherwise; a clear
    seen from another tab empties this page's copy box."""
    if not group:
        return
    block = LOG_JS_RE.search(group[0][1]).group(0)
    js = """
const els = {};
function el(id){ return els[id] || (els[id] = {id:id, hidden:false, disabled:false,
  value:'x', listeners:{}, addEventListener(t,f){ this.listeners[t]=f; },
  focus(){}, select(){}, classList:{contains(){ return false; }}}); }
const document = {getElementById: el};
let cleared = 0;
function clearErr(){ cleared += 1; }
""" + block + """
const out = [];
for (const ph of ['starting','scraping','waiting','downloading','writing']) {
  logStateSeen({phase: ph}); out.push(ph + '=' + el('logclear').disabled);
}
for (const ph of ['idle','done','error']) {
  logStateSeen({phase: ph}); out.push(ph + '=' + el('logclear').disabled);
}
el('clearask').hidden = false; logStateSeen({phase:'scraping'});
out.push('ask=' + el('clearask').hidden);
logStateSeen({phase:'done', cleared: 0});
el('logfull').hidden = false; el('logfull').value = 'old run';
logStateSeen({phase:'idle', cleared: 1});
out.push('full=' + el('logfull').hidden + '/' + JSON.stringify(el('logfull').value));
out.push('callout=' + cleared);
out.push('lines=' + logLines('a' + LOG_NL + 'b' + LOG_NL) + ',' + logLines(''));
console.log(out.join(' '));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(js)
        tmp = fh.name
    try:
        done = subprocess.run([node_bin, tmp], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    finally:
        Path(tmp).unlink()
    want = ("starting=true scraping=true waiting=true downloading=true "
            "writing=true idle=false done=false error=false ask=true "
            'full=true/"" callout=1 lines=2,0')
    if done.returncode != 0 or done.stdout.strip() != want:
        problems.append("DC-LOG script: behaved as {!r}, expected {!r}{}"
                        .format(done.stdout.strip(), want,
                                " — " + _node_error(done)
                                if done.returncode else ""))


def _import_module(label):
    import importlib.util
    path = SUITE / label
    name = "_dclog_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _phases_set(src):
    """Every phase a module's source can set, as string literals."""
    found = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "phase" and isinstance(kw.value, ast.Constant):
                    found.add(kw.value.value)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if (isinstance(t, ast.Subscript) and isinstance(node.value, ast.Constant)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "phase"):
                found.add(node.value.value)
    return found


def _threads_in_handler(src):
    """threading.Thread(...) calls inside make_handler(), by ast."""
    calls = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "make_handler":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "Thread"):
                    calls.append(sub.lineno)
    return calls


def _http(port, path, body=None):
    import urllib.request
    import urllib.error
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:{}{}".format(port, path),
                                 data=data, method="GET" if body is None
                                 else "POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def check_log_modules_driven(group, problems):
    """Drive every log module in-process: log() feeds the whole-run log,
    /api/log serves past the page's 200 lines, /api/clear is refused
    while a run is active and otherwise clears exactly what it says, and
    DC-RUN's claim admits one run of many simultaneous requests."""
    import threading
    from http.server import ThreadingHTTPServer
    for label, _html in group:
        src = (SUITE / label).read_text(encoding="utf-8")
        try:
            M = _import_module(label)
        except Exception as e:                       # pragma: no cover
            problems.append("{}: could not be imported: {}: {}".format(
                label, e.__class__.__name__, e))
            continue
        initial = json.loads(json.dumps(M.STATE, default=str))
        quiet = M.log

        # The page's rule (anything but idle, done or error is running)
        # must be the module's rule, for every phase the module can set.
        running = set(getattr(M, "RUNNING_PHASES", ()))
        for ph in NOT_RUNNING:
            if ph in running:
                problems.append("{}: RUNNING_PHASES contains {!r}, which "
                                "the page treats as finished".format(label, ph))
        for ph in sorted(_phases_set(src) - running - set(NOT_RUNNING)):
            problems.append("{}: sets phase {!r}, which is neither in "
                            "RUNNING_PHASES nor idle/done/error".format(label, ph))
        # The last run's download link goes with its summary, wherever a
        # module keeps one: a link left behind offers the old run's file
        # under a clean page.
        if "last_file" in initial and "last_file" not in M.CLEAR_RESETS:
            problems.append("{}: Clear keeps last_file, the last run's "
                            "download link".format(label))
        for key, value in M.CLEAR_RESETS.items():
            if key not in initial:
                problems.append("{}: CLEAR_RESETS names {!r}, which STATE "
                                "does not have".format(label, key))
            elif initial[key] != value:
                problems.append("{}: Clear sets {!r} to {!r}, not its "
                                "starting value {!r}".format(
                                    label, key, value, initial[key]))
        if label not in OWN_CLAIM and _threads_in_handler(src):
            problems.append("{}: make_handler() starts a thread itself (line "
                            "{}), outside start_run()'s claim".format(
                                label, _threads_in_handler(src)))

        session = M.load_session(Path("/nonexistent/session.json"))
        session["hub_url"] = "http://127.0.0.1:8750"
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                      M.make_handler(session,
                                                     M.build_page(session)))
        except Exception as e:
            problems.append("{}: its server would not start: {}: {}".format(
                label, e.__class__.__name__, e))
            continue
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            M.log("a line from the PREVIOUS run")
            M.begin_run_log()
            for n in range(250):
                M.log("line {}".format(n))
            code, txt = _http(port, "/api/log")
            lines = txt.strip().split("\n") if txt.strip() else []
            if code != 200 or len(lines) != 250 or "PREVIOUS" in txt:
                problems.append("{}: /api/log gave {} with {} line(s) (want "
                                "200 with the run's 250 and nothing before "
                                "it)".format(label, code, len(lines)))
            if len(M.STATE["log"]) != 200:
                problems.append("{}: the page's log holds {} lines, not 200"
                                .format(label, len(M.STATE["log"])))
            keep = {k: v for k, v in json.loads(json.dumps(M.STATE, default=str)).items()
                    if k not in M.CLEAR_RESETS and k not in
                    ("log", "progress", "summary", "phase", "cleared")}
            with M.LOCK:
                M.STATE["phase"] = sorted(running)[0] if running else "busy"
                M.STATE["summary"] = "the last run"
            code, _ = _http(port, "/api/clear", {})
            if code != 409 or M.STATE["summary"] != "the last run":
                problems.append("{}: /api/clear during a run gave {} and the "
                                "summary is {!r}".format(label, code,
                                                         M.STATE["summary"]))
            with M.LOCK:
                M.STATE["phase"] = "done"
                for key in M.CLEAR_RESETS:
                    if M.STATE.get(key) in (None, False, 0, []):
                        M.STATE[key] = "left over"
            code, _ = _http(port, "/api/clear", {})
            after = json.loads(json.dumps(M.STATE, default=str))
            if code != 200 or after["log"] or after["summary"] or \
                    after["phase"] != "idle" or _http(port, "/api/log")[1]:
                problems.append("{}: /api/clear after a run gave {} and left "
                                "phase {!r}, summary {!r}, {} log line(s)"
                                .format(label, code, after["phase"],
                                        after["summary"], len(after["log"])))
            for key, value in M.CLEAR_RESETS.items():
                if after.get(key) != value:
                    problems.append("{}: Clear left {!r} as {!r}".format(
                        label, key, after.get(key)))
            for key, value in keep.items():
                if after.get(key) != value:
                    problems.append("{}: Clear changed {!r}, which it must "
                                    "keep".format(label, key))
            if after.get("cleared") != initial.get("cleared", 0) + 1:
                problems.append("{}: Clear did not tell the open pages "
                                "(cleared = {!r})".format(label,
                                                          after.get("cleared")))
            if label not in OWN_CLAIM:
                _drive_start_run(label, M, problems)
        finally:
            srv.shutdown()
            srv.server_close()
            M.log = quiet


def _drive_start_run(label, M, problems):
    import threading
    with M.LOCK:
        M.STATE["phase"] = "idle"
    release, began = threading.Event(), []
    stop = threading.Event()

    def target(tag):
        began.append(tag)
        release.wait(10)

    M.log("a line from before the claim")
    gate = threading.Barrier(8)
    won = []

    def contender(i):
        gate.wait(5)
        won.append(M.start_run(target, (i,), events=(stop,)))

    stop.set()
    ts = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    import time
    for _ in range(100):
        if began:
            break
        time.sleep(0.02)
    if won.count(True) != 1 or len(began) != 1:
        problems.append("{}: eight simultaneous starts gave {} claim(s) and "
                        "{} worker(s); want one of each".format(
                            label, won.count(True), len(began)))
    if "before the claim" in M.run_log_text():
        problems.append("{}: the whole-run log did not begin at the claim"
                        .format(label))
    if stop.is_set():
        problems.append("{}: the winning start did not clear its events"
                        .format(label))
    stop.set()
    if M.start_run(target, ("late",), events=(stop,)) or not stop.is_set():
        problems.append("{}: a refused start cleared the running job's "
                        "events".format(label))
    release.set()
    for _ in range(100):
        with M.LOCK:
            ph = M.STATE["phase"]
        if ph not in M.RUNNING_PHASES:
            break
        time.sleep(0.02)
    if ph != "error" or "ended without recording how" not in M.run_log_text():
        problems.append("{}: a worker that ended without saying how left "
                        "phase {!r}".format(label, ph))
    with M.LOCK:
        M.STATE["phase"] = "idle"


def check_run_folder_logs(problems):
    """The two modules with a run folder save the run's whole log into it,
    on a way out that is not success: batch revise's Download stopped by an
    expired session, and the OCR toolkit's copy-mode run with no files."""
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as d, \
            contextlib.redirect_stdout(io.StringIO()):
        B = _import_module("modules/dc_batch_revise.py")

        class Driver:
            current_url = "https://dc/cgi/login.cgi"
            page_source = "<html>login</html>"

            def get(self, url):
                pass

        B._attach_or_fail = lambda session: Driver()
        B.looks_like_login_page = lambda html, url: True
        B.write_download_workbook = lambda path, rows, rgb: \
            Path(path).write_text("report", encoding="utf-8")
        B.QUEUE = [{"ctx": "series-a", "url": "https://dc/series-a",
                    "type": "series", "epoch": 0}]
        B.time.sleep, real_sleep = (lambda s: None), B.time.sleep
        try:
            B.begin_run_log()
            B.download_worker({"base_url": "https://dc"}, d)
        finally:
            B.time.sleep = real_sleep
        txts = sorted(Path(d).glob("DC_BatchRevise_Reports_*/DC_BatchRevise_Log_*.txt"))
        text = txts[0].read_text(encoding="utf-8") if txts else ""
        if len(txts) != 1 or "Saving reports into" not in text \
                or "saved as DC_BatchRevise_Log_" not in text:
            problems.append("dc_batch_revise: a stopped Download saved {} "
                            "whole-run log(s) into its run folder".format(len(txts)))

        O = _import_module("modules/dc_ocr_toolkit.py")
        O.begin_run_log()
        O.process_worker({}, {"entries": [], "out_dir": d, "save": "copy",
                              "mode": "auto"}, None)
        txts = sorted(Path(d).glob("DC_OCR_Output_*/DC_OCR_Log_*.txt"))
        text = txts[0].read_text(encoding="utf-8") if txts else ""
        if len(txts) != 1 or "Output run folder" not in text:
            problems.append("dc_ocr_toolkit: a copy-mode run saved {} "
                            "whole-run log(s) into its run folder ({})".format(
                                len(txts), O.STATE.get("summary")))


def check_callout_above_the_form(label, html, problems):
    """The message callout comes first inside <main>, before any form.

    It scrolls itself into view, so wherever it sits is where the page
    jumps. In the image describer every Save, Duplicate
    and Delete threw the page to the bottom, "as we've seen before in
    other modules". Only the downloader had the callout above its form;
    eight modules had it beside the status line under the last fieldset.
    """
    at = html.find('id="cerr"')
    if at < 0:
        return                         # no callout on this page
    main = html.find("<main")
    first = min([i for i in (html.find("<fieldset", main),
                             html.find("<form", main),
                             html.find('id="status"', main)) if i >= 0]
                or [len(html)])
    if main < 0 or not main < at < first:
        problems.append("{}: the message callout is not above the form, so "
                        "showing a message scrolls the page to wherever it "
                        "sits".format(label))


def check_dialogs_speak_for_themselves(label, html, problems):
    """Every dialog holds its own message region (role alert or status).

    A profile Save the module refused once put its error in
    the page callout, behind the open dialog, so it looked like a Save
    that did nothing. A dialog's own actions must be able to answer inside
    it. STRUCTURAL, said so: that each dialog's script writes there is
    driven for the image describer (ProfileDuplicate) and not for the
    others, whose dialogs already wrote to theirs (the shell's endpoint
    editor, the DOI journal editor) or gained one today (the drafter's
    link dialog, which used to move focus without a word).
    """
    for m in re.finditer(r'<div\b[^>]*role="dialog"[^>]*>', html):
        # the dialog's extent: up to the matching close of its <div>
        depth, i, end = 0, m.start(), None
        for t in re.finditer(r"<(/?)div\b[^>]*>", html[m.start():]):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                end = m.start() + t.end()
                break
        body = html[m.start():end or len(html)]
        if not re.search(r'role="(alert|status)"', body):
            name = re.search(r'aria-labelledby="([^"]+)"', m.group(0))
            problems.append("{}: the dialog {} has no message region of its "
                            "own, so its errors land behind it".format(
                                label, name.group(1) if name else "?"))


# "click X" in anything the operator reads: a log line, an error, the page's
# own help text. Only string literals count, read with ast (implicit
# concatenation already joined), so a comment or docstring describing what
# the module itself clicks on Digital Commons is not mistaken for advice.
CLICK_RE = re.compile(r"\b[Cc]lick\s+[\"\u201c'\u2018]?"
                      r"([A-Z][^\"\u201d\u2019'\n.,;:\u2014<(]{0,40})")


def _operator_strings(path):
    """Every string literal in a source file except docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def check_named_buttons_exist(problems):
    """A message that says "click X" names a button the page really has.

    2026-10-03, a stranger's install: the shell's dependency advice said
    click "Re-check dependencies" beside a button reading "Check
    dependencies". Checked across the whole group, which found a second
    one ("click Scrape again", beside "Scrape URLs"). The shell's messages
    come from every file in app/; a module's from its own file. A label
    matches when the phrase is the label or begins with it and a space, so
    "Run again" names Run and "Loader" does not name Load. Buttons are
    read from <button> markup; a label set only by script is not seen.
    """
    for rel_path, var_name in PAGE_SOURCES:
        html = page_html(rel_path, var_name)
        if html is None:
            continue
        labels = set()
        for m in re.finditer(r"<button\b[^>]*>(.*?)</button>", html, re.S):
            text = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split())
            if text:
                labels.add(text)
        files = (sorted((SUITE / "app").glob("*.py"))
                 if rel_path.startswith("app/") else [SUITE / rel_path])
        for f in files:
            for s in _operator_strings(f):
                for m in CLICK_RE.finditer(re.sub(r"<[^>]+>", "", s)):
                    phrase = m.group(1)
                    if not any(phrase == l or phrase.startswith(l + " ")
                               for l in labels):
                        problems.append(
                            "{}: {} tells the operator to click \"{}\", "
                            "and the page has no such button".format(
                                rel_path, f.relative_to(SUITE).as_posix(),
                                phrase.strip()))


TONE_PY_RE = re.compile(r"# ---- DC-TONE (\d+) -+\n.*?# ---- /DC-TONE -+\n", re.S)
TONE_JS_RE = re.compile(r"/\* DC-TONE (\d+):.*?/\* /DC-TONE \*/", re.S)


def check_tones(node_bin, group, problems):
    """DC-TONE 1 (1.0.1): green, amber, red for a finished run.

    - every log module carries one DC-TONE Python block and its page one
      DC-TONE script, and the copies agree;
    - EVERY set_state(phase="done", ...) in every module passes outcome=
      (ast, across all modules, not a list of call sites);
    - each page colors its status line with logTone(s) and no longer with
      a literal 'done' (a SOURCE check, said so - poll() needs a server);
    - logTone() and each module's outcome_tone()/run_warnings() behave.
    """
    import ast
    py, js = {}, {}
    for label, html in group:
        src = (SUITE / label).read_text(encoding="utf-8")
        b = _one_block(label, src, TONE_PY_RE, "DC-TONE Python", problems)
        if b is not None:
            py[label] = b
        b = _one_block(label, html, TONE_JS_RE, "DC-TONE script", problems)
        if b is not None:
            js[label] = b
            if "\\" in b:
                problems.append("{}: its DC-TONE script contains a "
                                "backslash".format(label))
        script = _strip_js_comments("\n".join(SCRIPT_RE.findall(html)))
        if "className=logTone(s)" not in script:
            problems.append("{}: its status line is not colored by "
                            "logTone(s)".format(label))
        if re.search(r"className\s*=\s*'done'|\?\s*'done'\s*:", script):
            problems.append("{}: still colors a finished run 'done' "
                            "whatever happened".format(label))
    _agree(py, "DC-TONE Python", problems)
    _agree(js, "DC-TONE script", problems)
    for path in sorted((SUITE / "modules").glob("dc_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for c in ast.walk(tree):
            if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "set_state"):
                continue
            kws = {k.arg: k.value for k in c.keywords}
            ph = kws.get("phase")
            if isinstance(ph, ast.Constant) and ph.value == "done" \
                    and "outcome" not in kws:
                problems.append("modules/{}:{}: set_state(phase=\"done\") "
                                "without outcome=".format(path.name, c.lineno))
    for label, _html in group:
        M = _import_module(label)
        quiet = M.log
        try:
            M.log = quiet
            with contextlib.redirect_stdout(io.StringIO()):
                M.begin_run_log()
                M.log("an ordinary line")
                M.log("a line that mentions a WARNING in passing")
                n0 = M.run_warnings()
                M.log("WARNING: something went wrong")
                M.log("  WARNING: indented, as a per-item line is")
                n2 = M.run_warnings()
                M.begin_run_log()
            got = (n0, n2, M.outcome_tone(0), M.outcome_tone(3, 1),
                   M.outcome_tone(3, 0), M.outcome_tone(0, 0),
                   M.outcome_tone(3))
            want = (0, 2, "green", "amber", "red", "green", "amber")
            if got != want:
                problems.append("{}: outcome_tone/run_warnings gave {} (want "
                                "{})".format(label, got, want))
        finally:
            M.log = quiet
    if not group or not node_bin:
        return
    block = TONE_JS_RE.search(group[0][1]).group(0)
    script = block + """
const cases = [null, {phase:'scraping'}, {phase:'idle'}, {phase:'error'},
  {phase:'done'}, {phase:'done', outcome:'green'},
  {phase:'done', outcome:'amber'}, {phase:'done', outcome:'red'},
  {phase:'error', outcome:'green'}];
console.log(cases.map(logTone).join(','));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(script)
        tmp = fh.name
    try:
        done = subprocess.run([node_bin, tmp], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    finally:
        Path(tmp).unlink()
    want = ",,,error,done,done,amber,error,error"
    if done.returncode != 0 or done.stdout.strip() != want:
        problems.append("DC-TONE script: logTone gave {!r}, expected {!r}"
                        .format(done.stdout.strip(), want))


FORM_PY_RE = re.compile(r"# ---- DC-FORM (\d+) -+\n.*?# ---- /DC-FORM -+\n", re.S)
FORM_JS_RE = re.compile(r"/\* DC-FORM (\d+):.*?/\* /DC-FORM \*/", re.S)
CONTROL_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.S | re.I)


def _form_value_for(html, fid):
    """A value of the right kind for one kept control: text for a text
    field or a choice (the first option), True for a box or a radio."""
    m = re.search(r'<(input|select|textarea)\b[^>]*\bid="{}"[^>]*>'
                  .format(re.escape(fid)), html)
    tag = m.group(0) if m else ""
    if re.search(r'type="(checkbox|radio)"', tag):
        return True
    if tag.startswith("<select"):
        return ""
    return "/kept/after/a/restart/" + fid


def check_forms(node_bin, group, problems):
    """DC-FORM 1 (1.0.1): the page's settings survive a restart.

    - every module that runs a job (the log group) carries one DC-FORM
      Python block and its page one DC-FORM script, and the copies agree;
    - every control on the page is either kept (FORM_FIELDS) or declared
      not kept (FORM_NOT_KEPT), found by markup, and every kept id exists;
    - the page is served its own FORM_FIELDS, and poll() calls formSeen(s);
    - driven: a form posted to one copy of the module is what a FRESH copy
      (a restart) hands the page; Clear keeps it; a field an older version
      wrote is dropped; an unreadable file is said and ignored;
    - the script restores fields, waits for a choice whose option has not
      loaded yet, and never saves before it has restored.
    """
    import threading
    from http.server import ThreadingHTTPServer
    py, js = {}, {}
    tmp = tempfile.mkdtemp(prefix="dcform-")
    try:
        for label, html in group:
            src = (SUITE / label).read_text(encoding="utf-8")
            b = _one_block(label, src, FORM_PY_RE, "DC-FORM Python", problems)
            if b is not None:
                py[label] = b
            b = _one_block(label, html, FORM_JS_RE, "DC-FORM script", problems)
            if b is not None:
                js[label] = b
                if "\\" in b:
                    problems.append("{}: its DC-FORM script contains a "
                                    "backslash".format(label))
            M = _import_module(label)
            kept = list(getattr(M, "FORM_FIELDS", ()))
            not_kept = list(getattr(M, "FORM_NOT_KEPT", ()))
            ids = []
            for t in CONTROL_RE.finditer(html):
                i = re.search(r'\bid="([^"]+)"', t.group(2))
                if i and not re.search(r'type="(file|hidden|button|submit)"',
                                       t.group(2)):
                    ids.append(i.group(1))
            stray = [i for i in ids if i not in kept and i not in not_kept]
            if stray:
                problems.append("{}: control(s) neither kept nor declared not "
                                "kept: {}".format(label, ", ".join(stray)))
            gone = [i for i in kept + not_kept if i not in ids]
            if gone:
                problems.append("{}: FORM_FIELDS/FORM_NOT_KEPT name(s) with no "
                                "control on the page: {}".format(
                                    label, ", ".join(gone)))
            served_page = M.build_page(M.load_session(
                Path("/nonexistent/session.json")))
            if isinstance(served_page, bytes):
                served_page = served_page.decode("utf-8")
            if "const FORM_IDS={};".format(json.dumps(kept)) \
                    not in served_page or "__FORM_FIELDS__" in served_page:
                problems.append("{}: the page is not served its own "
                                "FORM_FIELDS (verify the served bytes)"
                                .format(label))
            script = _strip_js_comments("\n".join(SCRIPT_RE.findall(html)))
            at = script.find("async function poll(")
            if at < 0 or "formSeen(s)" not in script[at:at + 4000]:
                problems.append("{}: poll() does not call formSeen(s)"
                                .format(label))
            if not kept:
                problems.append("{}: keeps no settings".format(label))
                continue
            # Driven through a restart.
            path = os.path.join(tmp, Path(label).stem + ".json")
            form = {"fields": {k: _form_value_for(html, k) for k in kept},
                    "parents": []}
            quiet = M.log
            said = []
            try:
                M.FORM_PATH = path
                M.log = lambda m, *a: said.append(str(m))
                session = M.load_session(Path("/nonexistent/session.json"))
                srv = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    M.make_handler(session, M.build_page(session)))
                port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever,
                                 daemon=True).start()
                try:
                    code, txt = _http(port, "/api/form", form)
                    if code != 200:
                        problems.append("{}: /api/form gave {} {}".format(
                            label, code, txt[:160]))
                    code, _ = _http(port, "/api/form",
                                    {"fields": {"no-such-field": "x"}})
                    if code != 400:
                        problems.append("{}: /api/form took an unknown field "
                                        "({})".format(label, code))
                finally:
                    srv.shutdown()
                    srv.server_close()
                M2 = _import_module(label)          # a restart
                M2.FORM_PATH = path
                M2.log = lambda m, *a: said.append(str(m))
                srv = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    M2.make_handler(session, M2.build_page(session)))
                port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever,
                                 daemon=True).start()
                try:
                    st = json.loads(_http(port, "/api/state")[1])
                    if st.get("form") != form:
                        problems.append("{}: after a restart the page was "
                                        "handed {!r}, not the form it saved"
                                        .format(label, st.get("form"))[:300])
                    _http(port, "/api/clear", {})
                    st = json.loads(_http(port, "/api/state")[1])
                    if st.get("form") != form:
                        problems.append("{}: Clear dropped the remembered "
                                        "form".format(label))
                finally:
                    srv.shutdown()
                    srv.server_close()
                older = dict(form, fields=dict(form["fields"],
                                               **{"retired-field": "x"}))
                Path(path).write_text(json.dumps(older), encoding="utf-8")
                if M2.load_form() != form:
                    problems.append("{}: a field an older version saved was "
                                    "not dropped".format(label))
                Path(path).write_text("{not json", encoding="utf-8")
                del said[:]
                if M2.load_form() is not None or not any(
                        "could not be read" in x for x in said):
                    problems.append("{}: an unreadable saved form was not "
                                    "ignored and said".format(label))
            finally:
                M.log = quiet
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _agree(py, "DC-FORM Python", problems)
    _agree(js, "DC-FORM script", problems)
    if not group or not node_bin:
        return
    block = FORM_JS_RE.search(group[0][1]).group(0)
    script = """
const FORM_IDS = ['dir', 'box', 'pick', 'r1', 'r2'];
const sent = [];
function fetch(u, o){ sent.push(JSON.parse(o.body)); return {then(){}}; }
function setTimeout(f){ f(); } function clearTimeout(){}
const els = {};
function mk(id, type, opts){ els[id] = {id:id, type:type, value:'', checked:false,
  options: opts, fired:0, listeners:{},
  addEventListener(t, f){ this.listeners[t] = f; },
  dispatchEvent(){ this.fired += 1; }}; }
mk('dir', 'text'); mk('box', 'checkbox'); mk('pick', 'select-one', []);
mk('r1', 'radio'); mk('r2', 'radio'); els.r1.checked = true;
const document = {getElementById: id => els[id] || null};
""" + block + """
saveForm(); const before = sent.length;
formSeen({form: {fields: {dir: '/x', box: true, pick: 'Alt', r1: false, r2: true}}});
const pendingAtFirst = els.pick.value;
els.pick.options.push({value: 'Archival'}, {value: 'Alt'});
formSeen({});
els.dir.listeners.input();
console.log(JSON.stringify([before, els.dir.value, els.box.checked,
  pendingAtFirst, els.pick.value, els.r1.checked, els.r2.checked,
  els.r2.fired, els.r1.fired, sent.length, sent[sent.length - 1].fields.pick]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(script)
        tmpjs = fh.name
    try:
        done = subprocess.run([node_bin, tmpjs], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=30)
    finally:
        Path(tmpjs).unlink()
    want = '[0,"/x",true,"","Alt",false,true,1,0,1,"Alt"]'
    if done.returncode != 0 or done.stdout.strip() != want:
        problems.append("DC-FORM script: behaved as {!r}, expected {!r}{}"
                        .format(done.stdout.strip(), want,
                                " - " + _node_error(done)
                                if done.returncode else ""))


FOLDER_PY_RE = re.compile(r"# ---- DC-FOLDER (\d+) -+\n.*?# ---- /DC-FOLDER -+\n",
                          re.S)
FOLDER_INPUT_RE = re.compile(r'<input\b[^>]*\bid="([A-Za-z0-9_-]*dir)"[^>]*>',
                             re.S)
# (module, endpoint, body, the label the page gives that field). Every body
# is INVALID on purpose - a blank or missing folder - so no worker starts.
FOLDER_DRIVES = (
    ("modules/dc_batch_revise.py", "/api/reports/scan",
     {"folder": "", "kind": "hier"}, "Reports folder"),
    ("modules/dc_batch_revise.py", "/api/reports/scan",
     {"folder": "", "kind": "queue"}, "Queues folder"),
    ("modules/dc_batch_revise.py", "/api/uploads/scan", {"folder": ""},
     "Folder of revised .xls files"),
    ("modules/dc_batch_revise.py", "/api/map", {"out_dir": ""},
     "Queue workbook folder"),
    ("modules/dc_batch_revise.py", "/api/start",
     {"workflow": "download", "out_dir": ""}, "Save reports to"),
    ("modules/dc_config_manager.py", "/api/scan",
     {"kind": "inventory", "folder": ""}, "Workbooks folder"),
    ("modules/dc_doi_xml.py", "/api/start", {"out_dir": "", "sets": []},
     "Output folder"),
    ("modules/dc_file_downloader.py", "/api/reports/scan", {"folder": ""},
     "Reports folder"),
    ("modules/dc_file_downloader.py", "/api/map", {"out_dir": ""},
     "Save downloads to"),
    ("modules/dc_file_downloader.py", "/api/map",
     {"out_dir": "__TMP__", "report_dir": "__TMP__/no-such-folder"},
     "Save reports to"),
    ("modules/dc_hierarchy_mapper.py", "/api/start", {"out_dir": ""},
     "Output folder"),
    ("modules/dc_image_describer.py", "/api/scan",
     {"mode": "folder", "folder": ""}, "Folder to scan"),
    ("modules/dc_ocr_toolkit.py", "/api/scan",
     {"mode": "run", "folder": ""}, "Downloader run folder"),
    ("modules/dc_regenerator.py", "/api/reports/scan", {"folder": ""},
     "Reports folder"),
    ("modules/dc_regenerator.py", "/api/map", {"out_dir": ""},
     "Output folder"),
    ("modules/dc_url_scraper.py", "/api/start",
     {"types": "__FIRST_TYPE__", "reports": "__FIRST_REPORT__",
      "out_dir": ""}, "Output folder"),
)


def check_folder_fields(served, problems):
    """DC-FOLDER 1 (1.0.1). No folder field carries a default, found by
    markup on every page rather than by a list; every module with one
    carries the one folder_error() block, the copies agree, and no other
    "folder does not exist" message survives in its source (ast, string
    literals only). Returns the modules that carry the block."""
    import ast
    group = []
    for label, html in served:
        defaulted = []
        for m in FOLDER_INPUT_RE.finditer(html):
            tag = m.group(0)
            if 'type="text"' not in tag:
                continue
            v = re.search(r'\bvalue="([^"]*)"', tag)
            if v and v.group(1).strip():
                defaulted.append("{}={!r}".format(m.group(1), v.group(1)))
        if defaulted:
            problems.append("{}: folder field(s) with a default: {}".format(
                label, ", ".join(defaulted)))
        if not label.startswith("modules/"):
            continue
        has_fields = bool(FOLDER_INPUT_RE.search(html))
        src = (SUITE / label).read_text(encoding="utf-8")
        blocks = FOLDER_PY_RE.findall(src)
        if has_fields and len(blocks) != 1:
            problems.append("{}: has folder fields and {} DC-FOLDER "
                            "block(s), not 1".format(label, len(blocks)))
            continue
        if not has_fields:
            continue
        group.append(label)
        tree = ast.parse(src)
        outside = []
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name != "folder_error"]:
            for c in ast.walk(fn):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                        and re.search(r"folder does not exist",
                                      c.value, re.I):
                    outside.append("{}:{}".format(fn.name, c.lineno))
        if outside:
            problems.append("{}: a folder message outside folder_error(): {}"
                            .format(label, ", ".join(sorted(set(outside)))))
    texts = {lb: FOLDER_PY_RE.search(
        (SUITE / lb).read_text(encoding="utf-8")).group(0) for lb in group}
    if len(set(texts.values())) > 1:
        first = group[0]
        for lb in group[1:]:
            if texts[lb] != texts[first]:
                problems.append("{}: its DC-FOLDER block differs from {}'s"
                                .format(lb, first))
    return group


def _fill_tmp(value, folder):
    """Put a real folder where a drive says __TMP__, in the STRUCTURE.

    The first version substituted into the JSON text, so the folder was
    spliced into a string literal unescaped. A Windows temp path
    (D:\\a\\...) then made an invalid escape and the whole check died
    on CI; on Linux a backslash would have turned "\\f" into a form
    feed and quietly tested a different folder. Strings are replaced
    as strings, so a path is never parsed as anything else.
    """
    if isinstance(value, str):
        return value.replace("__TMP__", folder)
    if isinstance(value, dict):
        return {k: _fill_tmp(v, folder) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_tmp(v, folder) for v in value]
    return value


def check_folder_fields_driven(group, problems):
    """Drive each module's server with a blank (or missing) folder and read
    the answer: a 400 that names the field the way its page labels it."""
    import threading
    from http.server import ThreadingHTTPServer
    # A BACKSLASH in the folder name wherever the platform allows one, so
    # a Linux run handles the character every Windows path is full of.
    # (On Windows it is the separator, and the temp path has plenty.)
    tmp = tempfile.mkdtemp(prefix="dc\\folder-" if os.sep == "/"
                           else "dcfolder-")
    covered = set()
    try:
        for label in group:
            M = _import_module(label)
            quiet = M.log
            M.log = lambda *_a, **_k: None
            ok_dir = os.path.join(tmp, "exists")
            os.makedirs(ok_dir, exist_ok=True)
            for lb, path, body, want in FOLDER_DRIVES:
                if lb != label:
                    continue
                covered.add(lb)
                b = _fill_tmp(body, ok_dir)
                if b.get("types") == "__FIRST_TYPE__":
                    b["types"] = [sorted(M.CODE_TO_LABEL)[0]]
                    b["reports"] = [sorted(M.REPORT_LABELS)[0]]
                session = M.load_session(Path("/nonexistent/session.json"))
                session["hub_url"] = "http://127.0.0.1:8750"
                srv = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    M.make_handler(session, M.build_page(session)))
                port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever,
                                 daemon=True).start()
                try:
                    code, txt = _http(port, path, b)
                finally:
                    srv.shutdown()
                    srv.server_close()
                quoted = "\u201c{}\u201d".format(want)
                try:
                    said = json.loads(txt).get("error", "")
                except ValueError:
                    said = txt
                if code != 400 or quoted not in said:
                    problems.append("{} {} with {}: gave {} {!r} (want 400 "
                                    "naming {})".format(label, path, body,
                                                        code, said[:160],
                                                        quoted))
            M.log = quiet
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for label in group:
        if label not in covered:
            problems.append("{}: has folder fields and no driven check in "
                            "FOLDER_DRIVES".format(label))


def main():
    node_bin = shutil.which("node")
    if not node_bin:
        print("node is not on PATH — install Node.js to run this check.")
        return 1

    problems = []
    served = []
    for rel_path, var_name in PAGE_SOURCES:
        label = rel_path
        html = page_html(rel_path, var_name)
        if html is None:
            problems.append("{}: no top-level {} string found"
                            .format(label, var_name))
            continue
        check_scripts(node_bin, label, html, problems)
        check_ids(label, html, problems)
        check_log_box(label, html, problems)
        check_hidden_stays_hidden(label, html, problems)
        check_callout_above_the_form(label, html, problems)
        check_dialogs_speak_for_themselves(label, html, problems)
        served.append((label, html))
    check_palette(served, problems)
    group = check_log_blocks(served, problems)
    check_poll_reports_state(group, problems)
    check_log_block_in_node(node_bin, group, problems)
    check_tones(node_bin, group, problems)
    with contextlib.redirect_stdout(io.StringIO()):
        check_forms(node_bin, group, problems)
    with contextlib.redirect_stdout(io.StringIO()):
        check_log_modules_driven(group, problems)
    check_run_folder_logs(problems)
    check_shell_steps_are_toned(node_bin, problems)
    check_shell_returns_to_a_running_module(problems)
    check_named_buttons_exist(problems)
    folders = check_folder_fields(served, problems)
    with contextlib.redirect_stdout(io.StringIO()):
        check_folder_fields_driven(folders, problems)

    print("Served pages — verification")
    print("  pages checked: {}".format(len(PAGE_SOURCES)))
    print("  problems: {}".format(len(problems)))
    for p in problems:
        print("  FAIL  " + p)
    if not problems:
        print("  every page parses, every id it uses exists in its markup,")
        print("  and every log renders through renderLog().")
        print("  Every page carries the one notification palette, and its")
        print("  inks meet AA contrast, amber included.")
        print("  Every module that runs a job keeps its page's settings")
        print("  across a restart (DC-FORM, driven), and every control on")
        print("  its page is either kept or declared not kept.")
        print("  Every finished run is colored by what happened: every")
        print("  set_state(phase=\"done\") passes outcome=, and every page")
        print("  shows it through logTone().")
        print("  The shell returns to a module that is already running.")
        print("  Every message callout sits above its page's form, and")
        print("  every dialog has a message region of its own.")
        print("  Every button a message tells the operator to click exists.")
        print("  No folder field has a default; {} module(s) carry the one"
              .format(len(folders)))
        print("  DC-FOLDER block, and each was driven with a blank folder")
        print("  and named the field the way its page labels it.")
        print("  {} module(s) with a log carry the one DC-LOG block; each was".format(len(group)))
        print("  driven: Copy log is the whole run, Clear is refused during")
        print("  a run and keeps what is loaded, and one start of many wins.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
