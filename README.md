# DC Admin Suite

A set of browser-based tools for administrators of a **Digital Commons**
repository (bepress). It runs on your own computer and works through a
Chrome window that **you** start and log into as an administrator. The
suite never opens Chrome for you, never sees your password, and never
closes your browser.

Ten modules ship in this release. Each is one Python file and runs on its
own local port. The modules that work through many records write a
filterable Excel (`.xlsx`) report of what they did.

**Version 1.0.1** — see [release notes](dc_admin_suite/docs/RELEASE-NOTES.md).
**Install:** see [the install guide](dc_admin_suite/docs/INSTALL.md).
**License:** MIT. Copyright (c) 2026 Jeffrey Mortimore.

## The modules

"Repository" says whether the module changes anything on your Digital
Commons site. **Read only** modules look and report. **Changes it** modules
act on your site, so try them on a small selection first.

| Module | File | Version | Needs Chrome | Repository | What it does, and its known limits |
|---|---|---|---|---|---|
| Structure URL Generator | `dc_url_scraper.py` | 1.4.0 | yes | read only | Exports every structure's homepage, configuration and regeneration URLs to a spreadsheet. |
| Hierarchy Mapping Tool | `dc_hierarchy_mapper.py` | 1.4.0 | yes | read only | Exports the structure hierarchy to a sortable spreadsheet. Several other modules start from this report. |
| Structure Regenerator | `dc_regenerator.py` | 1.4.0 | yes | **changes it** | Regenerates the selected structures, children before parents. Regeneration republishes pages on your site. |
| Configuration Manager | `dc_config_manager.py` | 1.4.0 | yes | **changes it** | Scrapes structure configurations to a spreadsheet, previews the changes you make in it (a dry run), and applies them. Apply only runs a current dry-run plan. |
| DOI & XML Generator | `dc_doi_xml.py` | 1.9.0 | no | read only | Builds DOIs for you to add to articles, and bepress, Crossref 5.4.0 and DOAJ XML from your public OAI-PMH feed. It refuses to build a Crossref deposit until you have set who the deposit is credited to. |
| Structure HTML Drafting Tool | `dc_field_drafter.py` | 1.6.3 | no | read only | Drafts structure HTML from editable templates. Ships with nine example templates; your own replace them. |
| Batch Revise Manager | `dc_batch_revise.py` | 1.4.0 | yes | **changes it** | Generates, downloads and uploads batch revise spreadsheets for the selected structures. Upload submits each revised spreadsheet through the structure's own form. |
| Batch File Downloader | `dc_file_downloader.py` | 1.35.0 | yes | read only | Downloads primary, native, supplemental, hidden and earlier-version files for the selected structures, with a report of every file and its access state. A stopped run records what it never reached and can be resumed from its own report. See "Downloading files" below. Not yet measured on a collection of several thousand records. |
| OCR & Accessibility Toolkit | `dc_ocr_toolkit.py` | 1.10.0 | no | local files only | Evaluates PDFs, images, Word and PowerPoint files against automated WCAG 2.1 AA checks, adds OCR text layers, and reports what still needs a person. **Known limits:** the "Tagged" column only checks that tags are present, and can be wrong on real files; "Alt Missing" is read from the files' text rather than from their structure; "AI Pages" counts pages sent to the AI endpoint, not pages successfully transcribed. |
| Image Description Generator | `dc_image_describer.py` | 1.8.0 | no | local files only | Writes reviewable alt-text and transcription sidecar files for folders of scanned images, using a Gemini, OpenAI-compatible or Claude endpoint with your own API key. Every description is a draft for a person to review. |

"Local files only" modules read and write files on your computer. The two
AI-assisted modules send each image or page to the AI endpoint **you**
configure, and nowhere else.

## What the suite never does

- **It never handles your credentials.** You log into Digital Commons in
  your own Chrome window; the suite attaches to that window.
- **It never disguises automation.** No altered user agent, no borrowed
  cookies, no attempt to answer a "verify you are human" prompt. If your
  repository asks whether a person is present, a person answers.
- **It never listens beyond your computer.** Every module serves only
  `127.0.0.1` and refuses requests that did not come from a local page.
- **It never ships your keys.** API keys live in `config/`, which git
  ignores. `docs/make_distribution.py` builds a copy to share with every key
  blanked.

## Downloading files

The Batch File Downloader fetches files by navigating your Chrome window,
the way you would by hand. Your repository may at times ask you to verify
that you are present. When it does, the run pauses, the module's page and
its browser tab title say so, and the run waits until you clear the prompt
in Chrome. Each prompt is recorded in the run's report. A run should be
watched by a person for its whole length.

Before running bulk downloads, check what your platform agreement and your
institution's policies say about them.

## Requirements

- Python 3.9 or newer, on macOS or Windows.
- Google Chrome.
- Per module: `selenium`, `beautifulsoup4` and `openpyxl` for the modules
  that use Chrome; `pymupdf`, `pikepdf`, `ocrmypdf`, `python-docx` and
  `python-pptx` (plus Tesseract and Ghostscript) for the OCR toolkit;
  `openpyxl` and `pymupdf` for the image describer. The suite's start page
  checks each module and prints the exact `pip install` command it needs.
- An API key from an AI provider, only for the image describer and the OCR
  toolkit's AI option.

## Tested on

Continuous integration runs every verification suite on Ubuntu and Windows
with Python 3.9 and 3.13. The maintainer runs the suite on macOS with
Python 3.14. Before release it was also installed on two Windows computers
with Python 3.14: a managed workstation under enterprise Chrome policy,
and a personal laptop that had no Python until the install. The release
notes have the details.

## Layout

```
README.md
LICENSE
.github/workflows/ci.yml       the release checks, on every push
dc_admin_suite/
  launch.py                    starts the suite
  app/                         the shell: start page, settings, Chrome bridge
  modules/                     one file per module (see modules/README.md)
  config.example/              the neutral starting profile and templates
  profiles/                    how to write your institution's profile
  docs/                        install guide, release notes, build tools
```

`config/` is created on first launch and holds your settings, templates and
keys. It never leaves your computer unless you copy it.

What each part of the shell is, for anyone reading or changing it:

| File | Role |
|---|---|
| `app/main.py` | starts the shell's server and opens the start page |
| `app/server.py` | the shell's local web server and its endpoints |
| `app/ui.py` | the start page, Main Menu and Settings pages |
| `app/settings.py` | reads and writes `config/settings.json` and the profile |
| `app/chrome_bridge.py` | attaches to your debug Chrome and checks the connection |
| `app/dependencies.py` | checks each module's packages and `chromedriver` |
| `app/module_registry.py` | finds the modules and starts or returns to them |
| `app/ai_endpoints.py` | the AI endpoint list under Settings → AI endpoints |
| `modules/_template_module.py` | a starting point for a new module |
| `modules/_verify_*.py` | the verification suites CI runs |

## Contributing

Issues and pull requests are welcome. Before opening a pull request, run
the checks CI runs (they are listed in the install guide).
