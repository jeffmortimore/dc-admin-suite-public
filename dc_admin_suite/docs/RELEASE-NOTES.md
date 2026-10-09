# Release notes

## 1.0.1 — 2026-10-08

Fixes from the first weeks of use, and the run history taken out of the
code's comments.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.4.0 |
| Hierarchy Mapping Tool | 1.4.0 |
| Structure Regenerator | 1.4.0 |
| Configuration Manager | 1.4.0 |
| DOI & XML Generator | 1.9.0 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.4.0 |
| Batch File Downloader | 1.35.0 |
| OCR & Accessibility Toolkit | 1.10.0 |
| Image Description Generator | 1.8.0 |
| Shell (start page, settings, Main Menu) | 1.12.2 |

### Shared by every module that runs a job

- **A finished run says how it went.** Green when it did what you asked
  (including a Stop you pressed), **amber** when it finished with problems
  to look at, and red when it ended in error or nothing it tried
  succeeded. Amber is new, and meets WCAG 2.1 AA contrast like the rest.
- **Your settings are remembered across a restart.** Each page keeps its
  settings in a file beside the suite's configuration, so a restart no
  longer puts a profile, an endpoint or a folder back to its default.
  Clear for a new run keeps them, as before.
- **No folder field has a default.** The pages used to fill folder fields
  with `~/Desktop`, which on a computer whose Desktop is stored in OneDrive
  is not the Desktop you see. A run now waits until you choose a folder,
  and a blank or missing one is refused with a message that names the
  field.

### Image Description Generator and OCR & Accessibility Toolkit

- **The reply limit is on the form, default 8,000.** It was part of each
  description profile, and the factory "Alt text only" profile used 300.
  On a thinking model the limit counts the model's reasoning, and the
  reasoning varied by a factor of four on the same image between two
  runs, so 300 refused most images. The OCR toolkit's alt-text drafts were
  capped at 300 too; one setting now covers pages and drafts.
- **A failed image or page reports what it was billed.** A reply the
  provider cut off is still charged; its tokens now appear in the report,
  and a retried request reports every attempt.
- **"Uncertain: Uncertain: None." is read as no uncertain readings.** A
  reply that repeated the label was counted as listing a doubt.

### Batch File Downloader

- **The Chrome bridge line leads with its verdict**, and no longer says
  FAILED in front of a connection that got the timeout it asked for.
- **Comments and the page's explanations describe what was measured, not
  where.** The explanation of the session limits no longer says they
  default to anything but 0.

### Checks

- A check that cannot run without Node.js is listed under `SKIPPED`
  rather than counted as passed or left out silently.
- The page checks drive each module through a restart, a blank folder
  and a finished run of each color.

### Known limits

- **OCR & Accessibility Toolkit:** "Tagged" checks only that tags are
  present and can be wrong on real files. "Alt Missing" is read from text,
  not structure. "AI Pages" counts pages sent, not pages transcribed.
- **Batch File Downloader:** not yet measured on a collection of several
  thousand records. A run needs a person present for its whole length, to
  answer any prompt asking whether one is.
- **Structure HTML Drafting Tool:** ships nine example templates.
- **Windows:** tested on two computers and in CI (see 1.0.0, below). Not
  yet tested with a long run on a managed workstation.

## 1.0.0 — 2026-10-03

The first public release.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.3.3 |
| Hierarchy Mapping Tool | 1.3.2 |
| Structure Regenerator | 1.3.2 |
| Configuration Manager | 1.3.2 |
| DOI & XML Generator | 1.8.2 |
| Structure HTML Drafting Tool | 1.6.2 |
| Batch Revise Manager | 1.3.2 |
| Batch File Downloader | 1.34.3 |
| OCR & Accessibility Toolkit | 1.9.2 |
| Image Description Generator | 1.7.0 |
| Shell (start page, settings, Main Menu) | 1.12.1 |

The suite version (1.0.0) names this release as a whole. Each module keeps
its own version, which is what its page footer shows.

### Shared by every module that runs a job

That is nine of the ten; the HTML drafting tool runs no jobs.

- **One run at a time.** A second Start while a run is active is refused.
- **Copy log copies the whole current run**, however far the page has
  scrolled. **Clear for a new run** empties the log, status and last
  summary, and keeps your settings and anything you have loaded.
- **A stopped run keeps its work.** It writes a `_PARTIAL` report.
- **One color scheme for messages:** gray for instructions, green when the
  state is what you intended, red for errors and warnings. Every color
  meets WCAG 2.1 AA contrast.
- **Messages appear above the form**, and a dialog shows its own errors
  inside the dialog.

### Known limits

- **OCR & Accessibility Toolkit:** "Tagged" checks only that tags are
  present and can be wrong on real files. "Alt Missing" is read from text,
  not structure. "AI Pages" counts pages sent, not pages transcribed.
- **Batch File Downloader:** not yet measured on a collection of several
  thousand records. A run needs a person present for its whole length, to
  answer any prompt asking whether one is.
- **Structure HTML Drafting Tool:** ships nine example templates.
- **Windows:** tested on two computers, below, and in CI. Not yet tested
  with a long run on a managed workstation.

### Tested on

CI: Ubuntu and Windows, Python 3.9 and 3.13, on every push. The maintainer:
macOS, Python 3.14.

Two Windows installs before release, both with Python 3.14:

- **A managed workstation**, under enterprise Chrome policy, with the
  Desktop redirected to OneDrive. Chrome started with remote debugging and
  the suite connected to it. The downloader's preflight passed, and an
  11-record structure downloaded through Chrome. The image describer
  described images through an AI endpoint. Nothing was blocked or warned
  about by SmartScreen, the firewall, antivirus or Chrome policy.
- **A personal laptop with no Python on it**, installed from the public
  copy by following the install guide. The dependency check, the
  verification suites and `check_docs` all passed. What a newcomer found
  hard there is now in the guide.
