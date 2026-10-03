"""
AI endpoints — the suite-level registry of vision/text AI endpoints.

Promoted to the shell in v1.3. Before that, two modules (the OCR &
Accessibility Toolkit and the Image Description Generator) both read and
wrote `config/ocr_toolkit.json` — a file named for one of them but owned, in
practice, by neither. Adding a third AI module would have made that worse,
and rotating a key meant remembering which module's UI to open.

This follows the precedent set by `crossref_prefix`: a value more than one
module depends on belongs to the shell, so there is exactly one authoritative
copy and one place to edit it.

WHERE THE KEYS LIVE, AND WHERE THEY DO NOT
------------------------------------------
Endpoints live in `config/ai_endpoints.json`, deliberately NOT in
`settings.json` and NOT in `session.json`:

  · settings.json is transportable and is the file copied into the generic
    distribution build for other institutions. An API key must never ride
    along with it.
  · session.json is rewritten at every module launch and handed to every
    module. It carries only the PATH to this file, never its contents, so
    keys are not duplicated across the suite each time a module starts.

Modules read this file themselves, per request, which also means a key added
or rotated in Settings is visible to an already-running module immediately.

Keys are stored in plain text — the UI says so. That is a deliberate,
documented limitation, not an oversight: the suite has no keychain of its
own, and a key encrypted with a key stored next to it would only look safer.
"""

import json
import urllib.error
import urllib.request

from . import settings as settings_mod

AI_KINDS = ("gemini", "openai", "anthropic")
DEFAULT_AI_CONFIG = {"endpoints": [], "default": ""}

# Where the two AI modules kept this list before it was promoted. Read once,
# to migrate; never written to again.
LEGACY_FILE_NAME = "ocr_toolkit.json"


def endpoints_file():
    return settings_mod.CONFIG_DIR / "ai_endpoints.json"


def legacy_file():
    return settings_mod.CONFIG_DIR / LEGACY_FILE_NAME


def normalize(cfg) -> dict:
    """Coerce any saved shape into the canonical one. Fields added in later
    versions get defaults here, so an older file never needs a hand-edit."""
    out = dict(DEFAULT_AI_CONFIG)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in out if k in cfg})
    eps = []
    for ep in (out.get("endpoints") or []):
        if not isinstance(ep, dict):
            continue
        kind = str(ep.get("kind", "anthropic")).strip().lower()
        eps.append({
            "name": str(ep.get("name", "")).strip(),
            "kind": kind,
            "url": str(ep.get("url", "")).strip(),
            "model": str(ep.get("model", "")).strip(),
            "api_key": str(ep.get("api_key", "")).strip(),
            # Prompt caching is Anthropic-only — `cache_control` appears
            # nowhere but the anthropic request builder. Gating it here and
            # not only in the UI means a hand-edited config, or one written
            # before the UI hid the control, cannot carry a true that no
            # code path will ever read. The stored file matches what runs.
            "prompt_cache": (bool(ep.get("prompt_cache", False))
                             if kind == "anthropic" else False),
        })
    out["endpoints"] = eps
    out["default"] = str(out.get("default", "")).strip()
    if out["default"] and out["default"] not in [e["name"] for e in eps]:
        out["default"] = ""
    return out


def validate(cfg) -> str:
    """Return "" when valid, else a user-facing message (HTTP 400)."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("endpoints"),
                                                   list):
        return "Bad endpoint registry format."
    names = set()
    for ep in cfg["endpoints"]:
        if not isinstance(ep, dict):
            return "Bad endpoint entry."
        name = str(ep.get("name", "")).strip()
        if not name:
            return "Every endpoint needs a name."
        if name.lower() in names:
            return "Duplicate endpoint name: " + name
        names.add(name.lower())
        if str(ep.get("kind", "")).strip().lower() not in AI_KINDS:
            return "Endpoint '{}': kind must be one of {}.".format(
                name, ", ".join(AI_KINDS))
        url = str(ep.get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            return "Endpoint '{}': URL must start with https://".format(name)
        if not str(ep.get("model", "")).strip():
            return ("Endpoint '{}': model is required (for Azure, use the "
                    "deployment name).".format(name))
    default = str(cfg.get("default", "")).strip()
    if default and default not in [str(e.get("name", "")).strip()
                                   for e in cfg["endpoints"]]:
        return "Default endpoint '{}' is not in the list.".format(default)
    return ""


def _migrate_from_legacy() -> dict:
    """One-time lift of endpoints out of the old per-module file.

    The legacy file is left untouched: the OCR toolkit's other settings live
    in it, and an older copy of a module should keep working if a user rolls
    one back.
    """
    try:
        raw = json.loads(legacy_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_AI_CONFIG)
    cfg = normalize(raw)
    if cfg["endpoints"]:
        save(cfg)
    return cfg


def load() -> dict:
    """The registry, migrating from the old per-module file on first run."""
    settings_mod.ensure_dirs()
    path = endpoints_file()
    if not path.exists():
        return _migrate_from_legacy()
    try:
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_AI_CONFIG)   # never crash the shell


def save(cfg: dict) -> dict:
    settings_mod.ensure_dirs()
    clean = normalize(cfg)
    endpoints_file().write_text(
        json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def find(cfg: dict, name: str):
    for ep in cfg.get("endpoints", []):
        if ep.get("name") == name:
            return ep
    return None


# ---------------------------------------------------------------------------
# Connection test. A deliberately tiny NON-streaming request: it exists to
# prove the URL, key and model are right, and a short answer cannot hit the
# idle-connection timeout that makes streaming necessary for real work. The
# full streaming client lives in the modules that do the real work.
# ---------------------------------------------------------------------------
TEST_PROMPT = "Reply with the single word: OK"
TEST_TIMEOUT = 30


def _test_request(ep):
    kind = (ep.get("kind") or "").lower()
    key = ep.get("api_key", "")
    if kind == "gemini":
        url = ep["url"].rstrip("/")
        if ":generatecontent" not in url.lower():
            url = "{}/models/{}:generateContent".format(url, ep["model"])
        return (url, {"Content-Type": "application/json",
                      "x-goog-api-key": key},
                {"contents": [{"parts": [{"text": TEST_PROMPT}]}]})
    if kind == "openai":
        url = ep["url"].rstrip("/")
        if "chat/completions" not in url.lower():
            url += "/chat/completions"
        return (url, {"Content-Type": "application/json",
                      "Authorization": "Bearer " + key, "api-key": key},
                {"model": ep["model"], "max_tokens": 16,
                 "messages": [{"role": "user", "content": TEST_PROMPT}]})
    if kind == "anthropic":
        url = ep["url"].rstrip("/")
        if not url.lower().endswith("/messages"):
            url += "/v1/messages"
        return (url, {"Content-Type": "application/json", "x-api-key": key,
                      "anthropic-version": "2023-06-01"},
                {"model": ep["model"], "max_tokens": 16,
                 "messages": [{"role": "user", "content": TEST_PROMPT}]})
    raise ValueError("Unknown endpoint kind: " + kind)


def _test_reply(kind, obj) -> str:
    try:
        if kind == "gemini":
            return "".join(
                p.get("text", "")
                for p in obj["candidates"][0]["content"]["parts"]).strip()
        if kind == "openai":
            return (obj["choices"][0]["message"]["content"] or "").strip()
        return "\n".join(b.get("text", "") for b in obj["content"]
                         if b.get("type") == "text").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def test_endpoint(ep) -> dict:
    """{"ok": True, "reply": "..."} or {"ok": False, "error": "..."}."""
    if not ep.get("api_key"):
        return {"ok": False, "error": "This endpoint has no API key."}
    try:
        url, headers, body = _test_request(ep)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TEST_TIMEOUT) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False,
                "error": "HTTP {} from endpoint: {}".format(e.code, detail)}
    except urllib.error.URLError as e:
        return {"ok": False,
                "error": "Could not reach endpoint: {}".format(e.reason)}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": "Request failed: {}".format(e)}
    reply = _test_reply((ep.get("kind") or "").lower(), obj)
    if not reply:
        return {"ok": False,
                "error": "The endpoint answered, but not in the shape this "
                         "kind expects — check the kind and model."}
    return {"ok": True, "reply": reply[:80]}
