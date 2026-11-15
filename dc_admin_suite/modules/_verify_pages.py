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
import time
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


# ---------------------------------------------------------------------------
# The status line, driven (1.0.8). Each page's own poll() runs in node with
# the helpers it calls taken from the page; any other name it reaches for
# is a recording stub. A staff test found two pages whose status line kept
# saying what a run had been doing after it ended ("Harvesting…", and an
# idle line that never noticed a hierarchy had loaded): the idle branch
# wrote only when the line was empty, which it never is after a run.
# ---------------------------------------------------------------------------
STATUS_NODE = r"""
const LOG_NL = String.fromCharCode(10);
function esc(t){ return String(t).split('&').join('&amp;').split('<').join('&lt;')
  .split('>').join('&gt;'); }
function mkel(id){
  const props = {textContent:'', innerHTML:'', className:'', value:'',
    checked:false, disabled:false, hidden:false, length:0};
  let writes = 0;
  const target = function(){ return mkel(id + '()'); };
  return new Proxy(target, {
    get(t, k){
      if (k === '__writes') return writes;
      if (typeof k === 'symbol') return k === Symbol.toPrimitive ? (() => '[' + id + ']') : undefined;
      if (k === 'then') return undefined;
      if (k in props) return props[k];
      if (k === 'classList') return {add(){}, remove(){}, toggle(){}, contains(){ return false; }};
      if (k === 'style' || k === 'dataset') return (props[k] = {});
      if (k === 'options' || k === 'children' || k === 'childNodes') return (props[k] = []);
      return (props[k] = mkel(id + '.' + String(k)));
    },
    set(t, k, v){
      if (k === 'textContent' || k === 'innerHTML') writes += 1;
      props[k] = v;
      if (k === 'innerHTML') props.textContent = String(v).replace(/<[^>]*>/g, '')
        .split('&lt;').join('<').split('&gt;').join('>').split('&quot;').join('"')
        .split('&amp;').join('&');
      return true;
    },
    apply(){ return mkel(id + '()'); }
  });
}
const ELS = {};
const document = {getElementById(id){ return ELS[id] || (ELS[id] = mkel(id)); },
  createElement(t){ return mkel('<' + t + '>'); }, querySelectorAll(){ return []; },
  querySelector(){ return null; }, title: ''};
let STATE = null;
async function fetch(url){ return {ok: true, json: async () => STATE, text: async () => ''}; }
function setInterval(){} function setTimeout(){}
const status = document.getElementById('status');
"""

STATUS_DRIVE = r"""
const OUT = [];
(async () => {
  for (const st of STATES) {
    STATE = st;
    let err = null;
    for (let tries = 0; tries < 80; tries++) {
      try { await poll(); err = null; break; }
      catch (e) {
        const m = /^(\S+) is not defined/.exec(e.message || '');
        if (e instanceof ReferenceError && m) { globalThis[m[1]] = mkel(m[1]); err = e; continue; }
        err = e; break;
      }
    }
    OUT.push({text: String(status.textContent), html: String(status.innerHTML),
              cls: String(status.className), writes: status.__writes,
              error: err ? String(err) : ''});
  }
  console.log(JSON.stringify(OUT));
})();
"""

STATUS_SKIP = {"poll", "renderLog", "formSeen", "logStateSeen", "refreshHierarchy",
               "renderList", "renderUploads", "noteLoaded", "saveForm", "esc",
               "refetchFiles", "renderTools", "applyVerifyTitle", "syncGo",
               "renderEndpoints", "refreshTargets"}


def _status_script(html):
    """poll() and the page functions it reaches, for STATUS_NODE."""
    src = "\n".join(SCRIPT_RE.findall(html or ""))
    poll = _js_function(src, "poll")
    if poll is None:
        return None
    # A poll that swallows its own errors would hide what the stub lacks.
    poll = poll.replace("catch(e){/* transient poll errors are fine */}",
                        "catch(e){throw e;}")
    defined = set(re.findall(r"function ([A-Za-z_$][\w$]*)\(", src))
    take, todo, seen = [], [poll], set()
    while todo:
        body = todo.pop()
        for name in sorted(defined - seen - STATUS_SKIP):
            if re.search(r"(?<![\w$.])" + re.escape(name) + r"\(", body):
                seen.add(name)
                fn = _js_function(src, name)
                if fn:
                    at = src.find("function {}(".format(name))
                    if src[max(0, at - 6):at] == "async ":
                        fn = "async " + fn
                    take.append(fn)
                    todo.append(fn)
    consts = re.findall(r"^\s*const (?:cap|\$)=.*$", src, re.M)
    return "\n".join(consts) + "\n" + "\n".join(take) + "\nasync " + poll


def _status_base(**kw):
    base = {"log": [], "paused": False, "summary": "",
            "progress": {"current": 1, "total": 2, "msg": "m"},
            "hier": {"loaded": True, "count": 3, "max_level": 2,
                     "source": "s", "version": 1},
            "queue": {"loaded": False, "count": 0, "generated": "",
                      "source": "", "version": 0},
            "targets": {"loaded": False, "version": 0, "count": 0,
                        "source": "", "path": ""},
            "report": {"loaded": False, "version": 0, "count": 0,
                       "source": "", "path": ""},
            "plan": {"ready": False, "changes": 0, "structures": 0},
            "verify": {"holding": False, "left": 0, "prompts": 0},
            "files": [], "tools": {}, "types": {}, "inventory": False,
            "fetching": False}
    base.update(kw)
    return base


def _drive_status(node_bin, html, states):
    script = _status_script(html)
    if script is None:
        return None, "no poll()"
    code = (STATUS_NODE + script + "\nconst STATES = " + json.dumps(states)
            + ";\n" + STATUS_DRIVE)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(code)
        tmp = fh.name
    try:
        done = subprocess.run([node_bin, tmp], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    finally:
        Path(tmp).unlink()
    try:
        return json.loads(done.stdout.strip().splitlines()[-1]), ""
    except Exception:
        return None, _node_error(done) or done.stdout[-300:]


STATUS_FILES = {"last_file": "/r/a&b \"q\".xlsx", "report_file": "/r/h.xlsx",
                "plan_file": "/r/p.xlsx"}
STATUS_HREF = {"/download": "last_file", "/download-report": "report_file",
               "/download-plan": "plan_file"}


def check_status_lines(node_bin, group, problems):
    """Every page with a log, its poll() driven: after a run the idle line
    replaces what the run was doing; an idle poll that changes nothing
    writes nothing (the line is a live region); a finished run is colored
    by logTone(s); and every download link in the line carries the path of
    the file it downloads (data-path, for Copy status and log)."""
    checked = 0
    for label, html in group:
        states = [_status_base(phase="scraping"),
                  _status_base(phase="idle"),
                  _status_base(phase="idle"),
                  _status_base(phase="done", summary="S", outcome="amber",
                               **STATUS_FILES)]
        out, why = _drive_status(node_bin, html, states)
        if out is None:
            problems.append("{}: its poll() could not be driven ({})"
                            .format(label, why))
            continue
        checked += 1
        errs = [o["error"] for o in out if o["error"]]
        if errs:
            problems.append("{}: poll() raised {}".format(label, errs[0][:160]))
            continue
        run, idle, again, done = out
        if not idle["text"].startswith("Idle"):
            problems.append("{}: after a run its status line still says {!r}"
                            .format(label, idle["text"][:80]))
        if again["writes"] != idle["writes"]:
            problems.append("{}: an idle poll that changed nothing rewrote "
                            "the status line".format(label))
        if done["cls"] != "amber":
            problems.append("{}: a finished run with problems is colored {!r}"
                            .format(label, done["cls"]))
        for href, tail in re.findall(r'<a [^>]*href="([^"]+)"([^>]*)>',
                                     done["html"]):
            field = STATUS_HREF.get(href)
            if field is None:
                continue
            want = ('data-path="' + STATUS_FILES[field].replace("&", "&amp;")
                    .replace('"', "&quot;") + '"')
            if want not in tail:
                problems.append("{}: its {} link does not carry the file's "
                                "path ({!r})".format(label, href, tail[:80]))
    return checked


def check_download_phase_word(node_bin, group, problems):
    """The downloader says Planning until its first file request, then
    Downloading; an inventory says Inventorying."""
    html = dict(group).get("modules/dc_file_downloader.py")
    if html is None:
        problems.append("downloader: page not found for the phase word")
        return
    out, why = _drive_status(node_bin, html, [
        _status_base(phase="downloading", fetching=False),
        _status_base(phase="downloading", fetching=True),
        _status_base(phase="downloading", inventory=True)])
    if out is None:
        problems.append("downloader: phase word not driven ({})".format(why))
        return
    got = [o["text"].split("…")[0] for o in out]
    if got != ["Planning", "Downloading", "Inventorying"]:
        problems.append("downloader: the status line words a download run "
                        "{!r}".format(got))


def check_log_spacing(group, problems):
    """DC-LOG 2: the log bar keeps the same space above the log box as the
    status line keeps above the bar (1rem), read from the styles."""
    for label, html in group:
        m = LOG_CSS_RE.search(html)
        bar = re.search(r"\.logbar\{[^}]*margin-bottom:([^;}]+)", m.group(0)
                        if m else "")
        st = re.search(r"#status\{margin:([^ ;}]+)", html)
        if not bar or not st or bar.group(1).strip() != st.group(1).strip():
            problems.append("{}: the log bar's bottom margin ({}) is not the "
                            "status line's ({})".format(
                                label, bar.group(1) if bar else None,
                                st.group(1) if st else None))


LOG_COPY_PRE = r"""
function node(tag, kids, attrs, text){
  if (tag === '#text') return {nodeType: 3, nodeValue: text, childNodes: []};
  return {nodeType: 1, nodeName: tag, childNodes: kids || [],
    getAttribute(a){ return (attrs || {})[a] || null; },
    get textContent(){ return this.childNodes.map(c => c.nodeType === 3 ? c.nodeValue : c.textContent).join(''); }};
}
const els = {};
function el(id){ return els[id] || (els[id] = {id:id, hidden:true, disabled:false,
  value:'', className:'', _html:'', writes:0, childNodes:[],
  get innerHTML(){ return this._html; }, set innerHTML(v){ this._html = v; this.writes += 1; },
  listeners:{}, addEventListener(t,f){ this.listeners[t]=f; }, focus(){}, select(){},
  classList:{contains(){ return false; }}}); }
const document = {getElementById: el};
let SAID = '', CLIP = null, REJECT = false, LOG = '';
const navigator = {clipboard: {writeText(t){ CLIP = t; return REJECT ? Promise.reject(1) : Promise.resolve(); }}};
async function fetch(u){ return {ok: true, text: async () => LOG}; }
function clearErr(){}
function logSelectAll(){}
"""

LOG_COPY_DRIVE = r"""
logSay = function(m){ SAID = m; };
const tick = () => new Promise(r => setTimeout(r, 20));
const out = [];
(async () => {
  el('status').childNodes = [node('#text', null, null, 'Done: S '),
    node('BR'), node('A', [node('#text', null, null, 'Download workbook')], {'data-path': '/r/a.xlsx'}),
    node('BR'), node('A', [node('#text', null, null, 'Other')], {})];
  LOG = 'l1' + LOG_NL + 'l2' + LOG_NL;
  logStateSeen({phase: 'done'}); copyLog(); await tick();
  out.push(JSON.stringify(CLIP)); out.push(SAID);
  logStateSeen({phase: 'idle'}); CLIP = null; copyLog(); await tick();
  out.push(JSON.stringify(CLIP)); out.push(SAID);
  LOG = ''; CLIP = null; copyLog(); await tick(); out.push(String(CLIP) + '|' + SAID);
  logStateSeen({phase: 'done'}); copyLog(); await tick(); out.push(JSON.stringify(CLIP)); out.push(SAID);
  LOG = 'l1' + LOG_NL; REJECT = true; copyLog(); await tick();
  out.push(el('logfull').hidden + '|' + JSON.stringify(el('logfull').value)); out.push(SAID);
  logShow('', 'A'); logShow('', 'A'); logShow('amber', 'A');
  out.push(el('status').writes + '|' + el('status').className);
  out.push(logAttr('a&"<>'));
  console.log(out.join(LOG_NL));
})();
"""


def check_log_copy_in_node(node_bin, group, problems):
    """DC-LOG 2, driven: Copy status and log copies the status line (a link
    with a path as its label and path) and then the whole run; an idle line
    is not copied as a result; a page with no log yet copies the status
    alone and says so; the fallback box gets the same text; logShow()
    writes only what changed; logAttr() escapes for an attribute."""
    if not group:
        return
    block = LOG_JS_RE.search(group[0][1]).group(0)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(LOG_COPY_PRE + block + "\n" + LOG_COPY_DRIVE)
        tmp = fh.name
    try:
        done = subprocess.run([node_bin, tmp], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    finally:
        Path(tmp).unlink()
    got = done.stdout.rstrip("\n").split("\n")
    status = "Done: S" + "\n" + "Download workbook: /r/a.xlsx" + "\n" + "Other"
    want = [
        json.dumps("Status:\n" + status + "\n\nLog:\nl1\nl2\n"),
        "The status and the whole current run, 2 lines, copied to the clipboard.",
        json.dumps("l1\nl2\n"),
        "The whole current run, 2 lines, copied to the clipboard.",
        "null|Nothing has been logged since the last clean start.",
        json.dumps("Status:\n" + status + "\n"),
        "The status line copied to the clipboard. Nothing has been logged "
        "since the last clean start.",
        "false|" + json.dumps("Status:\n" + status + "\n\nLog:\nl1\n"),
        "The status and the whole current run (1 line) are selected below. "
        "Press Ctrl+C (Cmd+C) to copy.",
        "1|amber",
        "a&amp;&quot;&lt;&gt;"]
    if done.returncode != 0 or got != want:
        bad = [(i, g, w) for i, (g, w) in enumerate(zip(got, want)) if g != w]
        problems.append("DC-LOG copy: behaved as {!r}{}".format(
            bad[:2] or got[len(want):], " — " + _node_error(done)
            if done.returncode else ""))


SHELL_CMD_DRIVE = r"""
const ELS = {};
function $(id){ return ELS[id] || (ELS[id] = {innerHTML: ''}); }
const document = {querySelectorAll(){ return []; }};
const esc = t => String(t).split('&').join('&amp;').split('<').join('&lt;')
  .split('>').join('&gt;').split('"').join('&quot;');
"""


def check_chrome_commands(node_bin, problems):
    """The start page gives Windows staff a PowerShell command (where
    %LOCALAPPDATA% does not expand) and a Command Prompt one, each labeled
    with its own Copy button; macOS keeps its one."""
    sys.path.insert(0, str(SUITE))
    try:
        from app import chrome_bridge
    finally:
        sys.path.pop(0)
    ins = chrome_bridge.chrome_instructions(9222)
    cmds = {c["label"]: c["command"] for c in ins["windows"].get("commands", [])}
    ps, cmd = cmds.get("PowerShell", ""), cmds.get("Command Prompt", "")
    if not (ps.startswith('& "') and "$env:LOCALAPPDATA" in ps and "%" not in ps):
        problems.append("shell: the PowerShell command is {!r}".format(ps))
    if "%LOCALAPPDATA%" not in cmd or cmd.startswith("&"):
        problems.append("shell: the Command Prompt command is {!r}".format(cmd))
    html = page_html("app/ui.py", "APP_HTML")
    src = "\n".join(SCRIPT_RE.findall(html or ""))
    fn = _js_function(src, "renderInstructions")
    if fn is None:
        problems.append("shell: renderInstructions not found")
        return
    code = (SHELL_CMD_DRIVE + "const STATE = {instructions: " + json.dumps(ins)
            + "};\n" + fn + "\nrenderInstructions();\nconsole.log(JSON.stringify("
            "[ELS.winInstructions.innerHTML, ELS.macInstructions.innerHTML]));")
    done = subprocess.run([node_bin, "-e", code], capture_output=True,
                          text=True, encoding="utf-8", timeout=20)
    try:
        win, mac = json.loads(done.stdout.strip())
    except Exception:
        problems.append("shell: renderInstructions not driven ({})"
                        .format(_node_error(done)))
        return
    for want in ("PowerShell:", "Command Prompt:", "$env:LOCALAPPDATA",
                 "%LOCALAPPDATA%", ">Copy PowerShell command<",
                 ">Copy Command Prompt command<"):
        if want not in win:
            problems.append("shell: the Windows steps lack {!r}".format(want))
    if ">Copy command<" not in mac or "$HOME/DCAdminSuiteChrome" not in mac:
        problems.append("shell: the macOS command is not shown as before")


# Counts said as "N structures" where N can be 1. Site-wide counts (a whole
# hierarchy or listing) are left as they are.
COUNT_OK = ("Found {} structures", "{} structures mapped",
            "Harvested {} structures", "{} structures across {} type(s) harvested",
            "{} structures across {} sheet(s)", "' structures · deepest level",
            "===1?' structure · ':' structures · '")


def check_counts_of_one(problems):
    """No message says "1 structures": a count that can be one is written
    "structure(s)", or chooses its word. Found in every module's source."""
    for path in sorted((SUITE / "modules").glob("dc_*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8")
                                  .splitlines(), 1):
            for hit in re.findall(r"\{\} structures\b|' structures\b", line):
                if not any(ok in line for ok in COUNT_OK):
                    problems.append("{}:{}: a count that can be 1 says "
                                    "'structures': {}".format(
                                        path.name, no, line.strip()[:90]))


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
        if ("className=logTone(s)" not in script
                and "logShow(logTone(s)" not in script):
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


BROWSE_PY_RE = re.compile(r"# ---- DC-BROWSE (\d+) -+\n.*?# ---- /DC-BROWSE -+\n",
                          re.S)
BROWSE_JS_RE = re.compile(r"/\* DC-BROWSE (\d+):.*?/\* /DC-BROWSE \*/", re.S)
BROWSE_CSS_RE = re.compile(r"/\* DC-BROWSE (\d+) styles \*/.*?"
                           r"/\* /DC-BROWSE styles \*/", re.S)
PATH_TAG_RE = re.compile(r"<(input|textarea)\b([^>]*)>", re.S | re.I)
BROWSE_BTN_RE = re.compile(r"<button\b([^>]*\bdata-browse=[^>]*)>(.*?)</button>",
                           re.S | re.I)
# A field is a path field if its id ends in "dir" (the DC-FOLDER rule) or
# its label says it takes a folder, a path or a place to save. Found on
# every page by markup, never from a list.
PATH_LABEL_RE = re.compile(r"\bfolders?\b|\bpaths?\b|\bsave\b.*\bto\b", re.I)
BROWSE_WANT = {"folder": {"folder"}, "files": {"files"},
               "file": {"file"},     # one file (1.12.0: a record workbook)
               "file-or-folder": {"file", "folder"}}


def _attr(attrs, name):
    m = re.search(r'\b%s="([^"]*)"' % re.escape(name), attrs)
    return m.group(1) if m else None


def _label_text(html, fid):
    m = re.search(r'<label\b[^>]*\bfor="%s"[^>]*>(.*?)</label>'
                  % re.escape(fid), html, re.S)
    if not m:
        return ""
    import html as _h
    return " ".join(_h.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split())


def browse_fields(html):
    """{id: (data-path kind or None, label)} for every path field on a page."""
    found = {}
    for m in PATH_TAG_RE.finditer(html):
        tag, attrs = m.group(1).lower(), m.group(2)
        fid = _attr(attrs, "id")
        if not fid:
            continue
        if tag == "input" and (_attr(attrs, "type") or "text") != "text":
            continue
        label = _label_text(html, fid)
        kind = _attr(attrs, "data-path")
        if kind or fid.endswith("dir") or PATH_LABEL_RE.search(label):
            found[fid] = (kind, label)
    return found


def check_browse_markup(served, problems):
    """DC-BROWSE 1 (1.0.4). Every path field, found by markup, carries
    data-path and the button(s) its kind needs; every button names a path
    field, starts hidden, and is labelled for a screen reader with its
    field's label. Every page with one carries the one DC-BROWSE script,
    styles and Python block, and the copies agree. Returns the modules."""
    group, fields_seen = [], 0
    py, js, css = {}, {}, {}
    for label, html in served:
        fields = browse_fields(html)
        buttons = [(m.group(1), m.group(2)) for m in BROWSE_BTN_RE.finditer(html)]
        for fid, (kind, lab) in sorted(fields.items()):
            if kind not in BROWSE_WANT:
                problems.append("{}: path field {} ({!r}) has data-path={!r}"
                                .format(label, fid, lab, kind))
                continue
            kinds = {_attr(a, "data-browse") for a, _t in buttons
                     if _attr(a, "data-for") == fid}
            if kinds != BROWSE_WANT[kind]:
                problems.append("{}: path field {} ({!r}) has Browse kinds {} "
                                "(want {})".format(label, fid, lab,
                                                   sorted(k for k in kinds if k),
                                                   sorted(BROWSE_WANT[kind])))
            if 'id="{}-browsemsg"'.format(fid) not in html:
                problems.append("{}: path field {} has no message region"
                                .format(label, fid))
        for attrs, text in buttons:
            fid = _attr(attrs, "data-for")
            if fid not in fields or not fields[fid][0]:
                problems.append("{}: a Browse button names {!r}, which is not "
                                "a path field".format(label, fid))
                continue
            aria = _attr(attrs, "aria-label") or ""
            if fields[fid][1] not in aria:
                problems.append("{}: Browse button for {} is labelled {!r}, "
                                "not with its field's label".format(label, fid,
                                                                    aria))
            if _attr(attrs, "type") != "button" or not re.search(r"\shidden\b",
                                                                 attrs):
                problems.append("{}: Browse button for {} must be "
                                "type=\"button\" and start hidden"
                                .format(label, fid))
            if not text.strip():
                problems.append("{}: Browse button for {} has no visible text"
                                .format(label, fid))
        has = any(k for k, _l in fields.values())
        fields_seen += len(fields)
        if not has:
            if label.startswith("modules/") and "DC-BROWSE" in html:
                problems.append("{}: carries DC-BROWSE with no path field"
                                .format(label))
            continue
        if not label.startswith("modules/"):
            problems.append("{}: has path fields but is not a module".format(label))
            continue
        group.append(label)
        src = (SUITE / label).read_text(encoding="utf-8")
        py[label] = _one_block(label, src, BROWSE_PY_RE, "DC-BROWSE Python", problems)
        js[label] = _one_block(label, html, BROWSE_JS_RE, "DC-BROWSE script", problems)
        css[label] = _one_block(label, html, BROWSE_CSS_RE, "DC-BROWSE styles", problems)
    for blocks, what in ((py, "DC-BROWSE Python"), (js, "DC-BROWSE script"),
                         (css, "DC-BROWSE styles")):
        _agree({k: v for k, v in blocks.items() if v}, what, problems)
    return group, fields_seen


def _http_h(port, path, body=None, headers=None, timeout=10):
    import urllib.request
    import urllib.error
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:{}{}".format(port, path),
                                 data=data,
                                 method="GET" if body is None else "POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError) as e:
        # No answer at all (a request left waiting) is reported, not raised.
        return None, {"error": "no answer: {}".format(e)}


class _FakeRun:
    """Stands in for subprocess.run inside pick_paths()."""
    def __init__(self, rc=0, out=b"", err=b"", exc=None):
        self.rc, self.out, self.err, self.exc = rc, out, err, exc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc is not None:
            raise self.exc
        import types
        return types.SimpleNamespace(returncode=self.rc, stdout=self.out,
                                     stderr=self.err)


def check_browse_python(M, label, problems):
    """The pure parts of one module's DC-BROWSE block, driven directly."""
    import subprocess
    tmp = tempfile.mkdtemp(prefix="dcbrowse-")
    try:
        home = os.path.expanduser("~")
        sub = os.path.join(tmp, "a folder")
        os.makedirs(sub)
        afile = os.path.join(sub, "report.xlsx")
        open(afile, "w").close()
        cases = [("", home), ("   ", home), ("relative/x", home),
                 (sub, sub), (afile, sub),
                 (os.path.join(sub, "not", "yet"), sub),
                 (os.path.join(tmp, "missing"), tmp)]
        for value, want in cases:
            got = M.start_folder(value)
            if got != want:
                problems.append("{}: start_folder({!r}) = {!r}, want {!r}"
                                .format(label, value, got, want))
        for kind in ("folder", "file", "files"):
            if M.browse_kind_error(kind):
                problems.append("{}: browse_kind_error refuses {!r}".format(label, kind))
        for kind in ("dir", "", None, "FOLDER"):
            if not M.browse_kind_error(kind):
                problems.append("{}: browse_kind_error accepts {!r}".format(label, kind))
        nasty = os.path.join(tmp, 'it\'s "quoted"; $(x) `y`')
        title = 'Title "with" \'quotes\''
        argv, env, stdin = M.browse_command("osascript", "folder", nasty, title)
        if argv[:2] != ["osascript", "-"] or argv[2:] != [nasty, title, "folder"] \
                or nasty.encode("utf-8") in (stdin or b""):
            problems.append("{}: osascript command does not pass the start "
                            "folder as an argument only: {!r}".format(label, argv))
        for mech in ("powershell", "tkinter"):
            argv, env, stdin = M.browse_command(mech, "files", nasty, title)
            if any(nasty in a or title in a for a in argv) or \
                    env.get("DC_BROWSE_START") != nasty or \
                    env.get("DC_BROWSE_TITLE") != title or \
                    env.get("DC_BROWSE_KIND") != "files":
                problems.append("{}: {} command puts the start folder or "
                                "title in its arguments".format(label, mech))
        ps = M._BROWSE_PS
        # The Windows dialog cannot be shown here; what CAN be checked is
        # the script's shape (behavioral proof is a person on Windows):
        # the owner is a shown topmost window before the dialog opens, and
        # the page's values arrive only through the environment.
        at_show, at_pick = ps.find("$owner.Show()"), ps.find("[DcPicker]::Pick(")
        if not (0 <= at_show < at_pick) or "$owner.TopMost = $true" not in ps \
                or "$env:DC_BROWSE_START" not in ps.split("# COMPILE-END")[-1]:
            problems.append("{}: the Windows dialog is not owned by a shown "
                            "topmost window, or does not read the start "
                            "folder from the environment".format(label))
        if "# COMPILE-START" not in ps or "# COMPILE-END" not in ps:
            problems.append("{}: the Windows dialog script has lost its "
                            "COMPILE markers".format(label))
        real = subprocess.run
        runs = [
            ("osascript", "folder", _FakeRun(out=b"/Users/x/A Folder/\n"),
             {"paths": ["/Users/x/A Folder"]}),
            ("osascript", "files", _FakeRun(out=b"/a/1.jpg\n/a/2.jpg\n"),
             {"paths": ["/a/1.jpg", "/a/2.jpg"]}),
            ("powershell", "files", _FakeRun(out="C:\\a\\1.jpg\r\nC:\\a\\é.jpg"
                                             .encode("utf-8")),
             {"paths": ["C:\\a\\1.jpg", "C:\\a\\é.jpg"]}),
            ("tkinter", "folder", _FakeRun(out=b"/one\n/two"),
             {"paths": ["/one"]}),
            ("powershell", "folder", _FakeRun(rc=3), {"cancelled": True}),
            ("osascript", "file", _FakeRun(rc=1, err=b"execution error: User "
                                           b"canceled. (-128)"),
             {"cancelled": True}),
            ("tkinter", "file", _FakeRun(out=b"  \n"), {"cancelled": True}),
        ]
        try:
            for mech, kind, fake, want in runs:
                subprocess.run = fake
                got = M.pick_paths(mech, kind, tmp, "T")
                if got != want:
                    problems.append("{}: pick_paths({}, {}) = {!r}, want {!r}"
                                    .format(label, mech, kind, got, want))
                if fake.calls and fake.calls[0][1].get("timeout") != M.BROWSE_TIMEOUT:
                    problems.append("{}: pick_paths runs without its timeout"
                                    .format(label))
            for fake, word in ((_FakeRun(rc=1, err=b"boom\nlast line"), "last line"),
                               (_FakeRun(exc=subprocess.TimeoutExpired("x", 1)),
                                "unchanged"),
                               (_FakeRun(exc=OSError("no such program")),
                                "no such program")):
                subprocess.run = fake
                got = M.pick_paths("osascript", "folder", tmp, "T")
                if "error" not in got or word not in got["error"]:
                    problems.append("{}: pick_paths on a failure said {!r} "
                                    "(want an error naming {!r})"
                                    .format(label, got, word))
        finally:
            subprocess.run = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_browse_windows_compile(group, problems):
    """On Windows only: compile the dialog's C# exactly as the module
    would (the part between the COMPILE markers) and confirm the type
    exists, without showing anything. Elsewhere it cannot run, and the
    summary says so. Returns what happened, for the summary."""
    import base64
    if os.name != "nt":
        return "not compiled (not Windows; CI's Windows jobs compile it)"
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        problems.append("Windows, but no PowerShell found to compile the "
                        "dialog's C#")
        return "not compiled (no PowerShell)"
    if not group:
        return "not compiled (no module carries DC-BROWSE)"
    M = _import_module(group[0])
    ps = M._BROWSE_PS
    part = ps.split("# COMPILE-START")[1].split("# COMPILE-END")[0]
    script = ("$ErrorActionPreference = 'Stop'\n" + part
              + "\nif ([DcPicker].GetMethod('Pick')) { 'COMPILED' }\n")
    code = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                               "-EncodedCommand", code],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        problems.append("the dialog's C# could not be compiled: {}".format(e))
        return "not compiled"
    said = done.stdout.decode("utf-8", "replace")
    if done.returncode != 0 or "COMPILED" not in said:
        problems.append("the Windows dialog's C# does not compile: {}".format(
            done.stderr.decode("utf-8", "replace")[-400:]))
        return "compile FAILED"
    return "compiled with " + os.path.basename(exe)


def check_browse_driven(group, problems):
    """Each module's /api/browse, driven through its own server with the
    dialog faked: the mechanism is reported, a choice is returned, the
    start folder is the field's, a bad kind and a missing dialog are
    refused without opening anything, a non-local request is refused
    first, and a second dialog waits for the first."""
    import threading
    from http.server import ThreadingHTTPServer
    for label in group:
        M = _import_module(label)
        quiet = M.log
        M.log = lambda *_a, **_k: None
        check_browse_python(M, label, problems)
        calls = []
        mode = {"mech": "osascript", "answer": {"paths": ["/chosen"]},
                "gate": None}

        def fake_pick(mech, kind, start, title):
            calls.append((mech, kind, start, title))
            if mode["gate"] is not None:
                mode["gate"].wait(10)
            return mode["answer"]
        real_pick, real_mech = M.pick_paths, M.browse_mechanism
        M.pick_paths = fake_pick
        M.browse_mechanism = lambda: mode["mech"]
        session = M.load_session(Path("/nonexistent/session.json"))
        session["hub_url"] = "http://127.0.0.1:8750"
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  M.make_handler(session, M.build_page(session)))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="dcbrowse-")
        try:
            def say(what, ok):
                if not ok:
                    problems.append("{} /api/browse: {}".format(label, what))
            code, j = _http_h(port, "/api/browse")
            say("GET gave {} {!r}".format(code, j),
                code == 200 and j.get("mechanism") == "osascript")
            code, j = _http_h(port, "/api/browse",
                              {"kind": "folder", "title": "Reports folder",
                               "start": os.path.join(tmp, "gone", "deeper")})
            say("a choice gave {} {!r}".format(code, j),
                code == 200 and j == {"paths": ["/chosen"]})
            say("the dialog was asked {!r}".format(calls[-1:]),
                calls[-1:] == [("osascript", "folder", tmp, "Reports folder")])
            mode["answer"] = {"cancelled": True}
            code, j = _http_h(port, "/api/browse", {"kind": "files"})
            say("a cancel gave {} {!r}".format(code, j),
                code == 200 and j == {"cancelled": True})
            n = len(calls)
            code, j = _http_h(port, "/api/browse", {"kind": "nonsense"})
            say("a bad kind gave {} {!r}".format(code, j),
                code == 400 and "nonsense" in j.get("error", "") and len(calls) == n)
            for verb, body in (("GET", None), ("POST", {"kind": "folder"})):
                code, j = _http_h(port, "/api/browse", body,
                                  {"Origin": "http://elsewhere.example"})
                say("a non-local {} gave {}".format(verb, code),
                    code == 403 and len(calls) == n)
            mode["mech"] = ""
            code, j = _http_h(port, "/api/browse")
            say("GET with no dialog gave {!r}".format(j),
                code == 200 and j.get("mechanism") == "" and j.get("none"))
            code, j = _http_h(port, "/api/browse", {"kind": "folder"})
            say("POST with no dialog gave {} {!r}".format(code, j),
                code == 409 and len(calls) == n)
            mode["mech"] = "osascript"
            mode["answer"] = {"paths": ["/first"]}
            mode["gate"] = threading.Event()
            first = {}
            t = threading.Thread(target=lambda: first.update(
                r=_http_h(port, "/api/browse", {"kind": "folder"})))
            t.start()
            for _ in range(200):
                if len(calls) > n:
                    break
                time.sleep(0.01)
            code, j = _http_h(port, "/api/browse", {"kind": "folder"},
                              timeout=3)
            say("a second dialog while one is open gave {} {!r}".format(code, j),
                code == 409 and "already open" in j.get("error", ""))
            mode["gate"].set()
            t.join(10)
            say("the first dialog then gave {!r}".format(first.get("r")),
                first.get("r") == (200, {"paths": ["/first"]}))
        finally:
            srv.shutdown()
            srv.server_close()
            shutil.rmtree(tmp, ignore_errors=True)
            M.pick_paths, M.browse_mechanism = real_pick, real_mech
            M.log = quiet


BROWSE_NODE = r"""
const BLOCK = process.argv[2], FIELDS = JSON.parse(process.argv[3]);
const els = {}, buttons = [], posts = [], fired = [];
let replies = [], getReply = {mechanism: 'osascript', none: ''};
function El(id, tag){ this.id = id; this.tagName = tag; this.value = '';
  this.disabled = false; this.readOnly = false; this.hidden = false;
  this.textContent = ''; this.className = ''; this.ls = {}; this.attrs = {}; }
El.prototype.getAttribute = function(n){ return this.attrs[n] === undefined ? null : this.attrs[n]; };
El.prototype.addEventListener = function(t, f){ (this.ls[t] = this.ls[t] || []).push(f); };
El.prototype.dispatchEvent = function(e){ fired.push(this.id + ':' + e.type); (this.ls[e.type] || []).forEach(f => f(e)); };
El.prototype.focus = function(){ fired.push(this.id + ':focus'); };
FIELDS.forEach(f => {
  els[f.id] = new El(f.id, f.tag);
  els[f.id + '-browsemsg'] = new El(f.id + '-browsemsg', 'span');
  f.buttons.forEach((b, i) => { const e = new El('', 'button');
    e.attrs = {'data-browse': b.kind, 'data-for': f.id, 'data-title': b.title};
    e.textContent = b.text; e.hidden = true; buttons.push(e); });
});
global.Event = function(t){ this.type = t; };
global.document = { getElementById: id => els[id] || null,
  querySelectorAll: sel => sel === 'button[data-browse]' ? buttons.slice() : [] };
global.setInterval = () => 0;
global.fetch = (url, opt) => {
  if(!opt || !opt.method) return Promise.resolve({json: () => Promise.resolve(getReply)});
  posts.push(JSON.parse(opt.body));
  const r = replies.shift();
  if(r === 'THROW') return Promise.reject(new Error('down'));
  return Promise.resolve({json: () => Promise.resolve(r)});
};
const out = {problems: []};
const bad = m => out.problems.push(m);
const tick = () => new Promise(r => setTimeout(r, 0));
(async () => {
  eval(BLOCK);
  await tick(); await tick();
  if(buttons.some(b => b.hidden)) bad('buttons still hidden with a dialog available');
  if(browseJoin(' /a ' + BROWSE_NL + BROWSE_NL + '/b', ['/b', '/c']) !== ['/a', '/b', '/c'].join(BROWSE_NL))
    bad('browseJoin does not append and skip duplicates');
  for(const btn of buttons){
    const f = els[btn.getAttribute('data-for')], kind = btn.getAttribute('data-browse');
    const msg = els[f.id + '-browsemsg'];
    f.value = kind === 'files' ? ['/old/1.jpg', '/old/2.jpg'].join(BROWSE_NL) : '/old/place';
    fired.length = 0; posts.length = 0;
    replies = [{paths: kind === 'files' ? ['/old/2.jpg', '/new/3.jpg'] : ['/picked/A b']}];
    const label = btn.textContent;
    const p = browseClick(btn);
    if(btn.textContent === label || !btn.disabled) bad(f.id + ': the button does not show it is waiting');
    await p;
    const want = kind === 'files' ? ['/old/1.jpg', '/old/2.jpg', '/new/3.jpg'].join(BROWSE_NL) : '/picked/A b';
    if(f.value !== want) bad(f.id + ' (' + kind + '): value ' + JSON.stringify(f.value));
    if(fired.indexOf(f.id + ':input') < 0 || fired.indexOf(f.id + ':change') < 0)
      bad(f.id + ': input/change not fired: ' + fired.join(','));
    if(!posts[0] || posts[0].kind !== kind || posts[0].title !== btn.getAttribute('data-title')
       || posts[0].start !== (kind === 'files' ? '/old/2.jpg' : '/old/place'))
      bad(f.id + ': posted ' + JSON.stringify(posts[0]));
    if(kind === 'files' && msg.textContent !== '1 path added; 1 already listed.')
      bad(f.id + ': files message ' + JSON.stringify(msg.textContent));
    if(btn.textContent !== label || btn.disabled) bad(f.id + ': button not restored');
    const keep = f.value;
    replies = [{cancelled: true}];
    await browseClick(btn);
    if(f.value !== keep || msg.textContent.indexOf('Nothing was chosen') !== 0) bad(f.id + ': cancel changed something');
    replies = [{error: 'The osascript dialog ended with status 1: x'}];
    await browseClick(btn);
    if(f.value !== keep || msg.textContent.indexOf('status 1') < 0 || msg.className.indexOf('dc-bad') < 0)
      bad(f.id + ': error not shown: ' + msg.textContent + ' / ' + msg.className);
    replies = ['THROW'];
    await browseClick(btn);
    if(msg.className.indexOf('dc-bad') < 0 || btn.disabled) bad(f.id + ': a dead module not reported');
    f.disabled = true; posts.length = 0;
    await browseClick(btn); browseSync();
    if(posts.length || !btn.disabled) bad(f.id + ': a disabled field still opens a dialog');
    f.disabled = false; browseSync();
  }
  getReply = {mechanism: '', none: 'NO DIALOG HERE'};
  buttons.forEach(b => b.hidden = false);
  browseState.mechanism = 'x';
  browseInit(); await tick(); await tick();
  if(buttons.some(b => !b.hidden)) bad('buttons shown with no dialog');
  const first = els[buttons[0].getAttribute('data-for') + '-browsemsg'];
  if(first.textContent !== 'NO DIALOG HERE') bad('no-dialog reason not said: ' + JSON.stringify(first.textContent));
  console.log(JSON.stringify(out));
})().catch(e => { console.log(JSON.stringify({problems: ['threw: ' + e.stack]})); });
"""


def check_browse_in_node(node_bin, served, group, problems):
    """Each page's own DC-BROWSE script, run in node against a stub of its
    path fields and buttons (from its markup): a choice fills the field and
    fires input and change, a path list gains files without duplicates, a
    cancel or an error changes nothing and says so, a disabled field opens
    nothing, and with no dialog the buttons hide and the reason is said."""
    import html as _h
    pages = dict(served)
    for label in group:
        html = pages[label]
        m = BROWSE_JS_RE.search(html)
        if not m:
            continue
        fields = []
        for fid, (kind, lab) in sorted(browse_fields(html).items()):
            if not kind:
                continue
            tag = re.search(r"<(input|textarea)\b[^>]*\bid=\"%s\"" % fid, html).group(1)
            bts = [{"kind": _attr(a, "data-browse"), "title": _h.unescape(_attr(a, "data-title") or ""),
                    "text": _h.unescape(t.strip())}
                   for a, t in BROWSE_BTN_RE.findall(html) if _attr(a, "data-for") == fid]
            fields.append({"id": fid, "tag": tag, "buttons": bts})
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(BROWSE_NODE)
            script = fh.name
        try:
            done = subprocess.run([node_bin, script, m.group(0), json.dumps(fields)],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(script)
        text = done.stdout.decode("utf-8", "replace").strip()
        try:
            res = json.loads(text.splitlines()[-1])
        except (ValueError, IndexError):
            problems.append("{}: DC-BROWSE node run said {!r} {!r}".format(
                label, text[-300:], done.stderr.decode("utf-8", "replace")[-300:]))
            continue
        for p in res["problems"]:
            problems.append("{}: DC-BROWSE in node: {}".format(label, p))



# ---------------------------------------------------------------------------
# DC-PICK 1 (suite 1.0.5). A page that reopens with parents already checked
# said only how many; an operator could not see which, nor clear them
# without searching for each. Every page with a parent picker — found by
# its markup, not by a list — carries the one block: the checked parents
# by name, each with Remove, and Clear all. Agreement first; then each
# page's OWN renderList() and updateCount() are run in node with the block,
# because a block that agrees and is never called is the adjacent check.
# ---------------------------------------------------------------------------
PICK_JS_RE = re.compile(r"/\* DC-PICK (\d+):.*?/\* /DC-PICK \*/", re.S)
PICK_HTML_RE = re.compile(r"<!-- DC-PICK (\d+):.*?<!-- /DC-PICK -->", re.S)
PICK_CSS_RE = re.compile(r"/\* DC-PICK (\d+) styles \*/.*?/\* /DC-PICK styles \*/",
                         re.S)
PICKER_MARK = 'aria-label="Parent structures"'


def pick_pages(served):
    """The group: every served page with a parent-structure picker."""
    return [(label, html) for label, html in served if PICKER_MARK in html]


def check_pick_blocks(served, problems):
    group = pick_pages(served)
    if not group:
        problems.append("DC-PICK: no page carries a parent picker, so "
                        "nothing was checked")
        return group
    js, mk, css = {}, {}, {}
    for label, html in group:
        for store, rx, what in ((js, PICK_JS_RE, "DC-PICK script"),
                                (mk, PICK_HTML_RE, "DC-PICK markup"),
                                (css, PICK_CSS_RE, "DC-PICK styles")):
            block = _one_block(label, html, rx, what, problems)
            if block is not None:
                store[label] = block
                if "\\" in block:
                    problems.append("{}: its {} contains a backslash"
                                    .format(label, what))
        if label in mk and 'id="selcount"' not in mk[label]:
            problems.append("{}: its count line is not inside DC-PICK"
                            .format(label))
    for store, what in ((js, "DC-PICK script"), (mk, "DC-PICK markup"),
                        (css, "DC-PICK styles")):
        _agree(store, what, problems)
    return group


def _js_function(src, name):
    """The text of `function name(){...}` in src, by brace matching."""
    at = src.find("function {}(".format(name))
    if at < 0:
        return None
    depth, i = 0, src.index("{", at)
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[at:j + 1]
    return None


PICK_NODE = r"""
function mk(tag,id){ return {tag:tag,id:id||'',children:[],hidden:false,disabled:false,
  value:'',textContent:'',attrs:{},listeners:{},style:{},className:'',
  set innerHTML(v){ this.children=[]; }, get innerHTML(){ return ''; },
  append(...k){ this.children.push(...k); },
  setAttribute(a,v){ this.attrs[a]=v; },
  addEventListener(t,f){ this.listeners[t]=f; },
  focus(){ FOCUS=this; },
  querySelectorAll(t){ const o=[]; (function w(e){ e.children.forEach(c=>{ if(c.tag===t) o.push(c); w(c); }); })(this); return o; }}; }
let FOCUS=null, SAVES=0;
const els={};
const document={getElementById(id){ return els[id]||(els[id]=mk('x',id)); },
  createElement(t){ return mk(t); }};
els.picked=mk('ul','picked'); els.pickclear=mk('button','pickclear');
const filter=document.getElementById('filter'), list=document.getElementById('list'),
      selcount=document.getElementById('selcount');
function saveForm(){ SAVES+=1; }
let nodes=[{ctx:'b',type:'ETD',level:2,desc:0},{ctx:'a',type:'ETD',level:2,desc:0},
           {ctx:'c',type:'ETD',level:2,desc:0}];
let checked=new Set(['c','a']);
"""

PICK_DRIVE = r"""
const out=[];
const names=()=>els.picked.children.map(li=>li.children[0].textContent).join(',');
renderList();
out.push('shown='+names()+' clear='+(els.pickclear.disabled?'off':'on')+' hidden='+els.picked.hidden+' count='+selcount.textContent);
const rm=els.picked.querySelectorAll('button').find(b=>b.attrs['aria-label']==='Remove a');
rm.listeners.click();
out.push('removed='+names()+' left='+[...checked].join(',')+' focus='+(FOCUS&&FOCUS.attrs['aria-label']));
const before=SAVES;
els.pickclear.listeners.click();
out.push('cleared='+checked.size+' hidden='+els.picked.hidden+' clear='+(els.pickclear.disabled?'off':'on')+' count='+selcount.textContent+' focus='+(FOCUS&&FOCUS.id)+' saved='+(SAVES>before));
console.log(out.join(' | '));
"""


def check_pick_in_node(node_bin, group, problems):
    """Behavioral: each page's own renderList() and updateCount() with its
    DC-PICK script, against a stub page. Two parents restored are listed by
    name; Remove takes one off and moves focus to the next; Clear all
    empties the set, hides the list, disables itself, updates the count,
    returns focus to the filter, and — where the page keeps its settings —
    saves the emptied set."""
    for label, html in group:
        page_js = "\n".join(SCRIPT_RE.findall(html))
        block = PICK_JS_RE.search(html)
        fns = [_js_function(page_js, n) for n in ("renderList", "updateCount")]
        if block is None or None in fns:
            problems.append("{}: DC-PICK not driven — missing {}".format(
                label, "the block" if block is None else "renderList/updateCount"))
            continue
        saves = "saveForm" in fns[1]
        js = PICK_NODE + block.group(0) + "\n" + "\n".join(fns) + PICK_DRIVE
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
        want = ("shown=a,c clear=on hidden=false count=2 parent(s) checked. | "
                "removed=c left=c focus=Remove c | "
                "cleared=0 hidden=true clear=off count=0 parent(s) checked. "
                "focus=filter saved={}".format("true" if saves else "false"))
        if done.returncode != 0 or done.stdout.strip() != want:
            problems.append("{}: DC-PICK behaved as {!r}, expected {!r}{}"
                            .format(label, done.stdout.strip(), want,
                                    " — " + _node_error(done)
                                    if done.returncode else ""))


INDEX_RE = re.compile(r"# ---- DC-INDEX (\d+) -+\n.*?# ---- /DC-INDEX -+\n",
                      re.S)


def _index_page(rows, caption="Site index", title="Configuration Tools"):
    """A listing page in the markup Digital Commons serves: one
    <table class="cttypes">, a <th> per row, links in spans with an
    aria-hidden separator. Slugs are invented."""
    body = []
    for header, slugs in rows:
        items = "".join(
            '<span class="context-index-item"><a href="/cgi/user_config.cgi'
            '?context={0}">{0}</a><span class="context-index-separator" '
            'aria-hidden="true">|</span></span>\n'.format(s) for s in slugs)
        body.append("<tr>\n<th>{}</th>\n<td>\n{}</td>\n</tr>".format(header,
                                                                    items))
    return ('<html><head><title>{}</title></head><body>'
            '<table class="cttypes search"><tr><td>Jump to publication'
            '</td></tr></table>'
            '<table class="cttypes">\n<caption role="heading" aria-level="3">'
            '{}</caption>\n<tbody>{}</tbody></table></body></html>'
            .format(title, caption, "\n".join(body)))


# Each row: the newer heading, the older code, its slugs. The ETD row also
# carries a sub-document, the platform itself and a repeat, all skipped.
INDEX_ROWS = [
    ("Communities", "ir_community", ["example-community"]),
    ("Journals", "ir_journal", ["example-journal-a", "example-journal-b"]),
    ("ETDs", "ir_etd", ["example-etd", "example-etd/sub",
                        "https://dc.example.edu", "example-etd"]),
    ("Galleries", "ir_gallery", ["example-gallery"]),
    ("Series", "ir_series", ["example-series"]),
    ("Books", "ir_book", ["example-book"]),
    ("Conferences", "ir_event_community", ["example-event"]),
]
INDEX_WANT = {"example-community": "ir_community",
              "example-journal-a": "ir_journal",
              "example-journal-b": "ir_journal", "example-etd": "ir_etd",
              "example-gallery": "ir_gallery", "example-series": "ir_series",
              "example-book": "ir_book", "example-event": "ir_event_community"}

# How each listing reader is driven through its worker. A module that
# reads the listing and is not here is reported, never skipped.
INDEX_WORKERS = {
    "modules/dc_hierarchy_mapper.py": ("crawl_worker", ("session", "tmp")),
    "modules/dc_regenerator.py": ("map_worker", ("session", "tmp")),
    "modules/dc_batch_revise.py": ("map_worker", ("session", "tmp")),
    "modules/dc_config_manager.py": ("harvest_worker", ("session",)),
    "modules/dc_url_scraper.py": ("scrape_worker",
                                  ("session", "codes", "reports", "tmp")),
    "modules/dc_file_downloader.py": ("map_worker", ("session", "tmp")),
}


class _IndexHalt(Exception):
    """Raised by the fake driver past the listing: the check is about the
    listing, so the worker is stopped at its next page."""


class _IndexDriver:
    def __init__(self, html):
        self.page_source, self.gets = html, 0
        self.current_url = ("https://dc.example.edu/cgi/user_config.cgi"
                            "?context=https://dc.example.edu&x_showall=1")
        self.title = "Configuration Tools"

    def get(self, url):
        self.gets += 1
        if self.gets > 1:
            raise _IndexHalt(url)

    def __getattr__(self, name):
        raise _IndexHalt(name)


def _index_import(label):
    import importlib.util
    path = SUITE / label
    name = "_dcindex_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _index_by_code(M, label, html):
    """{slug: code} from one module's extract_contexts, whatever its shape."""
    to_code = {lab: code for lab, code in M.STRUCTURE_TYPES}
    if label.endswith("dc_url_scraper.py"):
        r = M.extract_contexts(html, [c for _l, c in M.STRUCTURE_TYPES])
        return None if r is None else {s: c for c, v in r.items() for s in v}
    r = M.extract_contexts(html)
    if r is None:
        return None
    if label.endswith("dc_config_manager.py"):
        return {s: to_code.get(lab, lab) for lab, v in r.items() for s in v}
    return {s: to_code.get(lab, lab) for s, lab in r.items()}


def check_index(problems):
    """DC-INDEX 1 (1.0.8). Every module that reads the site-wide listing,
    found by ast, carries the one DC-INDEX block; each reads the newer
    "Site index" layout and the older "Contents of this site" one to the
    same structures and types; and each worker, driven with a fake
    driver, names what it saw when the table is absent and reports a row
    whose heading it does not know."""
    group = []
    for path in sorted((SUITE / "modules").glob("dc_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.FunctionDef) and n.name == "extract_contexts"
               for n in tree.body):
            group.append("modules/" + path.name)
    if not group:
        problems.append("DC-INDEX: no module reads the listing, so nothing "
                        "was checked")
        return group
    blocks = {}
    for label in group:
        b = _one_block(label, (SUITE / label).read_text(encoding="utf-8"),
                       INDEX_RE, "DC-INDEX", problems)
        if b is not None:
            blocks[label] = b
    _agree(blocks, "DC-INDEX", problems)

    newer = _index_page([(h, s) for h, _c, s in INDEX_ROWS])
    older = _index_page([(c, s) for _h, c, s in INDEX_ROWS],
                        caption="Contents of this site")
    unknown = _index_page([(h, s) for h, _c, s in INDEX_ROWS]
                          + [("Widgets", ["example-widget"])])
    absent = ('<html><head><title>Fixture page</title></head><body><table '
              'class="cttypes"><caption>Something else</caption></table>'
              '</body></html>')
    real_sleep = time.sleep
    for label in group:
        if label not in INDEX_WORKERS:
            problems.append("{}: reads the listing and has no driven check "
                            "in INDEX_WORKERS".format(label))
            continue
        try:
            M = _index_import(label)
        except Exception as e:                       # pragma: no cover
            problems.append("{}: could not be imported: {}".format(label, e))
            continue
        for name, html in (("newer layout", newer), ("older layout", older)):
            got = _index_by_code(M, label, html)
            if got != INDEX_WANT:
                problems.append("{}: the {} read as {!r}".format(label, name,
                                                                 got))
        if _index_by_code(M, label, absent) is not None:
            problems.append("{}: a page without the listing read as one"
                            .format(label))
        if label not in blocks:
            continue        # reported above; its worker cannot be driven
        if M.index_unknown_headers(unknown) != [("Widgets", 1)]:
            problems.append("{}: the unknown heading was reported as {!r}"
                            .format(label, M.index_unknown_headers(unknown)))
        if M.index_unknown_headers(newer):
            problems.append("{}: a known heading was reported as unknown"
                            .format(label))

        fn_name, args = INDEX_WORKERS[label]
        for case, html in (("absent", absent), ("unknown", unknown)):
            lines = []
            M.log = lambda msg, *_a, **_k: lines.append(str(msg))
            M.get_driver = lambda _s, _h=html: _IndexDriver(_h)
            session = M.load_session(Path("/nonexistent/session.json"))
            session["base_url"] = "https://dc.example.edu"
            tmp = tempfile.mkdtemp(prefix="dcindex-")
            vals = {"session": session, "tmp": tmp,
                    "codes": [c for _l, c in M.STRUCTURE_TYPES],
                    "reports": list(getattr(M, "REPORT_LABELS", {}))[:1]}
            time.sleep = lambda *_a: None
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    getattr(M, fn_name)(*[vals[a] for a in args])
            except Exception as e:
                problems.append("{} ({}): the worker raised {}: {}".format(
                    label, case, e.__class__.__name__, e))
            finally:
                time.sleep = real_sleep
                shutil.rmtree(tmp, ignore_errors=True)
            summary = str(M.STATE.get("summary", ""))
            if case == "absent":
                if (M.STATE.get("phase") != "error"
                        or 'titled "Fixture page"' not in summary
                        or '"Something else"' not in summary):
                    problems.append("{}: with no listing it ended {!r}: {!r}"
                                    .format(label, M.STATE.get("phase"),
                                            summary))
            elif not any('headed "Widgets"' in ln for ln in lines):
                problems.append("{}: the worker did not report the unknown "
                                "heading".format(label))
    return group


def check_batch_history(problems):
    """Batch revise 1.6.2. The Spreadsheet History is read in both layouts:
    the older <table id="batch-history"> and the late-2026 <div
    id="batch-history"> around a table.manage-results (markup copied from a
    saved page; numbers invented). A page with neither is None."""
    M = _index_import("modules/dc_batch_revise.py")
    link = ('<a href="editor.cgi?context=example&amp;window=batch_revise_'
            'retrieve_spreadsheet&amp;exportitem={0}&amp;part={1}">Download '
            '{1}/1</a>')
    rows = "".join(
        "<tr><td>Generated</td><td>Fri Aug 07 06:43:21 2026 PT</td><td>{}"
        "</td><td><a href=\"#\">Delete</a></td></tr>".format(link.format(n, 1))
        for n in (1786110201, 1786100000))
    newer = ('<html><body><div class="batch-revise-section" id="batch-'
             'history"><table class="manage-results"><thead><tr><th>Source'
             '</th><th>Date Generated</th><th>Download</th><th>Delete</th>'
             '</tr></thead><tbody>' + rows + '</tbody></table><a href="#">'
             'Next</a> of 2</div></body></html>')
    older = ('<html><body><table id="batch-history"><tbody>' + rows +
             '</tbody></table></body></html>')
    for name, html in (("newer", newer), ("older", older)):
        got = M.parse_history(html)
        items = [e["exportitem"] for e in (got or [])]
        if items != [1786110201, 1786100000]:
            problems.append("dc_batch_revise: the {} history read as {!r}"
                            .format(name, items))
        elif got[0]["parts"] and got[0]["parts"][0][0] != 1:
            problems.append("dc_batch_revise: the {} history's part read "
                            "as {!r}".format(name, got[0]["parts"]))
    if M.parse_history("<html><body><table class='manage-results'>"
                       "</table></body></html>") is not None:
        problems.append("dc_batch_revise: a page with no history read as one")


# ---- The Configuration Manager's Apply (1.0.8) ------------------------------
# Driven through _apply_one, apply_worker, dryrun_worker and scrape_worker
# with a fake browser that behaves like the configuration pages where it
# matters:
#   * text typed into a field is keypresses (a tab moves focus to the next
#     field; a line break in a one-line field submits the form as it
#     stands), and a one-line field drops line breaks set in it;
#   * the page finishes itself with scripts after it loads: until one has
#     run, the disciplines list holds only an empty-valued placeholder
#     (SETTLE_JS reads that); another marks the Save Changes bar ready, which
#     never happens while the window is covered by other windows, and until
#     then the bar's button is not drawn and ignores a mouse click (a script
#     click still submits the form);
#   * Save Changes stores the form and loads a new page (the old page's
#     elements go stale), and a hidden Save Changes button cannot be
#     clicked.
#   * a script click returns before the form's request is answered: the
#     old page answers once more, then nothing answers while the request
#     is out, then the new page is loading, then loaded; a page requested
#     meanwhile is served first, and the save is written after it.
# Typing a value that holds a tab once saved half of it, and the log then
# said no structure had been updated; a dry run read before the scripts had
# run listed the placeholder as a change on structures nobody edited; and a
# mouse click while the window was covered saved nothing; and a page read
# back before the request was answered reported a save as NOT SAVED.

class _CMStale(Exception):
    """Like Selenium's StaleElementReferenceException."""


class _CMNotInteractable(Exception):
    """Like Selenium's ElementNotInteractableException."""


_CM_TRACE = ("Message: element not interactable\n"
             "  (Session info: chrome=0.0)\nStacktrace:\n0 0x1 <unknown>")


class _CMServer:
    """One structure's configuration, and how its page behaves."""

    def __init__(self, fields, keep=None, buttons=((True, True),),
                 unreadable_after_save=False, settle_polls=3, never="",
                 disciplines=(), ignore_clicks=0, saves_in_place=False,
                 covered=False, answer_polls=2, never_answers=False,
                 stale_reads=0, fail_get_at=0):
        self.stored = dict(fields)          # name -> (tag, value)
        self.keep = keep                    # what a save keeps, if not all
        self.buttons = buttons              # (shown, in the Save bar) each
        self.unreadable_after_save = unreadable_after_save
        self.settle_polls = settle_polls    # SETTLE_JS calls until settled
        self.never = never                  # "list": never settles
        self.covered = covered              # the bar is never marked ready
        self.answer_polls = answer_polls    # looks while the request is out
        self.never_answers = never_answers  # the request is never answered
        self.disciplines = disciplines      # items listed once settled
        self.ignore_clicks = ignore_clicks  # clicks that do nothing
        self.saves_in_place = saves_in_place
        self.stale_reads = stale_reads      # reads after a save that show
        self.stale_left = 0                 #   what was there before it
        self.before = None
        self.fail_get_at = fail_get_at      # this config GET raises
        self.config_gets = 0
        self.saves = 0
        self.regens = 0
        self.clicks = 0


class _CMElement:
    def __init__(self, page, tag, name, value="", itype="",
                 displayed=True, button=False, multiple=False, in_bar=False):
        self.page, self.tag_name, self.name = page, tag, name
        self.value, self.itype = value, itype
        self.displayed, self.button = displayed, button
        self.multiple, self.in_bar = multiple, in_bar

    def _alive(self):
        if self.page.gone:
            raise _CMStale("Message: stale element reference\n"
                           "Stacktrace:\n0 0x1 <unknown>")

    def one_line(self):
        return self.tag_name == "input"

    def set_value(self, v):
        self._alive()
        if self.one_line():                 # the browser's sanitization
            v = v.replace("\r", "").replace("\n", "")
        self.value = v

    def get_attribute(self, name):
        self._alive()
        if name == "multiple":
            return "true" if self.multiple else None
        return {"type": self.itype, "value": self.value,
                "name": self.name}.get(name)

    def is_displayed(self):
        self._alive()
        if self.in_bar and not self.page.bar_ready:
            return False                   # the bar is not drawn yet
        return self.displayed

    def is_enabled(self):
        self._alive()
        return True

    def is_selected(self):
        self._alive()
        return False

    def clear(self):
        self._alive()
        self.value = ""

    def send_keys(self, text):
        focus = self
        for ch in text:
            if self.page.gone:
                return                     # keystrokes land nowhere
            if ch == "\t":
                focus = self.page.after(focus)
            elif ch in "\r\n" and focus.one_line():
                self.page.submit()
            else:
                focus.value += ch

    def click(self):
        self._alive()
        if not self.button:
            return
        if not self.is_displayed():
            raise _CMNotInteractable(_CM_TRACE)
        server = self.page.driver.server
        server.clicks += 1
        if self.in_bar and not self.page.bar_ready:
            return                         # the bar is not ready yet
        self.script_click()

    def script_click(self):
        """element.click() run in the page: the form is submitted as the
        button would submit it, whatever has been drawn, and the call
        returns before the request is answered."""
        self._alive()
        server = self.page.driver.server
        if server.ignore_clicks:
            server.ignore_clicks -= 1
            return
        if server.saves_in_place:
            self.page.submit()
            return
        self.page.driver.pending = {"page": self.page, "first": True,
                                    "polls": server.answer_polls}


class _CMPage:
    def __init__(self, driver, read=False):
        self.driver, self.gone = driver, False
        self.polls, self.ready, self.bar_ready = 0, False, False
        driver.origins += 1
        self.origin, self.loading = driver.origins, 0
        server = driver.server
        values = server.stored
        if read and server.stale_left > 0:
            values = server.before          # a read that lags the save
            server.stale_left -= 1
        self.fields = [
            _CMElement(self, tag, name, value,
                       itype="" if tag == "textarea" else "text")
            for name, (tag, value) in values.items()]
        self.listing = (None if server.disciplines is None else
                        _CMElement(self, "select", "disciplines",
                                   multiple=True))
        self.buttons = [_CMElement(self, "input", "x_process_changes",
                                   "Save Changes", itype="submit",
                                   displayed=shown, button=True,
                                   in_bar=bar)
                        for shown, bar in server.buttons]

    def settle_state(self):
        server = self.driver.server
        self.polls += 1
        if server.never == "list":
            return [True, 1]
        self.ready = self.polls >= server.settle_polls
        self.bar_ready = self.ready and not server.covered
        return [True, 0] if self.ready else [True, 1]

    def after(self, el):
        order = self.fields + self.buttons
        return order[min(order.index(el) + 1, len(order) - 1)]

    def store(self):
        """What the server writes when the form's request arrives."""
        server = self.driver.server
        form = {e.name: e.value for e in self.fields}
        if server.keep:
            form = server.keep(form)
        server.before = dict(server.stored)
        for name, value in form.items():
            server.stored[name] = (server.stored[name][0], value)
        server.stale_left = server.stale_reads
        server.saves += 1

    def submit(self):
        server = self.driver.server
        form = {e.name: e.value for e in self.fields}
        if server.keep:
            form = server.keep(form)
        server.before = dict(server.stored)
        for name, value in form.items():
            server.stored[name] = (server.stored[name][0], value)
        server.stale_left = server.stale_reads
        server.saves += 1
        if server.saves_in_place:
            return                         # stored, but no new page
        self.gone = True
        self.driver.page = _CMPage(self.driver)
        if server.unreadable_after_save:
            self.driver.broken = True

    def html(self):
        import html as _h
        if self.driver.broken:
            return "<html><body><p>Something went wrong.</p></body></html>"
        parts = []
        for e in self.fields:
            if e.tag_name == "textarea":
                parts.append('<textarea name="{}">{}</textarea>'.format(
                    e.name, _h.escape(e.value, quote=False)))
            else:
                parts.append('<input type="text" name="{}" value="{}">'
                             .format(e.name, _h.escape(e.value)))
        if self.listing is not None:
            if self.ready:     # the placeholder stays, its text emptied
                opts = '<option value=""></option>' + "".join(
                    '<option value="{}">{}</option>'.format(i + 1,
                                                            _h.escape(d))
                    for i, d in enumerate(self.driver.server.disciplines))
            else:
                opts = '<option value="">Placeholder text</option>'
            parts.append('<select id="disciplines" class="selectedMultiSelect'
                         'Options" name="disciplines" multiple="multiple">'
                         '{}</select>'.format(opts))
        for b in self.buttons:
            parts.append('<input type="submit" name="x_process_changes" '
                         'value="Save Changes">')
        return ('<html><body><div id="user-config"><form name="config_form">'
                '{}</form></div></body></html>'.format("".join(parts)))

    def find_elements(self, by, value):
        if by == "name":
            found = [e for e in self.fields if e.name == value]
            if self.listing is not None and value == "disciplines":
                found.append(self.listing)
            return found
        if by == "xpath" and "x_process_changes" in value:
            return list(self.buttons)
        raise AssertionError("fixture: unexpected lookup {} {}"
                             .format(by, value))


class _CMDriver:
    """One fake browser. `servers` is one _CMServer, or {context: server}
    for a run over several structures."""

    def __init__(self, M, servers):
        self.M, self.broken = M, False
        self.origins, self.pending = 0, None
        self.servers = servers if isinstance(servers, dict) else None
        self.server = None if self.servers else servers
        self.page, self.current_url, self.gets = None, "", []

    def get(self, url):
        self.gets.append(url)
        self.current_url = url
        late = self.pending
        if late:
            # A page requested while the form's request is out is served
            # first; the save is written after it.
            late["page"].gone = True
            self.pending = None
        if self.servers:
            ctx = url.split("context=", 1)[1].split("&", 1)[0]
            self.server = self.servers[ctx]
            self.broken = False
        if "x_regenerate=1" in url:
            self.server.regens += 1
            return
        if getattr(self.server, "raise_on_get", False):
            raise RuntimeError(_CM_TRACE)
        self.server.config_gets += 1
        if self.server.config_gets == self.server.fail_get_at:
            raise RuntimeError(_CM_TRACE)
        self.page = _CMPage(self, read=True)
        if late and not self.server.never_answers:
            late["page"].store()

    @property
    def page_source(self):
        return self.page.html() if self.page else ""

    @property
    def title(self):
        return "Configuration Tools"

    def find_element(self, by, value):
        if by == "id" and value == "user-config" and not self.broken:
            return self.page
        raise AssertionError("fixture: no element {} {}".format(by, value))

    def find_elements(self, by, value):
        return self.page.find_elements(by, value)

    def execute_script(self, script, *args):
        M = self.M
        if script == M.SET_VALUE_JS:
            args[0].set_value(args[1])
            return None
        if script == M.READ_VALUE_JS:
            args[0]._alive()
            return args[0].value
        if script == M.CLICK_JS:
            args[0].script_click()
            self.server.clicks += 1
            return None
        if script == M.DOC_JS:
            late = self.pending
            if late:
                if late["first"]:          # the click has not navigated yet
                    late["first"] = False
                    return ["complete", late["page"].origin]
                late["page"].gone = True
                if self.server.never_answers or late["polls"] > 0:
                    late["polls"] -= 1
                    raise _CMStale("Message: no such execution context")
                self.pending = None        # the request is answered
                late["page"].store()
                self.page = _CMPage(self)
                self.page.loading = 1
                if self.server.unreadable_after_save:
                    self.broken = True
            if self.page.loading:
                self.page.loading -= 1
                return ["loading", self.page.origin]
            return ["complete", self.page.origin]
        if script == M.IN_BAR_JS:
            args[0]._alive()
            return args[0].in_bar
        if script == M.SETTLE_JS:
            if self.broken or self.page is None:
                return [True, 0]
            return self.page.settle_state()
        raise AssertionError("fixture: unexpected script " + script[:40])


def _cm_selenium_stub():
    """Stand-in Selenium modules for the names the module imports. Returns
    the previous sys.modules entries, for _cm_selenium_restore()."""
    import types
    names = ["selenium", "selenium.webdriver", "selenium.webdriver.common",
             "selenium.webdriver.common.by", "selenium.webdriver.support",
             "selenium.webdriver.support.ui",
             "selenium.webdriver.support.expected_conditions"]
    saved = {n: sys.modules.get(n) for n in names}
    mods = {n: types.ModuleType(n) for n in names}

    class By:
        ID, NAME, XPATH = "id", "name", "xpath"

    class WebDriverWait:
        def __init__(self, driver, timeout):
            self.driver = driver

        def until(self, cond):
            return cond(self.driver)

    class Select:
        def __init__(self, el):
            raise AssertionError("fixture: no single select here")

    ec = mods["selenium.webdriver.support.expected_conditions"]
    ec.presence_of_element_located = (
        lambda loc: (lambda d: d.find_element(*loc)))
    mods["selenium.webdriver.common.by"].By = By
    mods["selenium.webdriver.support.ui"].WebDriverWait = WebDriverWait
    mods["selenium.webdriver.support.ui"].Select = Select
    mods["selenium.webdriver.support"].expected_conditions = ec
    sys.modules.update(mods)
    return saved


def _cm_selenium_restore(saved):
    for n, m in saved.items():
        if m is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = m


# A value shaped like an HTML field's: a comment first, then markup, one
# line holding only a tab, and more markup after it. The field after it on
# the page is a one-line input.
_CM_OLD = "<div><h2>About</h2>\n<p>Text.</p>\n\t\n<h3>Help</h3></div>"
_CM_NEW = "<!-- a note -->\n" + _CM_OLD
_CM_BASE = "https://dc.example.edu"


def _cm_fields():
    return {"intro": ("textarea", _CM_OLD), "title": ("input", "A title")}


def _cm_module(problems):
    label = "modules/dc_config_manager.py"
    try:
        return _index_import(label)
    except Exception as e:                           # pragma: no cover
        problems.append("{}: could not be imported: {}".format(label, e))
        return None


class _CMClock:
    """The module's clock in these checks: time passes only when the
    module sleeps, so how a wait ends never depends on how fast the machine
    running the checks is. (With the real clock and 50 ms waits, a loaded
    machine could run out of time between two looks and turn a clean save
    into UNKNOWN.)"""

    def __init__(self):
        self.now = 1.0e9

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class _cm_fast:
    """Selenium stubbed and the module's clock replaced (_CMClock), for
    the length of a check; the module's own wait limits are kept."""

    def __init__(self, M):
        self.M = M

    def __enter__(self):
        self.saved = _cm_selenium_stub()
        self.sleep = time.sleep
        time.sleep = lambda s: None
        self.clock = self.M.time
        self.M.time = _CMClock()
        return self

    def __exit__(self, *exc):
        time.sleep = self.sleep
        self.M.time = self.clock
        _cm_selenium_restore(self.saved)
        return False


def _cm_one(M, server, diffs, regen=True):
    driver = _CMDriver(M, server)
    with contextlib.redirect_stdout(io.StringIO()):
        M.begin_run_log()
        status, back = M._apply_one(driver, _CM_BASE, "example-etd",
                                    diffs, regen)
    return status, back, M.run_log_text()


def check_config_apply(problems):
    """The Configuration Manager's Apply, driven (see the note above)."""
    M = _cm_module(problems)
    if M is None:
        return 0
    cases = 0
    with _cm_fast(M):
        # 1. A value holding a tab and line breaks, the hidden Save Changes
        #    button first, a page that settles on the third look: saved
        #    whole, the next field untouched, regenerated.
        server = _CMServer(_cm_fields(), buttons=((False, False),
                                                  (True, True)))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if server.stored["intro"][1] != _CM_NEW:
            problems.append("config apply: after a value holding a tab and "
                            "line breaks was applied, the field holds {} "
                            "character(s), not the planned {}".format(
                                len(server.stored["intro"][1]), len(_CM_NEW)))
        if server.stored["title"][1] != "A title":
            problems.append("config apply: the field after the one changed "
                            "was changed too")
        if server.saves != 1 or status != "Saved + regenerated":
            problems.append("config apply: a clean save gave {} save(s) and "
                            "status {!r}".format(server.saves, status))
        if "intro: saved as planned" not in back:
            problems.append("config apply: the Read Back for a clean save "
                            "was {!r}".format(back))

        # 2. A line break planned for a one-line field: the form drops it,
        #    the read-back sees that, and nothing is saved.
        server = _CMServer(_cm_fields())
        status, back, _l = _cm_one(M, server,
                                   [("title", "A title", "Two\nlines")])
        cases += 1
        if server.saves or not status.startswith("NOT SAVED"):
            problems.append("config apply: a value the form cannot hold gave "
                            "{} save(s) and status {!r}".format(server.saves,
                                                               status))
        if "cannot hold a line break" not in back:
            problems.append("config apply: the Read Back did not say why: "
                            "{!r}".format(back))

        # 3. The server keeps something other than what was sent: the page
        #    read back says so, and the structure is not regenerated.
        server = _CMServer(_cm_fields(), keep=lambda form: dict(
            form, intro="<p>Text.</p><magic ignore></magic>"))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if status != "NOT AS PLANNED (not regenerated)" or server.regens:
            problems.append("config apply: a save the server changed gave "
                            "status {!r} and {} regeneration(s)".format(
                                status, server.regens))
        if "changed, NOT as planned" not in back:
            problems.append("config apply: the Read Back for a changed save "
                            "was {!r}".format(back))

        # 4. Only a hidden Save Changes button: nothing pressed or saved.
        server = _CMServer(_cm_fields(), buttons=((False, False),))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if server.saves or not status.startswith("NOT SAVED (no Save "
                                                 "Changes button shown"):
            problems.append("config apply: with no Save Changes button shown "
                            "it gave {} save(s) and status {!r}".format(
                                server.saves, status))

        # 5. Saved, then the page cannot be read back: unknown, never "not
        #    saved", and not regenerated.
        server = _CMServer(_cm_fields(), unreadable_after_save=True)
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if not status.startswith("UNKNOWN") or server.regens:
            problems.append("config apply: a page unreadable after saving "
                            "gave status {!r} and {} regeneration(s)".format(
                                status, server.regens))

        # 6. The worker, two structures: one the server changes, one whose
        #    page raises a Selenium-style message. The summary counts what
        #    the pages hold, the log carries no stack trace, and the update
        #    log has its Read Back column.
        server = _CMServer(_cm_fields(), keep=lambda form: dict(
            form, intro="<p>Text.</p>"))
        bad = _CMServer(_cm_fields())
        bad.raise_on_get = True
        driver = _CMDriver(M, {"example-etd": server, "example-bad": bad})
        M._attach_or_fail = lambda session: driver
        M.PLAN = {"items": [
            ("example-etd", "ETD", [("intro", _CM_OLD, _CM_NEW)]),
            ("example-bad", "ETD", [("intro", _CM_OLD, _CM_NEW)])],
            "report_version": M.STATE["report"]["version"]}
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                M.begin_run_log()
                M.apply_worker({"base_url": _CM_BASE}, tmp, True)
            cases += 1
            summary = M.STATE.get("summary", "")
            logged = M.run_log_text()
            if ("0 of 2 structure(s) saved as planned" not in summary
                    or "1 changed NOT as planned" not in summary
                    or "updated" in summary.split("→")[0]):
                problems.append("config apply: the worker's summary was {!r}"
                                .format(summary))
            if "Stacktrace" in logged or "Session info" in logged:
                problems.append("config apply: a stack trace reached the log")
            if M.STATE.get("outcome") == "green":
                problems.append("config apply: a run that saved nothing as "
                                "planned finished green")
            books = [f for f in os.listdir(tmp)
                     if f.startswith("DC_Config_UpdateLog_")]
            if len(books) != 1:
                problems.append("config apply: {} update log(s) written"
                                .format(len(books)))
            else:
                from openpyxl import load_workbook
                wb = load_workbook(os.path.join(tmp, books[0]))
                rows = [list(r) for r in wb.active.iter_rows(
                    values_only=True)]
                head = rows[0]
                if "Read Back" not in head or "Status" not in head:
                    problems.append("config apply: the update log's columns "
                                    "are {!r}".format(head))
                else:
                    s, r = head.index("Status"), head.index("Read Back")
                    got = [(row[s], row[r] or "") for row in rows[1:]]
                    if (len(got) != 2
                            or not got[0][0].startswith("NOT AS PLANNED")
                            or "changed, NOT as planned" not in got[0][1]
                            or not got[1][0].startswith(
                                "ERROR before Save Changes")):
                        problems.append("config apply: the update log rows "
                                        "were {!r}".format(got))
    return cases


def check_config_settle(problems):
    """The Configuration Manager waits for each page's scripts, reads a
    multiple-choice list as its items, never applies one, and presses Save
    Changes a second time only when the first changed nothing (see the
    note above check_config_apply)."""
    M = _cm_module(problems)
    if M is None:
        return 0
    cases = 0
    with _cm_fast(M):
        # 7. Reading: the placeholder is never a value; the widget's listed
        #    items are; another multiple list is its selected options.
        def page(sel):
            return ('<div id="user-config"><form>{}</form></div>'
                    .format(sel))
        widget = ('<select class="selectedMultiSelectOptions" name="d" '
                  'multiple="multiple">{}</select>')
        plain = ('<select name="p" multiple="multiple"><option value="1">'
                 'One</option><option value="2" selected>Two</option>'
                 '</select>')
        before = M.extract_fields(page(widget.format(
            '<option value="">Placeholder text</option>')))[0]
        emptied = M.extract_fields(page(widget.format(
            '<option value=""></option>')))[0]
        after = M.extract_fields(page(widget.format(
            '<option value="7">Alpha</option><option value="9">Beta'
            '</option>') + plain))[0]
        cases += 1
        if before.get("d") != "" or emptied.get("d") != "":
            problems.append("config settle: an unloaded list read as {!r}, "
                            "a loaded empty one as {!r}".format(
                                before.get("d"), emptied.get("d")))
        if after.get("d") != "Alpha; Beta" or after.get("p") != "Two":
            problems.append("config settle: multiple lists read as {!r} and "
                            "{!r}".format(after.get("d"), after.get("p")))
        if M.multi_fields(page(widget.format("") + plain)) != {"d", "p"}:
            problems.append("config settle: multi_fields found {!r}".format(
                M.multi_fields(page(widget.format("") + plain))))

        # 8. A covered window: the Save bar is never marked ready and its
        #    button never drawn, and the press still saves, once.
        server = _CMServer(_cm_fields(), covered=True,
                           buttons=((False, False), (True, True)))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if (status != "Saved + regenerated" or server.saves != 1
                or server.clicks != 1):
            problems.append("config settle: in a covered window the status "
                            "was {!r}, {} click(s), {} save(s)".format(
                                status, server.clicks, server.saves))

        # 8c. A slow answer to the press: waited for, and saved as planned.
        server = _CMServer(_cm_fields(), answer_polls=6)
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if status != "Saved + regenerated" or server.saves != 1:
            problems.append("config settle: a slow answer to Save Changes "
                            "gave status {!r}, {} save(s)".format(
                                status, server.saves))

        # 8d. No answer at all: UNKNOWN, one press, no read-back as NOT SAVED.
        server = _CMServer(_cm_fields(), never_answers=True)
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if not status.startswith("UNKNOWN") or server.clicks != 1:
            problems.append("config settle: an unanswered Save Changes gave "
                            "status {!r}, {} click(s)".format(status,
                                                             server.clicks))

        # 8a. An older page: one Save Changes button, shown, no Save bar.
        server = _CMServer(_cm_fields(), buttons=((True, False),))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if status != "Saved + regenerated" or server.saves != 1:
            problems.append("config settle: a page without a Save bar gave "
                            "status {!r}, {} save(s)".format(status,
                                                             server.saves))

        # 8b. A page whose disciplines list never loads: nothing set,
        #     nothing pressed, nothing saved, and it says why.
        server = _CMServer(_cm_fields(), never="list")
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if (status != "NOT SAVED (the page did not finish loading)"
                or server.saves or server.clicks):
            problems.append("config settle: a page that never settled gave "
                            "status {!r}, {} click(s), {} save(s)".format(
                                status, server.clicks, server.saves))

        # 9. A press that changes nothing, with every field unchanged: one
        #    more try, which saves; both presses on the record.
        server = _CMServer(_cm_fields(), ignore_clicks=1)
        status, back, logged = _cm_one(M, server,
                                       [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if (status != "Saved + regenerated" or server.saves != 1
                or server.clicks != 2):
            problems.append("config settle: after a press that did nothing "
                            "the status was {!r}, {} click(s), {} save(s)"
                            .format(status, server.clicks, server.saves))
        if "trying once more" not in logged or "pressed twice" not in back:
            problems.append("config settle: the second press is not on the "
                            "record ({!r})".format(back))

        # 10. A press that saved something without a new page: no second
        #     press, whatever the page held.
        server = _CMServer(_cm_fields(), saves_in_place=True,
                           keep=lambda form: dict(form, intro="<p>X</p>"))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if server.clicks != 1 or server.saves != 1 or not status.startswith(
                "NOT AS PLANNED"):
            problems.append("config settle: a save without a new page gave "
                            "{} click(s), {} save(s), status {!r}".format(
                                server.clicks, server.saves, status))

        # 11a. A press that brought a new page but changed nothing: no
        #      second press — the server answered.
        server = _CMServer(_cm_fields(), keep=lambda form: dict(
            form, intro=_CM_OLD))
        status, back, _l = _cm_one(M, server, [("intro", _CM_OLD, _CM_NEW)])
        cases += 1
        if server.clicks != 1 or status != "NOT SAVED (not regenerated)":
            problems.append("config settle: a press answered with no change "
                            "gave {} click(s), status {!r}".format(
                                server.clicks, status))

        # 11. A plan holding only a multiple-choice list: never changed.
        server = _CMServer(_cm_fields(), disciplines=["Alpha"])
        status, back, _l = _cm_one(M, server,
                                   [("disciplines", "Alpha", "")])
        cases += 1
        if (server.saves or server.clicks
                or not status.startswith("NOT SAVED (no field could be set")
                or "multiple-choice" not in back):
            problems.append("config settle: a multiple-choice change gave "
                            "status {!r}, {!r}, {} save(s)".format(
                                status, back, server.saves))

        # 12. The dry run over three structures: one with a real change, one
        #     whose disciplines never load, one whose disciplines differ.
        site = {"example-a": _CMServer(_cm_fields(), covered=True),
                "example-b": _CMServer(_cm_fields(), never="list"),
                "example-c": _CMServer(_cm_fields(),
                                       disciplines=["Physical Science"])}
        driver = _CMDriver(M, site)
        M._attach_or_fail = lambda session: driver
        sheet = {"title": "A title", "disciplines": ""}
        M.UPD_MODEL = {
            "types": {"ETD": ["example-a", "example-b", "example-c"]},
            "rows": {"ETD": {"example-a": dict(sheet, intro=_CM_NEW),
                             "example-b": dict(sheet, intro=_CM_OLD),
                             "example-c": dict(sheet, intro=_CM_OLD)}},
            "fields": {"ETD": ["intro", "title", "disciplines"]},
            "source": "fixture"}
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                M.begin_run_log()
                M.dryrun_worker({"base_url": _CM_BASE}, tmp, ["ETD"])
            cases += 1
            summary = M.STATE.get("summary", "")
            logged = M.run_log_text()
            planned = {c: d for c, _l, d in (M.PLAN or {}).get("items", [])}
            if (set(planned) != {"example-a", "example-c"}
                    or [f for f, _o, _n in planned["example-a"]] != ["intro"]
                    or planned["example-c"]):
                problems.append("config settle: the dry run planned {!r}"
                                .format(planned))
            for want in ("1 proposed change(s)", "1 structure(s) not checked",
                         "1 difference(s) in multiple-choice lists"):
                if want not in summary:
                    problems.append("config settle: the dry run's summary "
                                    "lacks {!r}: {!r}".format(want, summary))
            if "WARNING: 'example-b'" not in logged:
                problems.append("config settle: no warning for the page that "
                                "never settled")
            if M.STATE.get("outcome") != "amber":
                problems.append("config settle: the dry run finished {!r}"
                                .format(M.STATE.get("outcome")))

        # 13. The scrape over two structures in a covered window: one whose
        #     disciplines list two items, one whose list never loads.
        site = {"example-a": _CMServer(_cm_fields(), covered=True,
                                       disciplines=["Physical Science",
                                                    "Life Science"]),
                "example-b": _CMServer(_cm_fields(), covered=True,
                                       never="list")}
        driver = _CMDriver(M, site)
        M._attach_or_fail = lambda session: driver
        M.SCRAPE_MODEL = {"types": {"ETD": ["example-a", "example-b"]},
                          "source": "fixture"}
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                M.begin_run_log()
                M.scrape_worker({"base_url": _CM_BASE}, tmp, ["ETD"])
            cases += 1
            summary = M.STATE.get("summary", "")
            if ("1 of 2 structure(s) scraped" not in summary
                    or "1 not recorded" not in summary):
                problems.append("config settle: the scrape's summary was {!r}"
                                .format(summary))
            if M.STATE.get("outcome") != "amber":
                problems.append("config settle: the scrape finished {!r}"
                                .format(M.STATE.get("outcome")))
            books = [f for f in os.listdir(tmp)
                     if f.startswith("DC_Config_Report_")]
            if len(books) != 1:
                problems.append("config settle: {} report(s) written"
                                .format(len(books)))
            else:
                from openpyxl import load_workbook
                wb = load_workbook(os.path.join(tmp, books[0]))
                rows = [list(r) for r in wb["ETD"].iter_rows(
                    values_only=True)]
                head = rows[0]
                got = [(r[0], r[head.index("disciplines")]
                        if "disciplines" in head else None)
                       for r in rows[1:]]
                if got != [("example-a", "Physical Science; Life Science")]:
                    problems.append("config settle: the report's rows were "
                                    "{!r}".format(got))
    return cases



def check_config_readback(problems):
    """Apply describes the page that answered Save Changes and, after a
    press that brought a new page, reads a page that does not yet show the
    save again, logging each read (see the note above check_config_apply).
    """
    M = _cm_module(problems)
    if M is None:
        return 0
    cases = 0
    diffs = [("intro", _CM_OLD, _CM_NEW)]
    with _cm_fast(M):
        # 19. The first read after the save lags it; the second shows it:
        #     Saved, one press, the reads stop there, regenerated, and the
        #     answering page and the regeneration are in the log.
        server = _CMServer(_cm_fields(), stale_reads=1)
        status, back, logged = _cm_one(M, server, diffs)
        cases += 1
        if (status != "Saved + regenerated" or server.saves != 1
                or server.clicks != 1 or server.regens != 1):
            problems.append("config readback: a save the first read lagged "
                            "gave status {!r}, {} save(s), {} click(s), {} "
                            "regeneration(s)".format(status, server.saves,
                                                     server.clicks,
                                                     server.regens))
        for want in ("read 1 of the page shows intro unchanged — reading "
                     "it again in 2 s",
                     "read 2, after waiting 2 s, shows intro saved as "
                     "planned",
                     'Save Changes was answered with the page "Configuration '
                     'Tools"', "whose form shows intro saved as planned",
                     "'example-etd': regeneration requested."):
            if want not in logged:
                problems.append("config readback: the log lacks {!r}"
                                .format(want))
        if "read 3" in logged:
            problems.append("config readback: it read again after a read "
                            "showed the save")
        if "saved as planned only at read 2, after waiting 2 s" not in back:
            problems.append("config readback: the Read Back was {!r}"
                            .format(back))

        # 20. Every read lags: four reads, NOT SAVED, one press, not
        #     regenerated.
        server = _CMServer(_cm_fields(), stale_reads=99)
        status, back, logged = _cm_one(M, server, diffs)
        cases += 1
        if (status != "NOT SAVED (not regenerated)" or server.clicks != 1
                or server.regens or server.config_gets != 5):
            problems.append("config readback: a save no read showed gave "
                            "status {!r}, {} click(s), {} regeneration(s), "
                            "{} page load(s)".format(status, server.clicks,
                                                     server.regens,
                                                     server.config_gets))
        if ("read 4, after waiting 10 s, shows intro unchanged" not in logged
                or "read 5" in logged):
            problems.append("config readback: the reads logged were not "
                            "four: {!r}".format(logged[-400:]))

        # 21. A press that brought no new page is not read again (the one
        #     second press covers that).
        server = _CMServer(_cm_fields(), saves_in_place=True, stale_reads=1)
        status, back, logged = _cm_one(M, server, diffs)
        cases += 1
        if "reading it again" in logged:
            problems.append("config readback: a press that brought no new "
                            "page was read again")

        # 22. A further read that cannot be made: the status stays the one
        #     read before it, and the log says so.
        server = _CMServer(_cm_fields(), stale_reads=99, fail_get_at=3)
        try:
            status, back, logged = _cm_one(M, server, diffs)
        except Exception as e:
            status, back, logged = "raised " + repr(e)[:80], "", ""
        cases += 1
        if (status != "NOT SAVED (not regenerated)"
                or "read 2 could not be made" not in logged
                or "the status is from read 1" not in logged
                or "Stacktrace" in logged):
            problems.append("config readback: a further read that failed "
                            "gave status {!r}; log {!r}".format(
                                status, logged[-300:]))
    return cases


CM_SCAN_NODE = r"""
const loadedPath={inventory:'',report:''};
let RESP=null, MSG='';
async function api(u,b){ return RESP; }
function flashErr(m){ MSG='ERR:'+m; }
function flash(m){ MSG='OK:'+m; }
function Option(t,v){ return {text:t,value:v}; }
function mkSel(){ return {opts:[],_v:null,disabled:false,
  set innerHTML(x){ this.opts=[]; this._v=null; },
  append(o){ this.opts.push(o); },
  get value(){ return this._v!==null ? this._v : (this.opts[0]?this.opts[0].value:''); },
  set value(v){ this._v=v; }}; }
const load={disabled:false};
"""

CM_SCAN_DRIVE = r"""
(async ()=>{
  const out=[];
  const sel=mkSel();
  RESP={ok:true,j:{files:[],pattern:'DC_URL_Inventory_*.xlsx'}};
  await doScan('inventory','/x',sel,load,'inventories');
  out.push('empty='+MSG);
  RESP={ok:true,j:{files:[{name:'new.xlsx',path:'/x/new.xlsx',modified:'2'},
                          {name:'old.xlsx',path:'/x/old.xlsx',modified:'1'}]}};
  noteLoaded({targets:{path:''},report:{path:'/x/old.xlsx'}});
  await doScan('report','/x',sel,load,'config reports');
  out.push('kept='+sel.value);
  noteLoaded({targets:{},report:{loaded:false}});
  await doScan('report','/x',sel,load,'config reports');
  out.push('none='+sel.value);
  console.log(out.join(' | '));
})();
"""


def check_config_scan(node_bin, problems):
    """The Configuration Manager's Scan names what it looked for, and keeps
    the loaded workbook selected (doScan and noteLoaded driven in node,
    poll() checked to call noteLoaded); /api/load puts the workbook's path
    in the state the page polls (driven over the module's own server)."""
    label = "modules/dc_config_manager.py"
    html = page_html(label, "PAGE")
    src = SCRIPT_RE.findall(html or "")
    src = "\n".join(src)
    fns = [_js_function(src, "noteLoaded"), _js_function(src, "doScan"),
           _js_function(src, "poll")]
    if None in fns:
        problems.append("config scan: noteLoaded, doScan or poll not found "
                        "in the page")
        return 0
    if "noteLoaded(s)" not in fns[2]:
        # Text, not behavior: poll() needs the whole page to run.
        problems.append("config scan: poll() does not call noteLoaded(s)")
    code = CM_SCAN_NODE + fns[0] + "\nasync " + fns[1] + "\n" + CM_SCAN_DRIVE
    try:
        out = subprocess.run([node_bin, "-e", code], capture_output=True,
                             text=True, encoding="utf-8", timeout=20)
        got = out.stdout.strip()
    except Exception as e:                           # pragma: no cover
        problems.append("config scan: node failed: {}".format(e))
        return 0
    want = ("empty=ERR:No DC_URL_Inventory_*.xlsx workbooks in that folder. "
            "To load a config report, choose Update configurations first. | "
            "kept=/x/old.xlsx | none=/x/new.xlsx")
    if got != want:
        problems.append("config scan: the page gave {!r} ({})".format(
            got, out.stderr.strip()[-200:]))

    from http.server import ThreadingHTTPServer
    import threading
    M = _cm_module(problems)
    if M is None:
        return 1
    session = M.load_session(Path("/nonexistent/session.json"))
    session["hub_url"] = "http://127.0.0.1:8750"
    with tempfile.TemporaryDirectory() as tmp:
        book = os.path.join(tmp, "DC_Config_Report_fixture.xlsx")
        M.write_config_workbook(book, {"ETD": [("example-a", {"intro": "x"})]},
                                _CM_BASE, "333333")
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  M.make_handler(session,
                                                 M.build_page(session)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            with contextlib.redirect_stdout(io.StringIO()):
                code1, _b = _http(port, "/api/load",
                                  {"kind": "report", "path": book})
            code2, body = _http(port, "/api/state")
            state = json.loads(body) if code2 == 200 else {}
            code3, body3 = _http(port, "/api/scan",
                                 {"kind": "inventory", "folder": tmp})
            scan = json.loads(body3) if code3 == 200 else {}
        finally:
            srv.shutdown()
    if code1 != 200 or state.get("report", {}).get("path") != book:
        problems.append("config scan: after /api/load the state's report "
                        "path was {!r} (load {})".format(
                            state.get("report", {}).get("path"), code1))
    if scan.get("pattern") != "DC_URL_Inventory_*.xlsx":
        problems.append("config scan: /api/scan answered {!r}".format(scan))
    return 2


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
    check_log_copy_in_node(node_bin, group, problems)
    check_log_spacing(group, problems)
    statused = check_status_lines(node_bin, group, problems)
    check_download_phase_word(node_bin, group, problems)
    check_chrome_commands(node_bin, problems)
    check_counts_of_one(problems)
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
    browsers, path_fields = check_browse_markup(served, problems)
    with contextlib.redirect_stdout(io.StringIO()):
        check_browse_driven(browsers, problems)
    check_browse_in_node(node_bin, served, browsers, problems)
    win_csharp = check_browse_windows_compile(browsers, problems)
    pickers = check_pick_blocks(served, problems)
    check_pick_in_node(node_bin, pickers, problems)
    readers = check_index(problems)
    check_batch_history(problems)
    applied = check_config_apply(problems)
    applied += check_config_settle(problems)
    applied += check_config_readback(problems)
    scanned = check_config_scan(node_bin, problems)

    print("Served pages — verification")
    print("  pages checked: {}".format(len(PAGE_SOURCES)))
    print("  status lines driven through each page's own poll(): {}"
          .format(statused))
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
        print("  {} path field(s) in {} module(s), found by markup, each with"
              .format(path_fields, len(browsers)))
        print("  its Browse button(s); the one DC-BROWSE block in each was")
        print("  driven with the dialog faked, on the server and in node.")
        print("  The Windows dialog's C#: {}.".format(win_csharp))
        print("  {} page(s) with a parent picker carry the one DC-PICK"
              .format(len(pickers)))
        print("  block; each page's own list was driven in node: the")
        print("  checked parents by name, Remove, and Clear all.")
        print("  {} module(s) that read the site-wide listing carry the one"
              .format(len(readers)))
        print("  DC-INDEX block; each read the Site index and the older")
        print("  Contents of this site layout alike, and each worker named")
        print("  what it saw with no listing and reported an unknown heading.")
        print("  Batch revise reads its Spreadsheet History in both layouts.")
        print("  The Configuration Manager's Apply was driven through {} cases"
              .format(applied))
        print("  with a browser that types like one and finishes its pages")
        print("  with scripts: pages are read and clicked only once settled;")
        print("  values are set, never typed, and read back before saving;")
        print("  a multiple-choice list is read, never applied; and what the")
        print("  page holds after saving is what the status and log say.")
        print("  Its Scan names what it looked for and keeps the loaded")
        print("  workbook selected ({} checks, in node and over its own"
              .format(scanned))
        print("  server).")
        print("  {} module(s) with a log carry the one DC-LOG block; each was".format(len(group)))
        print("  driven: Copy log is the whole run, Clear is refused during")
        print("  a run and keeps what is loaded, and one start of many wins.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
