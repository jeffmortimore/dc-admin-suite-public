# Release notes

## 1.0.8 — 2026-11-15

Digital Commons changed its administrator pages, and the site-wide
structure listing that six modules start from changed with them: the
table is now captioned **Site index**, and its rows are headed by plural
names (Communities, Journals, ETDs, Galleries, Series, Books,
Conferences) where they were headed by type codes. Every run that began
from the listing stopped at once with "Couldn't find the 'Contents of
this site' table", though nothing was wrong with the login or the
settings. Nothing was changed or downloaded by those runs.

- **Both layouts are read**, by one shared block in every module that
  reads the listing. Conferences are event communities, as before.
- **A row heading the module does not know is reported** in the log, with
  the number of structures under it, instead of being passed through as a
  type no filter can select.
- **When the listing is not on the page, the message says what was**: the
  page's title and its table captions, then the possible causes. It no
  longer asserts a login or settings problem it cannot see.

The same change reached the pages the Batch File Downloader and the Batch
Revise Manager read for each record and structure:

- **Record listings** (series, ETDs, events, books) now sit inside an outer
  layout table, which the downloader took for the listing: every such
  structure read as having **no records**, without an error. Fixed; the
  listing's "Next" link (which was ">") is followed too.
- **The release option is no longer in the listing.** The downloader now
  reads it from each record's Submission details page, where the structure
  has one ("Release Option", or "Document Type and Release Option"): one
  more administrator page per record, and the embargo date comes from the
  same page. Structures without one (galleries, most series) are not asked
  and their rows are marked n/a, as before. A record whose page will not
  load is reported with unknown access; its files are unaffected.
- **View revisions** puts the user, date and comment in one cell, and
  "Show Previous" is now a menu. Both are read; revision dates read as
  they did.
- **Batch revise's Spreadsheet History** moved its identifier from the
  table to a box around it, so generated spreadsheets could not be found
  for download. Fixed.

**The Configuration Manager's Apply no longer types values.** It entered
each changed value one key at a time, so a tab character in a value moved
to the next field, and a line break after it, in a one-line field, saved
the form with the value half entered — while the run reported the
structure as not updated. Now:

- **Values are set in the form directly** and read back before anything is
  saved; nothing is saved for a structure whose form will not hold a value
  (a one-line field cannot hold a line break).
- **After Save Changes the page is read again**, and the update log says
  what it holds, field by field, in a new **Read Back** column. Each
  structure's Status is Saved, PARTLY SAVED, NOT SAVED, NOT AS PLANNED or
  UNKNOWN; "Fields Changed" is now "Fields Planned".
- **A structure is regenerated only when every field was saved as
  planned.**
- **Each configuration page is read, set and saved only once its own
  scripts have finished.** Before, a dry run could list a placeholder in
  the disciplines list as a change on structures no one had edited. A
  page that does not finish within 15 seconds is skipped and named in the
  log.
- **Save Changes is pressed by a script click on its button**, which
  submits the form as the button does, and Apply waits for the page that
  follows to finish loading before it reads the field back. Before, a press while the browser
  window was covered by other windows could save nothing.
- **A save that does not show at once is read again.** The page read
  back right after Save Changes can still show the value from before the
  save; Apply then reads it again after about 2, 5 and 10 seconds, logs
  each read, and reports what the last read shows. The log also describes
  the page that answered Save Changes, and says when a regeneration was
  requested.
- **The disciplines list is recorded as the disciplines it holds** and
  compared by a dry run, but Apply never changes it: a difference is named
  in the log and left out of the plan.
- **A Save Changes press that changes nothing is tried once more**, and
  only when every planned field reads back unchanged.
- **Scan says what it looked for** when it finds nothing, and the
  workbook list keeps the loaded workbook selected after a Scan.
- **If you have applied changes to fields holding tab characters with an
  earlier version,** run a dry run against the report you scraped before
  to check them.

**The status line and the log, in every module with a log:**

- **Copy log is now Copy status and log.** It copies the status line
  first, with each download link as its name and the file's full path,
  then the whole log of the run. While a page is idle the status line is
  only an instruction, so the log alone is copied.
- **The status line moves on when a run ends.** In the Configuration
  Manager it stayed on "Harvesting…" after a harvest; in the Batch Revise
  Manager and the Structure Regenerator the idle line kept its first words
  after a hierarchy, a queue or a folder was loaded. Each now shows what
  to do next, and the Configuration Manager shows the harvest's summary.
- **The status line is written only when it changes.** It is announced to
  screen readers, and pages that rewrote it every second and a half could
  have it read out again each time.
- **A little more space between the log's buttons and the log.**

**The Batch File Downloader:**

- **It says Planning, not Downloading,** until the first file is
  requested.
- **Every record state's listing says how many records it holds,** or
  that it holds none. Before, a count was logged only when a listing ran
  to a second page.
- **A re-run with nothing left to do finishes green,** saying why, instead
  of showing a red error.
- **A re-run pointed at a run folder names the folders inside it that hold
  the plans and reports.** Each structure keeps its own in its own folder.
- **The Chrome bridge line no longer lists Selenium's internals** when the
  timeout is as asked.

**The start page** gives Windows a **PowerShell** command beside the
Command Prompt one, each with its own Copy button. The Command Prompt
command's `%LOCALAPPDATA%` does not work in PowerShell.

**Small:** "Regenerating 1 structure(s)", and a loaded workbook of one
structure says "1 structure".

Upgrading changes none of your settings.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.5.2 |
| Hierarchy Mapping Tool | 1.5.2 |
| Structure Regenerator | 1.6.2 |
| Configuration Manager | 1.5.8 |
| DOI & XML Generator | 1.10.1 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.6.3 |
| Batch File Downloader | 1.41.1 |
| OCR & Accessibility Toolkit | 1.13.1 |
| Image Description Generator | 1.12.4 |
| Shell (start page, settings, Main Menu) | 1.13.1 |

## 1.0.7 — 2026-11-01

Text equivalents now say where their record is, and can be checked against
it. Every sidecar the Image Description Generator writes opens with an
**accessibility notice** and, when the image's record is known, its
**Record URL**. Give the module a content inventory (a workbook with
`publication` and `manuscript_id` columns) and each image is first
described on its own, then checked against its record in a second request:
the record may settle a doubt or supply a name, place or date the image is
consistent with, and a **RECORD** section says what the record said. The
transcription is never changed. Upgrading changes none of your settings.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.5.0 |
| Hierarchy Mapping Tool | 1.5.0 |
| Structure Regenerator | 1.6.0 |
| Configuration Manager | 1.5.0 |
| DOI & XML Generator | 1.10.0 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.6.0 |
| Batch File Downloader | 1.40.0 |
| OCR & Accessibility Toolkit | 1.13.0 |
| Image Description Generator | 1.12.3 |
| Shell (start page, settings, Main Menu) | 1.13.0 |

### Image Description Generator

- **A new sidecar header.** The banner reads `TEXT EQUIVALENT     **AI
  GENERATED**`; `Source:` replaces `Source image:`; the `Status:` line is
  gone (the banner says it). `Record URL:` follows the Article ID when the
  image's record is known, and `Record check:` says when, by which
  endpoint, and with what verdict a record was consulted.
- **An ACCESSIBILITY NOTICE** comes before the alt text in every sidecar.
  Its wording is your institution's, from `branding.ai_notice` in your
  profile (see `profiles/README.md`), usually ending with whom to ask for
  more; without one, a neutral notice is written.
- **Record metadata workbook (optional)** and **Check against the record**
  (images whose description lists a doubt, or every image that has a
  record). Images are joined to records by context and article ID, never by
  the number in a record's URL. Only descriptive fields are sent (title,
  creators' names, dates, places, keywords, abstract, comments, and the
  like), never e-mail addresses, file data or existing alt text.
- **What the check may change is fixed in code:** the alt text, the
  description and the Uncertain line; a Reading line only by removing
  `[?]` marks. The transcription is kept from the first reading whatever
  the reply says. A check that fails leaves the first reading's sidecar,
  and the row says why. Each check is a second request.
- **Two report columns**, Record URL (a link) and Record Check, and a
  Record metadata line on the Summary. Where the first description listed a
  doubt, the check reads settled, partly or no help; where it listed none,
  agrees; either way, conflict when the record disagrees with the image.
  Otherwise the column says why no check ran, that the image has no record,
  or that the check failed.
- **The record's wording stays in the RECORD section.** The check may take
  names, places and dates from the record into the alt text and
  description, never its title or other wording; a reply that quotes the
  record there is reported on the row. A person the record credits is
  named in that role ("Creator (record)"), not as the photographer.
- **A place from the record goes in the description, not the alt text.**
  The collection already gives that context, and the alt text has 125
  characters to spend on the subject. A reply that adds a record's city,
  cemetery, church or location to the alt text is reported on the row.
- **A check cannot clear doubts the text still marks.** If the reply says
  there is no doubt ("None", "None remaining") while the Reading it keeps
  (or, with no Reading, the transcription) still marks text `[illegible]`
  or `[?]`, the first reading's Uncertain line is kept, and the row says
  so.

## 1.0.6 — 2026-10-30

The OCR & Accessibility Toolkit's report now measures what it says. A
PDF's **Tagged** column comes from a walk of its structure tree (Yes,
Incomplete with the reason, Empty tree, or No), and **Alt Missing** counts
the figures without alt text. If veraPDF is installed, each PDF is also
checked against PDF/UA-1 and the failed rules are listed. Processing a
scanned PDF no longer turns each page into a figure without alt text: the
scan is marked as decoration and its OCR text is tagged. A PDF that is
already tagged is never OCR'd, so its tags are kept. Both AI modules send a
file to the AI endpoint only when the downloader's report marks it public,
unless you turn on the override. Upgrading changes none of your settings.

1.0.5 was not published separately: if you are upgrading from 1.0.4, this
release also carries everything in its notes below.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.5.0 |
| Hierarchy Mapping Tool | 1.5.0 |
| Structure Regenerator | 1.6.0 |
| Configuration Manager | 1.5.0 |
| DOI & XML Generator | 1.10.0 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.6.0 |
| Batch File Downloader | 1.40.0 |
| OCR & Accessibility Toolkit | 1.13.0 |
| Image Description Generator | 1.11.0 |
| Shell (start page, settings, Main Menu) | 1.13.0 |

### OCR & Accessibility Toolkit

- **Tagged** reads Yes, Incomplete (untagged content, vector drawings
  outside the tags, tags pointing at missing content, MarkInfo not set),
  Empty tree, or No. Before, it only checked that a structure tree
  existed.
- **Auto-tag marks lines, rules, borders and fills as artifacts**
  (decoration), and Next Steps says how many: a chart or diagram drawn in
  lines needs a Figure with alt text instead.
- **Alt Missing** counts figures without alt text in a tagged PDF, images
  in an untagged one, and pictures in Word and PowerPoint files. A picture
  marked decorative in Word or PowerPoint is not counted. A page scan is
  not counted: it needs OCR, and the new **Page Scans** column counts it.
- **Placeholder alt text counts as missing.** A label an auto-tagger or
  exporter fills in ("image 18", "Figure 3", "Picture 3"), a file name, or
  text with no letters says nothing to a screen-reader user. Alt Missing
  counts it, Next Steps quotes one, and an AI draft replaces it in Word,
  PowerPoint and tagged PDFs (the review step says how many). A PDF/UA
  validator accepts it, because the rule asks only that alt text be there.
- **Files other tools tagged** are read as a validator reads them: a tag
  that opens inside a text object, and content marked inside a form, are
  tagged.
- **Alt text for PDFs that are already tagged.** With an AI endpoint and
  AI alt text on, Process drafts alt text into the figures of a tagged PDF
  that have none or only a placeholder; real alt text is never replaced.
  Each figure is drawn from the page as a reader sees it. A figure the AI
  judges decorative, and one that is only a line, is made an artifact
  (decoration). A page drawn as a stack of thin slices is not sent; Next
  Steps names it for a person. A figure showing the same image as another
  shares its draft. The file is saved only if the tags check no worse
  afterward; if not, nothing is saved and Next Steps says why.
- **Alt-text drafts per file** is a field (default 25). When a file needs
  more, the row says how many were not asked about; processing the output
  again drafts the next ones.
- **Tables without header cells** are named in Next Steps.
- **Headings and tables are checked for what a validator cannot see.**
  For every tagged PDF, a set of detectors names: no headings, few
  headings for the length, a first heading below H1, a skipped level,
  paragraphs styled as headings, a heading on a page of its own (check its
  level and its parent heading), a first-page title tagged as ordinary
  text, tables with no header cells or no data cells, one-row tables, one
  table on the page tagged as several, and images drawn inside text. A
  **Detector Findings** column counts them and a **Detectors** sheet lists
  each with its page and element. They read the structure only and change
  nothing: each finding is a question for a person.
- **veraPDF** (optional, needs Java): each PDF is checked against
  PDF/UA-1. **Checked By** says what checked the file and **Rule Failures**
  lists the failed rules; the Summary explains each one.
- **Tagging:** a page scan is marked as decoration and its OCR text is
  tagged; an image the AI judges decorative is marked as decoration (in
  Word and PowerPoint, the way Office marks it). The Auto-tag box now
  decides whether a file is tagged.
- **AI:** AI Pages Sent and AI Pages Transcribed are counted apart, and a
  request that failed is no longer described as a model that stopped.
  Stopping part-way through a file still reports that file.
- **Images outside the tags** of a tagged file — a document behind a
  tagged cover page — are counted in Alt Missing and named in Next Steps.
- **Doc Title** reads Present, Placeholder (a title a tool filled in, such
  as "Microsoft Word - …", or the file's own name) or Missing, and Set a
  document title replaces a placeholder as well as a blank.
- **The AI gate:** only files the downloader's report marks public (and
  open, or with no access option) go to the AI endpoint, unless you check
  **Also send hidden and restricted files**. A file the downloader named
  but no report describes is held back. If the downloader saved its
  reports to a folder of their own, choose it in **Reports folder, if saved
  elsewhere**; the first session's report and each re-run session's are
  both read. Other files from a plain folder are sent and marked
  "visibility not known". The **AI Gate** column says what the gate decided
  for each file the run could send, and the Summary counts the files whose
  transcription or alt text it held back. Evaluate only sends nothing; its
  Summary says how many files a processing run could not send.

### Batch File Downloader

- A downloaded file that another program holds open for a moment (on
  Windows, often a virus scanner reading the new file) is waited for, for
  up to about 15 seconds. If it is still held, that one file is recorded
  as failed, so the next session asks for it again, and the run goes on.
  Before, it ended the run.
- A record whose only file is its native (it has no PDF of its own) now
  gets a row for each planned file: the primary row says the native was
  saved, and the native row names that same file. Before, both planned
  rows came back in every later session and the native was downloaded
  again each time.
- A session of rows nobody has tried yet (a plan's sessions) says it is
  downloading them and how many it downloaded; only a re-run of rows that
  did not download says it recovered them. When more sessions remain, the
  closing line says to copy the session's report into the job folder
  first.
- When several files in a row come over the network layer because Chrome
  delivered nothing for them, the note says how many files Chrome has
  delivered in the run. It suggests that downloads may be blocked in the
  Chrome profile only when Chrome has delivered none.
- The explanation of the gap before a file download says what was
  measured: lockouts at a 0.3 second gap, and long sessions at 1 and
  0.5 seconds with none. The default stays 5 seconds.
- The Session limits section no longer says the repository caps downloads
  per day. It does not: a long run is broken into sessions because each
  one is watched, and the repository can ask whether a person is present.

### Image Description Generator

- The same AI gate, the same **Reports folder, if saved elsewhere** field,
  an **AI Gate** column, and a count of the images held back. Preview
  refuses an image the gate holds back.

## 1.0.5 — not published separately

The three modules that scope a run by parent structure — Structure
Regenerator, Batch Revise Manager and Batch File Downloader — now list the
parents you have checked, by name, under the count. Each has a
**Remove** button, and **Clear all** unchecks every one. A page that
reopens with parents already checked shows which ones, without a search for
each. Upgrading changes none of your settings.

### What is in it

| Module | Version |
|---|---|
| Structure URL Generator | 1.5.0 |
| Hierarchy Mapping Tool | 1.5.0 |
| Structure Regenerator | 1.6.0 |
| Configuration Manager | 1.5.0 |
| DOI & XML Generator | 1.10.0 |
| Structure HTML Drafting Tool | 1.6.3 |
| Batch Revise Manager | 1.6.0 |
| Batch File Downloader | 1.38.0 |
| OCR & Accessibility Toolkit | 1.12.0 |
| Image Description Generator | 1.10.0 |
| Shell (start page, settings, Main Menu) | 1.13.0 |

### Checked parents, by name

- Under the count, each checked parent is listed by name with its own
  **Remove** button. Removing one moves the keyboard focus to the next.
- **Clear all** unchecks every parent and returns the focus to the filter.
  It is unavailable when nothing is checked.
- The count is announced to screen readers when it changes.
- In the Batch File Downloader, which keeps its settings across a restart,
  a cleared list is kept too.

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
