# modules/

Drop-in admin modules for the DC Admin Suite live here — one self-contained
`.py` file per module.

**Install** a module: save its `.py` file into this folder (or use
Settings → Manage modules in the suite). **Remove** one: delete the file, or
use the Settings page, which moves it to `_removed/` so it can be restored.
The Main Menu picks up changes on its Refresh button — no restart needed.

Files starting with `_` are ignored by the suite (templates, helpers,
temporarily disabled modules).

To write a new module, copy `_template_module.py`, rename it without the
leading underscore, and edit it. The template documents the full module
contract (MANIFEST, `--port` / `--session` arguments, and the
`attach_chrome()` helper for connecting Selenium to the user's logged-in
debug Chrome).

Some modules keep editable reference data in `../config/` — description
profiles, field templates, the journal registry. Those files travel with the
suite and are edited in each module's own UI, never by hand.

**AI endpoints belong to the shell**, not to any module (since shell v1.3).
They live in `config/ai_endpoints.json` and are added, edited and tested in
**Settings → AI endpoints**; modules read the file per request, so a key
rotated there reaches an already-running module with no relaunch. The
pre-v1.3 location, `config/ocr_toolkit.json`, is migrated automatically on
first load and left untouched afterwards so an older module still works.

`config/ai_endpoints.json` holds keys in plain text. Never copy a suite
folder by hand to share it — see "Before you share this folder" in
`docs/README.md`, and use `docs/make_distribution.py`.
