# Release notes

## 1.0.4 — 2026-10-19

Every field that asks for a folder or a file now has a **Browse…** button
beside it, which opens your computer's own folder or file dialog. You can
still type or paste a path. Upgrading changes none of your settings.

1.0.2 and 1.0.3 were not published separately: if you are upgrading from
1.0.1, this release also carries everything in their notes below.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.5.0 |
| Hierarchy Mapping Tool | 1.5.0 |
| Structure Regenerator | 1.5.0 |
| Configuration Manager | 1.5.0 |
| DOI & XML Generator | 1.10.0 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.5.0 |
| Batch File Downloader | 1.37.0 |
| OCR & Accessibility Toolkit | 1.12.0 |
| Image Description Generator | 1.10.0 |
| Shell (start page, settings, Main Menu) | 1.13.0 |

### Browse… beside every path field

- **Folder fields** have **Browse…**. The dialog opens at the folder
  already in the field, or the nearest folder above it that exists, or
  your home folder.
- **The image and file path lists** (Image Description Generator, OCR &
  Accessibility Toolkit) have **Browse…** too. You can choose several
  files at once; they are added to the list, and any already listed are
  skipped.
- **The Batch File Downloader's re-run** takes a report or a run folder,
  so it has **Choose file…** and **Choose folder…**.
- A chosen path is treated exactly as if you had typed it: the page
  remembers it and reacts to it.
- The dialog can take a second or two to appear, and on some computers it
  may open behind the browser window. While it is open, the button reads
  "Choosing…".
- On macOS the dialog comes from `osascript`; on Windows it is the
  standard Windows file dialog, opened through PowerShell, which can
  also choose folders and has an address bar you can paste a path into;
  elsewhere it comes from Python's `tkinter`. If none of these works on your
  computer, the buttons are not shown and the page says why; type the path
  instead.

## 1.0.3 — 2026-10-14

The two AI modules now say which model answered each request. An AI
provider can retire a model and send requests that name it to a newer
model without any error or warning; until this release, the sidecars and
reports would still have named the model you asked for. Upgrading
changes none of your settings.

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
| Batch File Downloader | 1.36.0 |
| OCR & Accessibility Toolkit | 1.11.0 |
| Image Description Generator | 1.9.0 |
| Shell (start page, settings, Main Menu) | 1.13.0 |

### Image Description Generator and OCR & Accessibility Toolkit

- **Every sidecar names the model that answered.** Below the `Model:` line
  (the model you asked for), a `Served by:` line gives the model the
  provider says answered. When the provider does not say, the line says
  so. (Image Description Generator.)
- **The report has a Served Model column**, and the Summary sheet has a
  **Model served** line. When the provider answered with a different
  model from the one requested, the line says so in words: "Requested A,
  but the provider says B answered." A dated version of the same model
  (for example, `-001` or `-20261001` added to the end of the name)
  counts as the same model. The same line is written to the log at the
  end of a run.
- **The endpoint Test says which model answered**, on the Settings page
  and in each AI module's endpoint list.

### If you use Gemini

`gemini-3.7-flash` has been retired, and requests that name it are now
answered by `gemini-3.8-flash`. Update the model in **Settings → AI
endpoints**; the Test button will confirm which model answers.

## 1.0.2 — 2026-10-12

Fixes to the Batch File Downloader, found on real collections. Only that
module changes; every other version is as in 1.0.1.

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
| Batch File Downloader | 1.36.0 |
| OCR & Accessibility Toolkit | 1.10.0 |
| Image Description Generator | 1.8.0 |
| Shell (start page, settings, Main Menu) | 1.12.2 |

### Batch File Downloader

- **Inventory only works again.** In 1.0.1 an inventory stopped with
  "Unexpected error" at the end of the first structure that had records,
  before its plan was written. The rows it had gathered were still saved in
  a `_PARTIAL` report, and that report re-runs normally. An inventory now
  writes its plan for every structure, and the page says **Start
  inventory** and **Inventorying…** instead of speaking of downloads.
- **A file is kept once, even when the server holds it twice.** Two
  versions of one record can be the same bytes: a stamped PDF identical
  to its unstamped copy, or an earlier revision identical to the current
  one. After both are saved, the second copy is removed; its row still
  begins "Downloaded" and names the file that holds its bytes. Files that
  only share a size are both kept. If a copy cannot be removed, both stay
  and the row says so.
- **Stop works during a listing read.** A large listing can be a single
  request of a minute or more, and Stop used to wait for it.
- **The status line says what the run is doing** — checking the tab, the
  preflight, checking the site page, reading a listing — instead of
  "Attaching to Chrome…" until the first record finished. A re-run reads
  "Starting…" until its preflight is done, as a new run always has.
- **A structure with no records is named in the log**, and a run started
  long after the previous one no longer opens with "Still working".
- **The verification suite checks more and says more.** Checks that cannot
  run without an institution profile are listed under SKIPPED instead of
  counted as passes, and four checks that recorded nothing when the code
  under test returned early now fail.

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
