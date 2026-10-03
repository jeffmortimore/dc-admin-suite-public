"""
Dependency checking.

Every module declares the pip packages it needs in its MANIFEST
("requires": ["selenium", "beautifulsoup4", ...]). The shell aggregates the
requirements of ALL currently installed modules — re-scanning module files
each time, so newly added or edited modules are always accounted for — and
verifies each package is importable in the current Python environment.
"""

import importlib.util
import re
import shutil
import subprocess
import sys

# pip install name  →  import name (only entries that differ need a mapping)
IMPORT_NAME_MAP = {
    "beautifulsoup4": "bs4",
    "pymupdf": "fitz",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "webdriver-manager": "webdriver_manager",
    "google-api-python-client": "googleapiclient",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "attrs": "attr",
}


def import_name_for(pip_name: str) -> str:
    key = pip_name.strip().lower()
    return IMPORT_NAME_MAP.get(key, key.replace("-", "_"))


def is_installed(pip_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name_for(pip_name)) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# ---------------------------------------------------------------------------
# chromedriver — an external binary, not a pip package, and the one piece of
# the toolchain that goes stale on its own, because Chrome updates itself.
#
# Selenium 4.6+ ships Selenium Manager, which fetches a driver matching the
# installed browser on demand. A chromedriver sitting on PATH SHADOWS that,
# so the suite keeps using a binary that was correct when it was installed
# and refuses the browser some weeks later with SessionNotCreatedException.
# Selenium's own advice, printed in that very failure, is to remove the
# hand-installed driver.
#
# So the healthy state here is ABSENT. This check therefore says nothing
# when it finds nothing, warns when it finds a driver whose major version
# matches (correct today, stale at the next Chrome update), and calls it a
# problem when the majors already disagree.
# ---------------------------------------------------------------------------
_CD_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def _major(version: str) -> str:
    """The leading version component, or "" when there isn't one."""
    m = re.match(r"\s*(\d+)", version or "")
    return m.group(1) if m else ""


def browser_major(browser: str) -> str:
    """Major version from a DevTools Browser string ('Chrome/141.0.7390.54')."""
    m = _CD_VERSION_RE.search(browser or "")
    return _major(m.group(1)) if m else ""


def chromedriver_on_path(timeout: float = 10.0) -> dict:
    """Locate a chromedriver on PATH and ask it its version.

    Returns {"present", "path", "version", "major", "error"}. A driver that
    is present but will not answer --version is reported as present WITH an
    error, never as absent: absent is the healthy state here, and it must
    not be claimed on the strength of a subprocess call that failed.
    """
    info = {"present": False, "path": "", "version": "", "major": "",
            "error": ""}
    found = shutil.which("chromedriver")
    if not found:
        return info
    info["present"] = True
    info["path"] = found
    try:
        proc = subprocess.run([found, "--version"], capture_output=True,
                              text=True, timeout=timeout)
        text = (proc.stdout or "") + (proc.stderr or "")
        m = _CD_VERSION_RE.search(text)
        if m:
            info["version"] = m.group(1)
            info["major"] = _major(m.group(1))
        else:
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            info["error"] = (lines[0][:200] if lines
                             else "it printed no version")
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = "{}: {}".format(e.__class__.__name__, e)
    return info


def chromedriver_advice(info: dict, browser: str = "") -> list:
    """Troubleshooting lines about a chromedriver on PATH. [] when healthy."""
    if not info.get("present"):
        return []                      # the healthy state — say nothing
    where = info.get("path") or "chromedriver"
    remove = ("Remove it (or take its folder off your PATH) and let Selenium "
              "Manager fetch a driver matching whatever Chrome is installed. "
              "That is Selenium's own advice, and it keeps working when "
              "Chrome updates itself.")
    if info.get("error"):
        return ["A chromedriver is on your PATH at {} but would not report "
                "its version ({}).".format(where, info["error"]), remove]
    bmaj = browser_major(browser)
    cmaj = info.get("major", "")
    if bmaj and cmaj and bmaj != cmaj:
        return ["The chromedriver on your PATH is version {} but Chrome is "
                "version {}. Modules will fail to attach."
                .format(info["version"], bmaj), remove]
    return ["A chromedriver (version {}) is on your PATH at {}. It matches "
            "Chrome today, but it shadows Selenium Manager, so modules will "
            "start failing the next time Chrome updates itself."
            .format(info.get("version") or "unknown", where), remove]


def run_check(modules: list, browser: str = "") -> dict:
    """
    Aggregate and check dependencies across a fresh module scan.

    `modules` is the list returned by module_registry.scan_modules().
    `browser` is the DevTools Browser string when the caller has one, so a
    chromedriver on PATH can be compared against the browser it will drive.
    Returns a dict the UI renders directly.
    """
    # Aggregate: package → sorted list of module names that need it
    needed = {}
    for mod in modules:
        if mod.get("error"):
            continue  # unparseable modules are reported by the module scan
        for pkg in mod.get("requires", []):
            needed.setdefault(pkg.strip(), set()).add(mod["name"])

    deps = []
    missing = []
    for pkg in sorted(needed, key=str.lower):
        ok = is_installed(pkg)
        deps.append({
            "package": pkg,
            "import_name": import_name_for(pkg),
            "ok": ok,
            "needed_by": sorted(needed[pkg]),
        })
        if not ok:
            missing.append(pkg)

    # chromedriver is an external binary, not a pip package, so it gets its
    # own key rather than a row in `deps`. Its ABSENCE is the healthy state,
    # so it never lands in `missing` and never makes all_ok false — it would
    # otherwise report a correctly configured machine as broken.
    driver = chromedriver_on_path()
    driver_advice = chromedriver_advice(driver, browser)

    advice = []
    if missing:
        advice = [
            "Install the missing package(s) with:",
            "{} -m pip install {}".format(
                "python" if sys.platform.startswith("win") else "python3",
                " ".join(missing),
            ),
            "If you have multiple Python installations, run the command with "
            "the same Python that launched this suite ({}).".format(
                sys.executable
            ),
            "On macOS, if pip reports an 'externally managed environment', "
            "add:  --user   (or use a virtual environment).",
            "After installing, click “Check dependencies” again — no "
            "restart needed.",
        ]

    return {
        "all_ok": not missing,
        "chromedriver": driver,
        "chromedriver_advice": driver_advice,
        "module_count": len([m for m in modules if not m.get("error")]),
        "deps": deps,
        "missing": missing,
        "advice": advice,
        "python": sys.version.split()[0],
    }
