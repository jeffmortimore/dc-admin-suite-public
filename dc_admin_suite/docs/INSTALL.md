# Installing the DC Admin Suite

## 1. Get the suite

Download the release zip from the repository's Releases page and unzip it,
or clone the repository. Either way you have a folder with
`dc_admin_suite/` inside it.

## 2. Open a terminal in the suite's folder

Every command in this guide is typed in a terminal that is already in the
`dc_admin_suite` folder (the one with `launch.py` in it).

- **Windows:** in File Explorer, open the `dc_admin_suite` folder, click
  the address bar at the top, type `cmd`, and press Enter. A Command Prompt
  opens already in that folder.
- **macOS:** in Finder, right-click the `dc_admin_suite` folder and choose
  **New Terminal at Folder**. If that item is not in the menu, open
  Terminal, type `cd ` (with a space after it), drag the folder onto the
  Terminal window, and press Enter.

On Windows, type `python` wherever this guide shows `python3`.

## 3. Check Python

You need Python 3.9 or newer.

- macOS: `python3 --version`
- Windows: `python --version`

**On Windows, if you see** *"Python was not found; run without arguments
to install from the Microsoft Store…"*, Python is not installed; that
message comes from a Windows shortcut, not from Python. Install Python from
[python.org](https://www.python.org/downloads/), and on the installer's
first screen tick **Add python.exe to PATH**.

**After installing Python, close the Command Prompt and open a new one in
the folder (step 2).** A window that was already open does not see the new
Python.

## 4. Start the suite

From the `dc_admin_suite` folder:

- macOS: `python3 launch.py`
- Windows: `python launch.py`

Your browser opens the start page at `http://127.0.0.1:8750`. Leave the
terminal window open; the suite stops when you close it or press Ctrl+C.

## 5. Follow the start page

1. **Your repository.** Enter your Digital Commons address, for example
   `https://digitalcommons.example.edu`. If you mint DOIs, enter your
   Crossref prefix too.
2. **Dependencies.** Click **Check dependencies**. For each module it
   lists what is missing and the exact `pip install` command, using the
   same Python that started the suite. Run it in a second terminal, then
   click **Check dependencies** again; no restart is needed.
3. **Chrome.** Start a separate Chrome just for the suite, with its own
   profile and a debugging port. You do not need to close your other Chrome
   windows. The start page shows the command for your computer:

   macOS:
   ```
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/DCAdminSuiteChrome"
   ```
   Windows (Windows key + R, `cmd`, Enter):
   ```
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\DCAdminSuiteChrome"
   ```
4. **Log in.** In that new Chrome window, log into Digital Commons as an
   administrator.
5. **Connect.** Click the connect button on the start page. It checks that
   it can reach that Chrome window and that a session opens.

Then go to the Main Menu and open a module.

## 6. Choose where files go

Modules that download files or write reports ask for a folder. Choose one
yourself rather than accepting the default:

- **Create the folders first**, then enter their full paths, for example
  `C:\DC\downloads` and `C:\DC\reports` on Windows, or
  `/Users/you/DC/downloads` on macOS.
- **On Windows, keep the paths short.** Windows limits a file's full path
  to 260 characters, and downloaded files carry long names. The downloader
  checks this before it starts.
- **On a managed Windows computer, your Desktop may be stored in
  OneDrive.** The default `~/Desktop` then points to a different, local
  Desktop folder from the one you see, and your reports seem to vanish.
  An explicit folder avoids this.

## 7. Make it yours (optional)

- **Look and feel:** Settings → Look & feel sets your institution's name,
  logo and colors. Header text switches between white and black to keep
  WCAG AA contrast.
- **A profile in one file:** copy `config.example/profile.json` to
  `config/profile.json`, fill it in, and restart the suite. See
  `profiles/README.md`.
- **Crossref deposit identity:** Settings → Crossref deposit identity. The
  DOI & XML Generator will not build a deposit until this is set.
- **AI endpoints:** Settings → AI endpoints, for the image describer and
  the OCR toolkit's AI option. Add your own key; keys are metered per
  person.

## Sharing the suite with colleagues

Do not zip the folder by hand: `config/` holds your API keys in plain text.
Build a clean copy instead:

```
python3 docs/make_distribution.py --staff
```

It copies the repository, blanks every key, drops this computer's session
file and caches, and refuses to finish if any of them survive.

## Checking an install

These are the checks CI runs. Run them from the `dc_admin_suite` folder
(step 2); from anywhere else they cannot find their files.

```
python3 -m compileall -q app modules launch.py docs
python3 modules/_verify_file_downloader.py
python3 modules/_verify_image_describer.py
python3 modules/_verify_ocr_toolkit.py
python3 modules/_verify_pages.py
python3 docs/check_docs.py
```

**What the output means:**

- **The downloader check prints a lot that looks alarming.** It drives the
  module through failures on purpose, and the module's own log appears as
  it goes: `ERROR`, `HTTP 429`, `HTTP 403`, a long run of `line 0`,
  `line 1`, and so on.
  Those are the tests at work. **Only the last two lines count:**
  `passed: N` and `failed: 0`.
- **Node.js is optional.** Without it, `_verify_pages.py` stops at once
  with "node is not on PATH", five image describer tests are reported as
  skipped, and the downloader runs fewer checks, so its passed total is
  lower. None of these means the install is broken. Install Node.js from
  [nodejs.org](https://nodejs.org/) to run every check.

## Troubleshooting

- **Can't connect to Chrome:** start it with the exact command above. The
  separate `--user-data-dir` profile is required by current Chrome. The
  port in the command must match Settings → Chrome debug connection.
- **"The system cannot find the path specified" (Windows):** Chrome is
  installed somewhere else. Find `chrome.exe` and use that path.
- **A module won't open:** run it by hand to see its error:
  `python3 modules/<file>.py --port 8751 --session config/session.json`
- **Chrome updated and a module stopped connecting:** remove any
  `chromedriver` you installed by hand. Selenium 4.6 and later fetches a
  matching driver itself, and a hand-installed one goes stale.
