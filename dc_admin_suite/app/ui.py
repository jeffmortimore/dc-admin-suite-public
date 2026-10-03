"""
Shell UI — a single accessible HTML page (Splash / Main Menu / Settings).

Served as one static string; all data arrives via /api/* calls, so this file
contains no server-side templating. Accessibility targets WCAG 2.1 AA:
semantic landmarks, skip link, labelled controls, aria-live status regions,
focus management on view changes, visible focus indicators, 4.5:1 contrast,
keyboard operability, and a layout that works from 320 px wide upward.
"""

APP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DC Admin Suite</title>
<style>
  :root{
    --primary:#1F4E79; --accent:#946B2D; --neutral:#9AA5AD;
    --on-primary:#ffffff;
    --ink:#1b1b1f; --paper:#f7f6f3; --card:#ffffff; --line:#d7d4cc;
    --ok:#1d6b34; --err:#a3252c; --warn:#8a5a00;
    --focus:#1a5dc8;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--ink); background:var(--paper); min-height:100vh;
  }
  /* ---- accessibility basics ---- */
  .skip-link{
    position:absolute; left:-9999px; top:0; background:var(--card);
    color:var(--ink); padding:.6rem 1rem; z-index:100; border:2px solid var(--focus);
  }
  .skip-link:focus{left:.5rem; top:.5rem}
  :focus-visible{outline:3px solid var(--focus); outline-offset:2px}
  @media (prefers-reduced-motion: reduce){
    *{animation:none!important; transition:none!important}
  }
  .visually-hidden{
    position:absolute!important; width:1px; height:1px; overflow:hidden;
    clip:rect(0 0 0 0); white-space:nowrap;
  }
  /* ---- header ---- */
  header{
    background:var(--primary); color:var(--on-primary);
    padding:.9rem 1.25rem; display:flex; flex-wrap:wrap; gap:.75rem 1.25rem;
    align-items:center;
  }
  header img.logo{max-height:44px; max-width:180px; background:#fff;
    padding:3px; border-radius:4px}
  header .titles{flex:1 1 14rem; min-width:0}
  header h1{margin:0; font-size:1.25rem; line-height:1.3}
  header .inst{margin:0; font-size:.85rem; opacity:.92}
  header nav a{
    color:var(--on-primary); margin-left:1rem; font-weight:600;
    text-decoration:underline; text-underline-offset:3px;
  }
  .accent-bar{height:5px; background:var(--accent)}
  /* ---- layout ---- */
  main{max-width:56rem; margin:0 auto; padding:1.25rem}
  section.card{
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:1.1rem 1.25rem; margin-bottom:1.1rem;
  }
  h2{margin:.1rem 0 .6rem; font-size:1.15rem; color:var(--primary)}
  h3{margin:.9rem 0 .4rem; font-size:1rem; color:var(--primary)}
  p{margin:.4rem 0}
  [hidden]{display:none !important}
  a{color:#0b4ea2}
  code,pre{font-family:ui-monospace,Consolas,"SF Mono",Menlo,monospace}
  pre.cmd{
    background:#eef1f5; border:1px solid var(--line); border-radius:6px;
    padding:.7rem .8rem; overflow-x:auto; white-space:pre-wrap;
    word-break:break-all; font-size:.85rem;
  }
  /* ---- forms & buttons ---- */
  label{display:block; font-weight:600; margin:.6rem 0 .2rem}
  .hint{font-size:.85rem; color:#4c4c52; margin:.15rem 0 .4rem}
  input[type=text],input[type=url],input[type=number]{
    width:100%; max-width:34rem; padding:.55rem .6rem; font-size:1rem;
    border:1px solid #767676; border-radius:6px; background:#fff; color:var(--ink);
  }
  input[type=color]{inline-size:3.2rem; block-size:2.2rem; padding:2px;
    border:1px solid #767676; border-radius:6px; vertical-align:middle}
  button{
    font-size:1rem; font-weight:600; padding:.55rem 1rem; border-radius:6px;
    border:2px solid var(--primary); background:var(--primary);
    color:var(--on-primary); cursor:pointer; margin:.35rem .5rem .35rem 0;
  }
  button.secondary{background:#fff; color:var(--primary)}
  button.danger{background:#fff; color:var(--err); border-color:var(--err)}
  button[disabled]{opacity:.55; cursor:not-allowed}
  button[aria-busy="true"]::after{content:" …"}
  fieldset{border:1px solid var(--line); border-radius:8px;
    padding:.6rem 1rem 1rem; margin:.8rem 0}
  legend{font-weight:700; color:var(--primary); padding:0 .35rem}
  details{border:1px solid var(--line); border-radius:8px; margin:.6rem 0;
    background:#fbfaf7}
  summary{cursor:pointer; font-weight:600; padding:.6rem .8rem}
  details[open] summary{border-bottom:1px solid var(--line)}
  details .inner{padding:.6rem .9rem}
  ol.steps{margin:.3rem 0 .6rem 1.2rem; padding:0}
  ol.steps li{margin:.3rem 0}
  /* ---- status / alerts ---- */
  .status{margin:.5rem 0; border-radius:8px; padding:.6rem .8rem; border:1px solid}
  .status:empty{display:none; padding:0; border:0}
  .status.good{background:#eaf5ec; border-color:var(--ok); color:#14522a}
  .status.bad{background:#fbecec; border-color:var(--err); color:#7c1c22}
  .status.warn{background:#fdf4e3; border-color:var(--warn); color:#6b4600}
  .status.info{background:#eef1f5; border-color:var(--neutral); color:#2c3440}
  .status ul{margin:.35rem 0 .1rem 1.2rem; padding:0}
  .status li{margin:.25rem 0}
  .pill{display:inline-block; font-size:.8rem; font-weight:700;
    border-radius:999px; padding:.1rem .6rem; border:1.5px solid}
  .pill.ok{color:var(--ok); border-color:var(--ok)}
  .pill.no{color:var(--err); border-color:var(--err)}
  .pill.na{color:#4c4c52; border-color:var(--neutral)}
  /* ---- splash stepper ---- */
  .step{border-left:5px solid var(--neutral); padding-left:1rem; margin:1.1rem 0}
  .step.done{border-left-color:var(--ok)}
  .step.active{border-left-color:var(--accent)}
  .step h3{margin-top:0}
  /* ---- modal dialog (AI endpoint editor) ---- */
  .modalback{position:fixed; inset:0; background:rgba(20,20,25,.45);
    display:none; align-items:flex-start; justify-content:center;
    padding:2rem 1rem; z-index:20; overflow:auto}
  .modalback.open{display:flex}
  .modal{background:#fff; border-radius:10px; max-width:40rem; width:100%;
    padding:1rem 1.2rem; border:1px solid var(--line)}
  .modal h3{margin:.1rem 0 .7rem}
  .modal .foot{display:flex; gap:.6rem; justify-content:flex-end;
    margin-top:1rem; flex-wrap:wrap}
  .rowbtn{background:none; border:0; color:#1a5dc8; text-decoration:underline;
    cursor:pointer; font:inherit; padding:0; text-align:left}
  /* ---- module cards ---- */
  ul.modules{list-style:none; margin:0; padding:0; display:grid; gap:.9rem;
    grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
  ul.modules li{border:1px solid var(--line); border-radius:10px;
    background:var(--card); padding:.9rem 1rem; display:flex;
    flex-direction:column; gap:.4rem}
  ul.modules li h3{margin:0}
  ul.modules li .desc{flex:1}
  ul.modules li .meta{font-size:.8rem; color:#4c4c52}
  /* ---- reorder controls (Manage Modules table) ---- */
  button.ord{font-size:.9rem; font-weight:700; line-height:1;
    padding:.3rem .55rem; margin:0; min-width:2.2rem}
  td.ordcell{white-space:nowrap; text-align:center}
  td.ordcell button.ord{margin:0 .15rem}
  /* Keep Module & File columns compact so Requires/Order/Action have room */
  th.col-module,td.col-module{width:22%}
  th.col-file,td.col-file{width:20%}
  /* ---- tables ---- */
  table{border-collapse:collapse; width:100%; margin:.5rem 0; font-size:.92rem}
  caption{text-align:left; font-weight:700; padding:.25rem 0}
  th,td{border:1px solid var(--line); padding:.4rem .6rem; text-align:left;
    vertical-align:top}
  th{background:#eef1f5}
  footer{max-width:56rem; margin:0 auto; padding:0 1.25rem 2rem;
    font-size:.85rem; color:#4c4c52}
  @media (max-width:480px){
    header{padding:.75rem .9rem} main{padding:.9rem}
    header nav a{margin-left:0; margin-right:1rem}
  }
/* DC-PALETTE 2 — one notification palette for the shell and every module.
   Gray: instructions and neutral state. Green: something succeeded.
   Red: errors and warnings. The same block, byte for byte, in every page;
   modules/_verify_pages.py fails the build if any copy differs, and checks
   each ink against its background for WCAG 2.1 AA contrast. It sits last in
   each page's <style>, so it wins over the older per-page colors. */
:root{--dc-info-bg:#eef1f5;--dc-info-line:#9aa5ad;--dc-info-ink:#2c3440;
 --dc-ok-bg:#eaf5ec;--dc-ok-line:#1d6b34;--dc-ok-ink:#14522a;
 --dc-bad-bg:#fbecec;--dc-bad-line:#a3252c;--dc-bad-ink:#7c1c22}
#status,#status.waiting,#cerr.notice,.sum,.sum.warn,.status.info{
 background:var(--dc-info-bg);border-color:var(--dc-info-line);color:var(--dc-info-ink)}
#status.done,#cerr.ok,.sum.loaded,.status.good,.notice{
 background:var(--dc-ok-bg);border-color:var(--dc-ok-line);color:var(--dc-ok-ink)}
#status.error,#cerr,.sum.bad,.status.bad,.status.warn,.notice.err{
 background:var(--dc-bad-bg);border-color:var(--dc-bad-line);color:var(--dc-bad-ink)}
.dc-ok{color:var(--dc-ok-ink)}
.dc-bad{color:var(--dc-bad-ink)}
.dc-info{color:var(--dc-info-ink)}
/* Version 2: an element marked hidden stays hidden, whatever display its
   class gives it. v1.34.2's Clear panel had display:flex, which beat the
   attribute, so it showed all the time. */
[hidden]{display:none!important}
/* /DC-PALETTE */
</style>
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header>
  <img id="brandLogo" class="logo" src="/branding/logo" alt="" hidden>
  <div class="titles">
    <h1 id="suiteName">DC Admin Suite</h1>
    <p class="inst" id="instName"></p>
  </div>
  <nav aria-label="Suite navigation">
    <a href="#/menu" id="navMenu">Main Menu</a>
    <a href="#/settings" id="navSettings">Settings</a>
  </nav>
</header>
<div class="accent-bar" role="presentation"></div>

<main id="main">

<!-- ======================= SPLASH ======================= -->
<section id="view-splash" aria-labelledby="splashTitle" hidden>
  <section class="card">
    <h2 id="splashTitle" tabindex="-1">Welcome — session setup</h2>
    <p>Revisit any of these settings later on the
       <a href="#/settings">Settings page</a>.</p>
  </section>

  <!-- Step 1: base URL -->
  <section class="card step" id="step1" aria-labelledby="s1h">
    <h3 id="s1h">Step 1 · Digital Commons instance</h3>
    <label for="splashBaseUrl">Base URL of the instance for this session</label>
    <p class="hint" id="baseUrlHint">Example: https://digitalcommons.example.edu</p>
    <input type="url" id="splashBaseUrl" aria-describedby="baseUrlHint"
           autocomplete="url" spellcheck="false">
    <div>
      <button id="saveBaseUrlBtn">Save base URL</button>
    </div>
    <div class="status" id="baseUrlStatus" role="status" aria-live="polite"></div>

    <label for="splashCrossrefPrefix" style="margin-top:1.1rem">Crossref DOI prefix</label>
    <p class="hint" id="splashPrefixHint">Your institution&rsquo;s registered
       Crossref prefix, used by DOI tools when minting or depositing DOIs.
       For example 10.12345. Blank until you set one.</p>
    <input type="text" id="splashCrossrefPrefix" aria-describedby="splashPrefixHint"
           spellcheck="false" autocomplete="off">
    <div>
      <button id="saveCrossrefPrefixBtn" class="secondary">Save Crossref prefix</button>
    </div>
    <div class="status" id="crossrefPrefixStatus" role="status" aria-live="polite"></div>
  </section>

  <!-- Step 2: dependencies -->
  <section class="card step" id="step2" aria-labelledby="s2h">
    <h3 id="s2h">Step 2 · Check module dependencies</h3>
    <p>Verify all dependencies are installed before connecting to Chrome.</p>
    <button id="depsCheckBtn1">Check dependencies</button>
    <div class="status" id="depsStatus1" role="status" aria-live="polite"></div>
  </section>

  <!-- Step 3: Chrome -->
  <section class="card step" id="step3" aria-labelledby="s3h">
    <h3 id="s3h">Step 3 · Start Chrome in debug mode and log in</h3>
    <p>Follow the instructions for your system to start Chrome and log in.</p>
    <details>
      <summary>Windows instructions</summary>
      <div class="inner" id="winInstructions"></div>
    </details>
    <details>
      <summary>Mac instructions</summary>
      <div class="inner" id="macInstructions"></div>
    </details>
    <p class="hint" id="chromeNote"></p>
    <p>Once Chrome is running in debug mode and you are logged in as
       administrator, click:</p>
    <button id="chromeCheckBtn1">I&rsquo;ve started Chrome and logged in — connect</button>
    <div class="status" id="chromeStatus1" role="status" aria-live="polite"></div>
  </section>

  <!-- Step 4: finish -->
  <section class="card step" id="step4" aria-labelledby="s4h">
    <h3 id="s4h">Step 4 · Setup complete</h3>
    <div class="status info" id="finishSummary" role="status" aria-live="polite"></div>
    <button id="finishBtn">Continue to Main Menu</button>
    <p class="hint"><strong>Note:</strong> <em>You can continue even if a
       check hasn&rsquo;t passed. Revisit any of these settings later on the
       Settings page.</em></p>
  </section>
</section>

<!-- ======================= MAIN MENU ======================= -->
<section id="view-menu" aria-labelledby="menuTitle" hidden>
  <section class="card">
    <h2 id="menuTitle" tabindex="-1">Main Menu</h2>
    <p>Installed modules run independently in their own tab.</p>
    <button id="refreshModulesBtn" class="secondary">Refresh module list</button>
    <a href="#/settings">Open Settings</a>
    <div class="status" id="menuStatus" role="status" aria-live="polite"></div>
  </section>
  <ul class="modules" id="moduleList" aria-label="Installed modules"></ul>
  <section class="card" id="noModules" hidden>
    <h3>No modules installed yet</h3>
    <p>Save module files into the <code>modules/</code> folder (or use the
       installer on the <a href="#/settings">Settings page</a>), then click
       “Refresh module list”.</p>
  </section>
</section>

<!-- ======================= SETTINGS ======================= -->
<section id="view-settings" aria-labelledby="settingsTitle" hidden>
  <section class="card">
    <h2 id="settingsTitle" tabindex="-1">Settings</h2>
    <p>Changes apply to the suite immediately; modules pick them up the next
       time they are launched. <a href="#/menu">Back to Main Menu</a></p>
  </section>

  <section class="card" aria-labelledby="setInstanceH">
    <h2 id="setInstanceH">Digital Commons instance</h2>
    <label for="setBaseUrl">Base URL</label>
    <input type="url" id="setBaseUrl" autocomplete="url" spellcheck="false">
    <div><button id="setSaveBaseUrlBtn">Save base URL</button></div>
    <div class="status" id="setBaseUrlStatus" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setCrossrefH">
    <h2 id="setCrossrefH">Crossref DOI prefix</h2>
    <p class="hint">Used by DOI tools (such as the DOI &amp; XML Generator)
       when minting or depositing DOIs. For example 10.12345. Blank until
       you set one.</p>
    <label for="setCrossrefPrefix">Prefix</label>
    <input type="text" id="setCrossrefPrefix" spellcheck="false" autocomplete="off">
    <div><button id="setSaveCrossrefPrefixBtn">Save Crossref prefix</button></div>
    <div class="status" id="setCrossrefPrefixStatus" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setCrossrefIdH">
    <h2 id="setCrossrefIdH">Crossref deposit identity</h2>
    <p class="hint">Who a DOI deposit is credited to. These are blank until
       you set them, and the DOI &amp; XML Generator will not build a Crossref
       deposit without them — that is deliberate, because a wrong depositor is
       far harder to notice than a missing one. Crossref emails deposit
       results to the depositor address.</p>
    <label for="setDepositorName">Depositor name</label>
    <input type="text" id="setDepositorName" spellcheck="false"
           placeholder="University Libraries, Example University">
    <label for="setDepositorEmail">Depositor email</label>
    <input type="email" id="setDepositorEmail" spellcheck="false"
           autocomplete="off" placeholder="repository@example.edu">
    <label for="setRegistrant">Registrant</label>
    <input type="text" id="setRegistrant" spellcheck="false"
           placeholder="Example University">
    <label for="setPublisher">Publisher</label>
    <input type="text" id="setPublisher" spellcheck="false"
           placeholder="Example University">
    <p class="hint">Publisher is used for conference proceedings deposits and
       for DOAJ article XML.</p>
    <div><button id="setSaveCrossrefIdBtn">Save deposit identity</button></div>
    <div class="status" id="setCrossrefIdStatus" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setChromeH">
    <h2 id="setChromeH">Chrome debug connection</h2>
    <label for="setChromeHost">Debugger host</label>
    <input type="text" id="setChromeHost" spellcheck="false">
    <label for="setChromePort">Debugger port</label>
    <input type="number" id="setChromePort" min="1" max="65535">
    <div>
      <button id="setSaveChromeBtn" class="secondary">Save connection settings</button>
      <button id="chromeCheckBtn2">Test connection</button>
    </div>
    <details>
      <summary>How to start Chrome in debug mode (Windows &amp; Mac)</summary>
      <div class="inner">
        <h3>Windows</h3><div id="winInstructions2"></div>
        <h3>Mac</h3><div id="macInstructions2"></div>
        <p class="hint" id="chromeNote2"></p>
      </div>
    </details>
    <div class="status" id="chromeStatus2" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setDepsH">
    <h2 id="setDepsH">Module dependencies</h2>
    <p>Re-scans all installed modules and checks every package they declare.</p>
    <button id="depsCheckBtn2">Check dependencies</button>
    <div class="status" id="depsStatus2" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setLookH">
    <h2 id="setLookH">Look &amp; feel</h2>
    <p>Customize the suite with your institution&rsquo;s name, logo, and
       colors. Reset restores this install&rsquo;s profile defaults — see
       profiles/README.md.</p>
    <label for="setSuiteName">Suite name</label>
    <input type="text" id="setSuiteName">
    <label for="setInstName">Institution name</label>
    <input type="text" id="setInstName">
    <fieldset>
      <legend>Colors</legend>
      <label for="colPrimary">Primary (header &amp; buttons)</label>
      <input type="color" id="colPrimary">
      <input type="text" id="colPrimaryHex" size="9" spellcheck="false"
             aria-label="Primary color hex value">
      <label for="colAccent">Accent (highlights)</label>
      <input type="color" id="colAccent">
      <input type="text" id="colAccentHex" size="9" spellcheck="false"
             aria-label="Accent color hex value">
      <label for="colNeutral">Neutral (rules &amp; muted elements)</label>
      <input type="color" id="colNeutral">
      <input type="text" id="colNeutralHex" size="9" spellcheck="false"
             aria-label="Neutral color hex value">
      <p class="hint">Header text automatically switches between white and
         black to keep WCAG-AA contrast against your primary color.</p>
    </fieldset>
    <fieldset>
      <legend>Logo</legend>
      <label for="logoFile">Upload logo (PNG, JPG, GIF, SVG, or WebP; max 2&nbsp;MB)</label>
      <input type="file" id="logoFile" accept=".png,.jpg,.jpeg,.gif,.svg,.webp">
      <div>
        <button id="logoClearBtn" class="danger">Remove current logo</button>
      </div>
    </fieldset>
    <div>
      <button id="saveBrandingBtn">Save look &amp; feel</button>
      <button id="resetBrandingBtn" class="secondary">Reset to default branding</button>
    </div>
    <div class="status" id="brandingStatus" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="setAiH">
    <h2 id="setAiH">AI endpoints</h2>
    <p class="hint">Vision/text AI endpoints used by modules that draft alt
       text, transcribe images, or OCR with an AI engine (the OCR &amp;
       Accessibility Toolkit and the Image Description Generator). Managed
       here so a key is added or rotated in <strong>one</strong> place;
       modules pick up a change immediately, without relaunching.</p>
    <p class="hint"><strong>API keys are stored in plain text</strong> in
       <code>config/ai_endpoints.json</code>. That file is deliberately kept
       out of <code>settings.json</code> so it never travels with a copied or
       redistributed suite — but treat the folder accordingly, use a key your
       institution permits for this purpose, and remove it when finished.</p>
    <div id="aiTableWrap"></div>
    <div>
      <button id="aiAddBtn">Add endpoint</button>
      <button id="aiTestBtn" class="secondary">Test selected</button>
    </div>
    <label for="aiTestPick">Endpoint to test</label>
    <select id="aiTestPick"></select>
    <div class="status" id="aiStatus" role="status" aria-live="polite"></div>
    <details>
      <summary>How to connect a provider</summary>
      <div class="inner">
        <p><strong>Anthropic Claude</strong> — key from
           <code>console.anthropic.com</code>. Kind <code>anthropic</code>,
           URL <code>https://api.anthropic.com</code>, model = a current
           Claude model name.</p>
        <p><strong>Google Gemini</strong> — key from
           <code>aistudio.google.com/apikey</code>. Kind <code>gemini</code>,
           URL <code>https://generativelanguage.googleapis.com/v1beta</code>,
           and a vision-capable model. Vertex AI (OAuth-only) is not
           supported — use an AI Studio key.</p>
        <p><strong>OpenAI</strong> — key from
           <code>platform.openai.com</code> (an API account is separate from
           a ChatGPT subscription). Kind <code>openai</code>, URL
           <code>https://api.openai.com/v1</code>, vision-capable model.</p>
        <p><strong>Microsoft Copilot / Azure OpenAI</strong> — Copilot itself
           has no file API; use your institution's Azure OpenAI resource.
           Kind <code>openai</code>, URL = the full deployment URL ending in
           <code>/chat/completions?api-version=…</code>, model = the
           deployment name.</p>
      </div>
    </details>
  </section>

  <section class="card" aria-labelledby="setModulesH">
    <h2 id="setModulesH">Manage modules</h2>
    <p>Modules are single <code>.py</code> files in the <code>modules/</code>
       folder. Installing here copies the file in; removing moves it to
       <code>modules/_removed/</code> so nothing is lost. The
       <strong>Order</strong> buttons set how modules appear on the Main Menu —
       the order is saved between sessions, and newly installed modules are
       added to the end without disturbing it.</p>
    <label for="moduleFile">Install a module file</label>
    <input type="file" id="moduleFile" accept=".py">
    <div>
      <button id="refreshModulesBtn2" class="secondary">Refresh list</button>
    </div>
    <div class="status" id="moduleMgmtStatus" role="status" aria-live="polite"></div>
    <div id="moduleTableWrap"></div>
  </section>

  <section class="card">
    <a href="#/menu">← Back to Main Menu</a>
  </section>
</section>

<div class="modalback" id="aiModal">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="aiMTitle">
    <h3 id="aiMTitle">Edit endpoint</h3>
    <label for="aiMName">Name</label>
    <input type="text" id="aiMName" autocomplete="off">
    <label for="aiMKind">Kind</label>
    <select id="aiMKind">
      <option value="anthropic">anthropic</option>
      <option value="gemini">gemini</option>
      <option value="openai">openai (OpenAI-compatible, incl. Azure)</option>
    </select>
    <label for="aiMUrl">URL</label>
    <input type="text" id="aiMUrl" autocomplete="off" spellcheck="false">
    <label for="aiMModel">Model (Azure: deployment name)</label>
    <input type="text" id="aiMModel" autocomplete="off" spellcheck="false">
    <label for="aiMKey">API key</label>
    <input type="text" id="aiMKey" autocomplete="off" spellcheck="false">
    <p id="aiMCacheRow"><label><input type="checkbox" id="aiMCache"> Cache
       the prompt prefix (needs a prompt over the model's minimum — 512
       tokens on Opus, 1,024 on Sonnet, 4,096 on Haiku — and reorders the
       request to put the text before the image)</label></p>
    <p><label><input type="checkbox" id="aiMDefault"> Use as the default
       endpoint</label></p>
    <div class="status" id="aiMErr" role="status" aria-live="polite"></div>
    <div class="foot">
      <button id="aiMDelete" class="danger">Delete</button>
      <button id="aiMCancel" class="secondary">Cancel</button>
      <button id="aiMSave">Save</button>
    </div>
  </div>
</div>

</main>
<footer>
  <p>DC Admin Suite · shell v1.12.1 · runs locally on your computer; nothing is
     sent anywhere except to your Digital Commons instance through your own
     Chrome session.</p>
</footer>

<script>
"use strict";
/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON response body */ }
  if (data === null) throw new Error("HTTP " + res.status);
  return data;
}

function setStatus(el, kind, html) {
  el.className = "status " + kind;
  el.innerHTML = html;
}

async function busy(btn, fn) {
  btn.setAttribute("aria-busy", "true"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.removeAttribute("aria-busy"); btn.disabled = false; }
}

/* WCAG relative luminance → pick black/white text on the primary color */
function onColor(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "#ffffff";
  const [r, g, b] = [0, 2, 4].map(i =>
    parseInt(m[1].slice(i, i + 2), 16) / 255).map(v =>
    v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  const L = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return (1.05 / (L + 0.05)) >= 4.5 ? "#ffffff" : "#111111";
}

/* ------------------------------------------------------------------ */
/* state & branding                                                    */
/* ------------------------------------------------------------------ */
let STATE = null;

function applyBranding() {
  const b = STATE.settings.branding, c = b.colors, root = document.documentElement;
  root.style.setProperty("--primary", c.primary);
  root.style.setProperty("--accent", c.accent);
  root.style.setProperty("--neutral", c.neutral);
  root.style.setProperty("--on-primary", onColor(c.primary));
  $("suiteName").textContent = b.suite_name || "DC Admin Suite";
  $("instName").textContent = b.institution || "";
  document.title = (b.suite_name || "DC Admin Suite") +
    (b.institution ? " — " + b.institution : "");
  const logo = $("brandLogo");
  if (STATE.has_logo) {
    logo.src = "/branding/logo?t=" + Date.now();
    logo.alt = (b.institution || "Institution") + " logo";
    logo.hidden = false;
  } else { logo.hidden = true; }
}

function renderInstructions() {
  const ins = STATE.instructions;
  const block = (o) => `
    <ol class="steps">${o.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
    <pre class="cmd" tabindex="0">${esc(o.command)}</pre>
    <button class="secondary copy-btn" data-cmd="${esc(o.command)}">Copy command</button>`;
  $("winInstructions").innerHTML = block(ins.windows);
  $("macInstructions").innerHTML = block(ins.mac);
  $("winInstructions2").innerHTML = block(ins.windows);
  $("macInstructions2").innerHTML = block(ins.mac);
  // The note is trusted, server-defined HTML (styled <strong>/<em> text
  // from chrome_bridge.chrome_instructions) — safe to set as innerHTML.
  $("chromeNote").innerHTML = ins.note;
  $("chromeNote2").innerHTML = ins.note;
  document.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(btn.dataset.cmd);
            btn.textContent = "Copied!"; }
      catch { btn.textContent = "Select the command above and copy it"; }
      setTimeout(() => btn.textContent = "Copy command", 2500);
    });
  });
}

/* ------------------------------------------------------------------ */
/* router                                                              */
/* ------------------------------------------------------------------ */
const VIEWS = { "#/splash":"view-splash", "#/menu":"view-menu",
                "#/settings":"view-settings" };

function route() {
  let hash = location.hash;
  if (!VIEWS[hash])
    hash = STATE.session.splash_complete ? "#/menu" : "#/splash";
  // Leaving the splash for Settings overrides splash completion.
  if (hash !== "#/splash" && !STATE.session.splash_complete) {
    api("/api/splash/complete", {});
    STATE.session.splash_complete = true;
  }
  for (const [h, id] of Object.entries(VIEWS)) $(id).hidden = (h !== hash);
  if (hash === "#/menu") renderModuleCards();
  if (hash === "#/settings") {
    fillSettingsForm(); renderModuleTable(); loadAiEndpoints();
  }
  if (hash === "#/splash") updateSplash();
  const heading = { "#/splash":"splashTitle", "#/menu":"menuTitle",
                    "#/settings":"settingsTitle" }[hash];
  $(heading).focus({ preventScroll: false });
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ */
/* splash                                                              */
/* ------------------------------------------------------------------ */
const splashDone = { url:false, deps:false, chrome:false };

function updateSplash() {
  $("splashBaseUrl").value = STATE.settings.base_url;
  $("splashCrossrefPrefix").value = STATE.settings.crossref_prefix || "";
  splashDone.url = !!STATE.settings.base_url;
  splashDone.deps = STATE.session.deps_ok;
  splashDone.chrome = STATE.session.chrome_ok;
  paintSteps();
}

function paintSteps() {
  const steps = [splashDone.url, splashDone.deps, splashDone.chrome];
  steps.forEach((done, i) => {
    const el = $("step" + (i + 1));
    el.classList.toggle("done", done);
    el.classList.toggle("active", !done && steps.slice(0, i).every(Boolean));
  });
  const all = steps.every(Boolean);
  $("step4").classList.toggle("done", all);
  /* Each line in its own tone (DC-PALETTE): a step that is done is a
     success and reads green; one still to do is an instruction and reads
     gray. Before v1.12 the whole box was one tone, so "✓ Connected to
     Chrome" sat in gray beside two steps not yet taken. */
  const parts = [
    [steps[0], "✓ Base URL saved", "• Base URL not saved yet"],
    [steps[1], "✓ Dependencies OK", "• Dependencies not verified yet"],
    [steps[2], "✓ Connected to Chrome", "• Chrome not connected yet"],
  ].map(([done, yes, no]) =>
    '<span class="' + (done ? "dc-ok" : "dc-info") + '">' +
    esc(done ? yes : no) + "</span>");
  setStatus($("finishSummary"), all ? "good" : "info",
    (all ? "<strong>Setup complete.</strong> " : "") + parts.join("<br>"));
}

async function saveBaseUrl(inputEl, statusEl, btn) {
  const url = inputEl.value.trim().replace(/\/+$/, "");
  if (!/^https?:\/\/.+\..+/.test(url)) {
    setStatus(statusEl, "bad",
      "Please enter a full URL starting with https:// — for example " +
      "https://digitalcommons.example.edu");
    return;
  }
  await busy(btn, async () => {
    const r = await api("/api/settings", { base_url: url });
    STATE.settings = r.settings;
    splashDone.url = true;
    setStatus(statusEl, "good", "Saved. This session will work on <strong>" +
      esc(url) + "</strong>.");
    paintSteps();
  });
}

async function saveCrossrefPrefix(inputEl, statusEl, btn) {
  const norm = inputEl.value.trim().replace(/^doi:/i, "").trim().replace(/\/+$/, "");
  if (!/^10\.\d{3,9}$/.test(norm)) {
    setStatus(statusEl, "bad",
      "Enter a Crossref DOI prefix like 10.12345 (10. followed by your " +
      "account's digits).");
    return;
  }
  await busy(btn, async () => {
    const r = await api("/api/settings", { crossref_prefix: norm });
    if (r.ok === false) {
      setStatus(statusEl, "bad", esc(r.error || "Could not save."));
      return;
    }
    STATE.settings = r.settings;
    // Keep both copies of the field in sync (splash + settings).
    $("splashCrossrefPrefix").value = r.settings.crossref_prefix;
    $("setCrossrefPrefix").value = r.settings.crossref_prefix;
    setStatus(statusEl, "good", "Saved. DOI tools will use <strong>" +
      esc(r.settings.crossref_prefix) + "</strong> the next time they launch.");
  });
}

async function saveCrossrefIdentity(statusEl, btn) {
  const block = {
    depositor_name: $("setDepositorName").value.trim(),
    depositor_email: $("setDepositorEmail").value.trim(),
    registrant: $("setRegistrant").value.trim(),
    publisher: $("setPublisher").value.trim(),
  };
  if (block.depositor_email &&
      !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(block.depositor_email)) {
    setStatus(statusEl, "bad", "Enter the depositor email as a plain " +
      "address — for example repository@example.edu.");
    return;
  }
  await busy(btn, async () => {
    const r = await api("/api/settings", { crossref: block });
    if (r.ok === false) {
      setStatus(statusEl, "bad", esc(r.error || "Could not save."));
      return;
    }
    STATE.settings = r.settings;
    const saved = r.settings.crossref || {};
    const blank = ["depositor_name", "depositor_email", "registrant",
                   "publisher"].filter(k => !saved[k]);
    if (blank.length) {
      setStatus(statusEl, "warn", "Saved, but still blank: <strong>" +
        esc(blank.join(", ")) + "</strong>. Crossref deposits stay blocked " +
        "until the depositor name, email and registrant are filled in.");
    } else {
      setStatus(statusEl, "good", "Saved. Deposits will be credited to " +
        "<strong>" + esc(saved.registrant) + "</strong> the next time a DOI " +
        "module launches.");
    }
  });
}

function renderDeps(r, statusEl) {
  if (r.module_count === 0) {
    setStatus(statusEl, "good",
      "No modules are installed yet, so there are no module dependencies " +
      "to check. (Python " + esc(r.python) + " is ready.)");
    return;
  }
  let html = r.all_ok
    ? "<strong>All dependencies are installed.</strong>"
    : "<strong>Missing package" + (r.missing.length > 1 ? "s" : "") +
      ": " + esc(r.missing.join(", ")) + "</strong>";
  html += `<table><caption class="visually-hidden">Dependency check results</caption>
    <tr><th scope="col">Package</th><th scope="col">Status</th>
    <th scope="col">Needed by</th></tr>` +
    r.deps.map(d => `<tr><td><code>${esc(d.package)}</code></td>
      <td><span class="pill ${d.ok ? "ok" : "no"}">${d.ok ? "installed" : "missing"}</span></td>
      <td>${esc(d.needed_by.join(", "))}</td></tr>`).join("") + "</table>";
  /* chromedriver is an external binary, and ABSENT is the healthy state —
     Selenium fetches a matching one itself. It is shown as its own line
     rather than a dependency row so it can never read as "missing". */
  if (r.chromedriver) {
    const cd = r.chromedriver;
    html += "<p>" + (cd.present
      ? `<span class="pill ${r.chromedriver_advice.length ? "no" : "ok"}">chromedriver on PATH</span> ` +
        esc(cd.version || "version unknown") + " at <code>" + esc(cd.path) + "</code>"
      : `<span class="pill ok">no chromedriver on PATH</span> Selenium fetches a matching one itself, which is what you want.`) +
      "</p>";
  }
  if (r.advice.length)
    html += "<ul>" + r.advice.map(a =>
      a.includes("pip install")
        ? `<li><pre class="cmd" tabindex="0">${esc(a)}</pre></li>`
        : `<li>${esc(a)}</li>`).join("") + "</ul>";
  setStatus(statusEl, r.all_ok ? "good" : "bad", html);
}

async function checkDeps(btn, statusEl) {
  await busy(btn, async () => {
    const r = await api("/api/deps/check", {});
    STATE.session.deps_ok = r.all_ok;
    splashDone.deps = r.all_ok;
    renderDeps(r, statusEl);
    paintSteps();
  });
}

/* This used to say "you're set" as soon as a Digital Commons tab was open,
   having never once tried to open a session. It now reports the attach,
   which is the thing every module depends on, and says "you're set" only
   when that succeeded. A status line is part of the product; one that
   asserts something it has not checked is a defect like any other. */
function renderChrome(r, statusEl) {
  if (r.ok) {
    let html = "<strong>Connected to " + esc(r.browser) + "</strong> (" +
      r.tabs + " tab" + (r.tabs === 1 ? "" : "s") + " open). ";
    if (r.attach_ok) {
      html += "Opened and released a test session" +
        (r.driver_version ? " using chromedriver " + esc(r.driver_version) : "") +
        ", so modules can attach. ";
      html += r.dc_tab_found
        ? "A tab is open on your Digital Commons instance — you're set."
        : "";
    } else {
      html += "<strong>Modules cannot attach to it yet.</strong>";
    }
    /* v1.12: a working connection is a success and reads green, even when
       there is advice to follow; the advice itself is instruction, so it
       is gray inside the box (DC-PALETTE). It used to turn the whole box
       gray, which is how "Connected to Chrome" came to look like a
       pending step. */
    if (r.advice.length)
      html += "<ul>" + r.advice.map(a =>
        `<li class="dc-info">${esc(a)}</li>`).join("") + "</ul>";
    setStatus(statusEl, r.attach_ok ? "good" : "bad", html);
  } else {
    setStatus(statusEl, "bad",
      "<strong>Could not connect to Chrome.</strong> Try the following, " +
      "then click the button again:<ul>" +
      r.advice.map(a => `<li>${esc(a)}</li>`).join("") + "</ul>");
  }
}

async function checkChrome(btn, statusEl) {
  await busy(btn, async () => {
    const r = await api("/api/chrome/check", {});
    STATE.session.chrome_ok = r.ok;
    STATE.session.dc_tab_found = r.dc_tab_found;
    STATE.session.attach_ok = r.attach_ok;
    /* The step is only done when a session actually opened. Marking it done
       on r.ok alone is how a stale chromedriver got a green splash. */
    splashDone.chrome = r.ok && r.attach_ok;
    renderChrome(r, statusEl);
    paintSteps();
  });
}

/* ------------------------------------------------------------------ */
/* main menu                                                           */
/* ------------------------------------------------------------------ */
function renderModuleCards() {
  const list = $("moduleList"), mods = STATE.modules;
  $("noModules").hidden = mods.length > 0;
  list.innerHTML = mods.map(m => m.error ? `
    <li>
      <h3>${esc(m.file)}</h3>
      <p class="desc"><span class="pill no">can't load</span> ${esc(m.error)}</p>
    </li>` : `
    <li>
      <h3>${esc(m.name)}</h3>
      <p class="desc">${esc(m.description)}</p>
      <p class="meta">${esc(m.file)}${m.version ? " · v" + esc(m.version) : ""}</p>
      <button data-launch="${esc(m.id)}">Open ${esc(m.name)}</button>
    </li>`).join("");
  list.querySelectorAll("button[data-launch]").forEach(btn => {
    btn.addEventListener("click", () => launchModule(btn));
  });
}

/* ------------------------------------------------------------------ */
/* module ordering (Settings › Manage Modules table only)              */
/* ------------------------------------------------------------------ */
/* Reorder controls (Move up / Move down) for one table row. */
function orderControls(m, i, total) {
  const label = m.error ? m.file : m.name;
  return `<button class="secondary ord" data-move="up" data-mid="${esc(m.id)}"
      ${i === 0 ? "disabled" : ""}
      aria-label="Move ${esc(label)} up">&#9650;<span class="visually-hidden"> move up</span></button>
    <button class="secondary ord" data-move="down" data-mid="${esc(m.id)}"
      ${i === total - 1 ? "disabled" : ""}
      aria-label="Move ${esc(label)} down">&#9660;<span class="visually-hidden"> move down</span></button>`;
}

function bindMoveButtons(scope) {
  scope.querySelectorAll("button[data-move]").forEach(btn => {
    btn.addEventListener("click", () =>
      moveModule(btn.dataset.mid, btn.dataset.move === "up" ? -1 : 1));
  });
}

/* Swap a module with its neighbor, persist the new order, re-render the
   affected views, and return focus to the moved control. */
async function moveModule(mid, delta) {
  const ids = STATE.modules.map(m => m.id);
  const i = ids.indexOf(mid), j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  const r = await api("/api/modules/order", { order: ids });
  if (r.ok) {
    STATE.modules = r.modules;
    renderModuleTable();
    renderModuleCards();   // Main Menu display order follows the saved order.
    refocusMove(mid, delta);
  } else {
    setStatus($("moduleMgmtStatus"), "bad",
      esc(r.error || "Could not save the new order."));
  }
}

function refocusMove(mid, delta) {
  const scope = $("moduleTableWrap");
  if (!scope) return;
  const dir = delta < 0 ? "up" : "down";
  const btns = Array.from(scope.querySelectorAll("button[data-move]"))
    .filter(b => b.dataset.mid === mid);
  const btn = btns.find(b => b.dataset.move === dir && !b.disabled)
           || btns.find(b => !b.disabled);
  if (btn) btn.focus();
}

async function launchModule(btn) {
  await busy(btn, async () => {
    const r = await api("/api/modules/launch", { id: btn.dataset.launch });
    if (r.ok) {
      // One tab per module, by name: opening a module whose tab is still
      // open goes back to that tab, and one whose tab was closed opens a
      // new one on the SAME running copy (since 2026-09-30).
      const tab = "dcAdminSuiteModule_" + btn.dataset.launch;
      setStatus($("menuStatus"), "good",
        "<strong>" + esc(r.name) + "</strong> " +
        (r.reused ? "is already running at " : "started at ") +
        `<a href="${esc(r.url)}" target="${esc(tab)}" rel="opener">${esc(r.url)}</a>` +
        (r.reused ? " (returning to it)." : " (opening in a new tab)."));
      // Keep the opener relationship (no "noopener"): the module tab must
      // stay in this tab's browsing-context group so its header link
      // (target="dcAdminSuiteHub") can find and reuse this hub tab.
      window.open(r.url, tab);
    } else {
      setStatus($("menuStatus"), "bad", esc(r.error));
    }
  });
}

async function refreshModules(btn, statusEl) {
  await busy(btn, async () => {
    const r = await api("/api/modules");
    STATE.modules = r.modules;
    renderModuleCards(); renderModuleTable();
    setStatus(statusEl, "good", "Module list refreshed — " +
      r.modules.length + " module" + (r.modules.length === 1 ? "" : "s") +
      " found.");
  });
}

/* ------------------------------------------------------------------ */
/* settings                                                            */
/* ------------------------------------------------------------------ */
function fillSettingsForm() {
  const s = STATE.settings, b = s.branding, c = b.colors;
  $("setBaseUrl").value = s.base_url;
  $("setCrossrefPrefix").value = s.crossref_prefix || "";
  const cr = s.crossref || {};
  $("setDepositorName").value = cr.depositor_name || "";
  $("setDepositorEmail").value = cr.depositor_email || "";
  $("setRegistrant").value = cr.registrant || "";
  $("setPublisher").value = cr.publisher || "";
  $("setChromeHost").value = s.chrome.host;
  $("setChromePort").value = s.chrome.port;
  $("setSuiteName").value = b.suite_name;
  $("setInstName").value = b.institution;
  for (const [key, id] of [["primary","colPrimary"],["accent","colAccent"],
                           ["neutral","colNeutral"]]) {
    $(id).value = c[key]; $(id + "Hex").value = c[key];
  }
}

/* ------------------------------------------------------------------ */
/* AI endpoints (Settings)                                             */
/*                                                                     */
/* Promoted from the two AI modules in v1.3 so a key is added or       */
/* rotated in one place. Modules read config/ai_endpoints.json per     */
/* request, so a change here reaches a running module immediately.     */
/* ------------------------------------------------------------------ */
let AI_CFG = { endpoints: [], "default": "" };
let aiEditIndex = -1, aiLastFocus = null;

async function loadAiEndpoints() {
  const r = await api("/api/ai");
  if (r && r.config) { AI_CFG = r.config; renderAiTable(); }
}

function renderAiTable() {
  const wrap = $("aiTableWrap"), eps = AI_CFG.endpoints || [];
  wrap.innerHTML = eps.length === 0
    ? "<p>No AI endpoints yet. Modules that use AI stay disabled until one " +
      "is added.</p>"
    : `<table><caption>AI endpoints</caption>
       <tr><th scope="col">Name</th><th scope="col">Kind</th>
       <th scope="col">Model</th><th scope="col">Key</th>
       <th scope="col">Options</th></tr>` +
      eps.map((ep, i) => `<tr>
        <td><button class="rowbtn" data-edit="${i}">${esc(ep.name)}</button>${
          AI_CFG["default"] === ep.name
            ? ' <span class="pill ok">default</span>' : ""}</td>
        <td>${esc(ep.kind)}</td>
        <td><code>${esc(ep.model)}</code></td>
        <td>${ep.api_key
              ? "•••" + esc(ep.api_key.slice(-4))
              : '<span class="pill no">none</span>'}</td>
        <td>${ep.kind !== "anthropic"
              ? '<span class="pill na" title="Prompt caching is an Anthropic '
                + 'feature">n/a</span>'
              : ep.prompt_cache
                ? '<span class="pill ok">prompt cache</span>'
                : '<span class="pill no">none</span>'}</td>
        </tr>`).join("") +
      "</table>";
  wrap.querySelectorAll("button[data-edit]").forEach(b =>
    b.addEventListener("click", () => openAiModal(Number(b.dataset.edit))));
  const pick = $("aiTestPick");
  pick.innerHTML = eps.map(ep =>
    `<option value="${esc(ep.name)}">${esc(ep.name)}</option>`).join("");
  if (AI_CFG["default"]) pick.value = AI_CFG["default"];
  $("aiTestBtn").disabled = eps.length === 0;
}

function openAiModal(i) {
  aiEditIndex = i;
  aiLastFocus = document.activeElement;
  const ep = i >= 0 ? AI_CFG.endpoints[i]
    : { name:"", kind:"anthropic", url:"https://api.anthropic.com",
        model:"", api_key:"", prompt_cache:false };
  $("aiMTitle").textContent = i >= 0 ? "Edit endpoint" : "Add endpoint";
  $("aiMName").value = ep.name; $("aiMKind").value = ep.kind;
  $("aiMUrl").value = ep.url; $("aiMModel").value = ep.model;
  $("aiMKey").value = ep.api_key;
  $("aiMCache").checked = !!ep.prompt_cache;
  syncAiCacheRow();
  $("aiMDefault").checked = i >= 0 && AI_CFG["default"] === ep.name;
  $("aiMDelete").style.display = i >= 0 ? "" : "none";
  setStatus($("aiMErr"), "info", "");
  $("aiModal").classList.add("open");
  $("aiMName").focus();
}

function closeAiModal() {
  $("aiModal").classList.remove("open");
  if (aiLastFocus) aiLastFocus.focus();
}

/* Save a whole candidate registry; the local copy is only replaced from the
   server's response, so a rejected save leaves the table untouched. */
async function saveAiCfg(cand) {
  const r = await api("/api/ai", cand);
  if (!r.ok) { setStatus($("aiMErr"), "bad", esc(r.error)); return false; }
  AI_CFG = r.config; renderAiTable();
  setStatus($("aiStatus"), "good", "AI endpoints saved.");
  return true;
}

// Prompt caching is an Anthropic feature: `cache_control` exists only in the
// anthropic request builder, and both AI modules read prompt_cache only on
// that path. Offering the control for a gemini or openai endpoint invites a
// setting that silently does nothing. Hiding it is half the fix — the box is
// also cleared, so a value set while the kind was anthropic cannot survive a
// switch and be saved from a control the user can no longer see.
function syncAiCacheRow() {
  const anthropic = $("aiMKind").value === "anthropic";
  $("aiMCacheRow").hidden = !anthropic;
  if (!anthropic) $("aiMCache").checked = false;
}

function wireAiEndpoints() {
  $("aiAddBtn").addEventListener("click", () => openAiModal(-1));
  $("aiMCancel").addEventListener("click", closeAiModal);
  $("aiMKind").addEventListener("change", syncAiCacheRow);

  $("aiMSave").addEventListener("click", async () => {
    const ep = { name: $("aiMName").value.trim(), kind: $("aiMKind").value,
                 url: $("aiMUrl").value.trim(),
                 model: $("aiMModel").value.trim(),
                 api_key: $("aiMKey").value.trim(),
                 prompt_cache: $("aiMCache").checked };
    const cand = { endpoints: AI_CFG.endpoints.slice(),
                   "default": AI_CFG["default"] };
    if (aiEditIndex >= 0) cand.endpoints[aiEditIndex] = ep;
    else cand.endpoints.push(ep);
    if ($("aiMDefault").checked) cand["default"] = ep.name;
    else if (cand["default"] === ep.name) cand["default"] = "";
    if (await saveAiCfg(cand)) closeAiModal();
  });

  $("aiMDelete").addEventListener("click", async () => {
    if (aiEditIndex < 0) return;
    const name = AI_CFG.endpoints[aiEditIndex].name;
    if (!confirm('Delete endpoint "' + name +
        '"? Modules using it will fall back to no endpoint.')) return;
    const cand = { endpoints: AI_CFG.endpoints.slice(),
                   "default": AI_CFG["default"] };
    cand.endpoints.splice(aiEditIndex, 1);
    if (cand["default"] === name) cand["default"] = "";
    if (await saveAiCfg(cand)) closeAiModal();
  });

  $("aiTestBtn").addEventListener("click", () => busy($("aiTestBtn"),
    async () => {
      const name = $("aiTestPick").value;
      if (!name) return;
      setStatus($("aiStatus"), "info", "Testing " + esc(name) + "…");
      const r = await api("/api/ai/test", { name: name });
      if (r.ok)
        setStatus($("aiStatus"), "good",
          "Test OK — <code>" + esc(name) + "</code> replied: " +
          esc(r.reply));
      else
        setStatus($("aiStatus"), "bad", esc(r.error));
    }));

  /* Modal accessibility: Escape closes, backdrop click closes, Tab is
     trapped inside, and focus returns to whatever opened it. */
  const back = $("aiModal");
  back.addEventListener("mousedown", ev => {
    if (ev.target === back) closeAiModal();
  });
  back.querySelector(".modal").addEventListener("keydown", ev => {
    if (ev.key === "Escape") { closeAiModal(); return; }
    if (ev.key !== "Tab") return;
    const items = Array.prototype.filter.call(
      back.querySelectorAll("input,select,button"),
      el => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault(); last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault(); first.focus();
    }
  });
}

function renderModuleTable() {
  const mods = STATE.modules, total = mods.length, wrap = $("moduleTableWrap");
  wrap.innerHTML = mods.length === 0
    ? "<p>No modules installed.</p>"
    : `<table><caption>Installed modules (in Main Menu order)</caption>
       <tr><th scope="col" class="col-module">Module</th>
       <th scope="col" class="col-file">File</th>
       <th scope="col">Requires</th><th scope="col">Order</th>
       <th scope="col">Action</th></tr>` +
      mods.map((m, i) => `<tr>
        <td class="col-module">${m.error ? '<span class="pill no">error</span> ' + esc(m.error)
                      : esc(m.name)}</td>
        <td class="col-file"><code>${esc(m.file)}</code></td>
        <td>${esc((m.requires || []).join(", ")) || "—"}</td>
        <td class="ordcell">${orderControls(m, i, total)}</td>
        <td><button class="danger" data-remove="${esc(m.id)}"
             data-name="${esc(m.name)}">Remove</button></td></tr>`).join("") +
      "</table>";
  bindMoveButtons(wrap);
  wrap.querySelectorAll("button[data-remove]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm('Remove module "' + btn.dataset.name +
          '"? The file moves to modules/_removed/ and can be restored.'))
        return;
      const r = await api("/api/modules/remove", { id: btn.dataset.remove });
      if (r.ok) {
        setStatus($("moduleMgmtStatus"), "good",
          "Removed <code>" + esc(r.file) + "</code> (kept in <code>" +
          esc(r.moved_to) + "</code>).");
        const mr = await api("/api/modules");
        STATE.modules = mr.modules;
        renderModuleTable(); renderModuleCards();
      } else setStatus($("moduleMgmtStatus"), "bad", esc(r.error));
    });
  });
}

function readFileB64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",", 2)[1] || "");
    fr.onerror = reject;
    fr.readAsDataURL(file);
  });
}

/* ------------------------------------------------------------------ */
/* wire-up                                                             */
/* ------------------------------------------------------------------ */
function wire() {
  wireAiEndpoints();
  $("saveBaseUrlBtn").addEventListener("click", () =>
    saveBaseUrl($("splashBaseUrl"), $("baseUrlStatus"), $("saveBaseUrlBtn")));
  $("setSaveBaseUrlBtn").addEventListener("click", () =>
    saveBaseUrl($("setBaseUrl"), $("setBaseUrlStatus"), $("setSaveBaseUrlBtn")));

  $("saveCrossrefPrefixBtn").addEventListener("click", () =>
    saveCrossrefPrefix($("splashCrossrefPrefix"), $("crossrefPrefixStatus"),
      $("saveCrossrefPrefixBtn")));
  $("setSaveCrossrefPrefixBtn").addEventListener("click", () =>
    saveCrossrefPrefix($("setCrossrefPrefix"), $("setCrossrefPrefixStatus"),
      $("setSaveCrossrefPrefixBtn")));
  $("setSaveCrossrefIdBtn").addEventListener("click", () =>
    saveCrossrefIdentity($("setCrossrefIdStatus"), $("setSaveCrossrefIdBtn")));

  $("depsCheckBtn1").addEventListener("click", () =>
    checkDeps($("depsCheckBtn1"), $("depsStatus1")));
  $("depsCheckBtn2").addEventListener("click", () =>
    checkDeps($("depsCheckBtn2"), $("depsStatus2")));

  $("chromeCheckBtn1").addEventListener("click", () =>
    checkChrome($("chromeCheckBtn1"), $("chromeStatus1")));
  $("chromeCheckBtn2").addEventListener("click", () =>
    checkChrome($("chromeCheckBtn2"), $("chromeStatus2")));

  $("finishBtn").addEventListener("click", async () => {
    await api("/api/splash/complete", {});
    STATE.session.splash_complete = true;
    location.hash = "#/menu";
  });

  $("refreshModulesBtn").addEventListener("click", () =>
    refreshModules($("refreshModulesBtn"), $("menuStatus")));
  $("refreshModulesBtn2").addEventListener("click", () =>
    refreshModules($("refreshModulesBtn2"), $("moduleMgmtStatus")));

  $("setSaveChromeBtn").addEventListener("click", () =>
    busy($("setSaveChromeBtn"), async () => {
      const r = await api("/api/settings", { chrome: {
        host: $("setChromeHost").value.trim() || "127.0.0.1",
        port: parseInt($("setChromePort").value, 10) || 9222 } });
      STATE.settings = r.settings;
      setStatus($("chromeStatus2"), "good", "Connection settings saved.");
      renderInstructions();
    }));

  // color pickers ↔ hex fields stay in sync
  for (const id of ["colPrimary", "colAccent", "colNeutral"]) {
    $(id).addEventListener("input", () => { $(id + "Hex").value = $(id).value; });
    $(id + "Hex").addEventListener("change", () => {
      const v = $(id + "Hex").value.trim();
      if (/^#[0-9a-f]{6}$/i.test(v)) $(id).value = v;
    });
  }

  $("saveBrandingBtn").addEventListener("click", () =>
    busy($("saveBrandingBtn"), async () => {
      const r = await api("/api/settings", { branding: {
        suite_name: $("setSuiteName").value.trim() || "DC Admin Suite",
        institution: $("setInstName").value.trim(),
        colors: { primary: $("colPrimaryHex").value.trim(),
                  accent: $("colAccentHex").value.trim(),
                  neutral: $("colNeutralHex").value.trim() } } });
      STATE.settings = r.settings;
      applyBranding();
      setStatus($("brandingStatus"), "good",
        "Look &amp; feel saved and applied.");
    }));

  $("resetBrandingBtn").addEventListener("click", () =>
    busy($("resetBrandingBtn"), async () => {
      // Reset to whatever THIS install's profile says the defaults are
      // (config/profile.json, else the neutral shipped example) — never to
      // an institution's values hardcoded here. See profiles/README.md.
      const d = (STATE.defaults && STATE.defaults.branding) || {};
      const r = await api("/api/settings", { branding: {
        suite_name: d.suite_name || "DC Admin Suite",
        institution: d.institution || "",
        colors: Object.assign({}, d.colors) } });
      STATE.settings = r.settings;
      applyBranding(); fillSettingsForm();
      setStatus($("brandingStatus"), "good",
        r.settings.branding.institution
          ? "Reset to the default branding for " +
            esc(r.settings.branding.institution) + "."
          : "Reset to default branding.");
    }));

  $("logoFile").addEventListener("change", async () => {
    const file = $("logoFile").files[0];
    if (!file) return;
    const r = await api("/api/branding/logo",
      { filename: file.name, data: await readFileB64(file) });
    if (r.ok) {
      STATE.has_logo = true; applyBranding();
      setStatus($("brandingStatus"), "good", "Logo uploaded and applied.");
    } else setStatus($("brandingStatus"), "bad", esc(r.error));
    $("logoFile").value = "";
  });

  $("logoClearBtn").addEventListener("click", () =>
    busy($("logoClearBtn"), async () => {
      await api("/api/branding/logo/clear", {});
      STATE.has_logo = false;
      STATE.settings.branding.logo = "";
      applyBranding();
      setStatus($("brandingStatus"), "good", "Logo removed.");
    }));

  $("moduleFile").addEventListener("change", async () => {
    const file = $("moduleFile").files[0];
    if (!file) return;
    const r = await api("/api/modules/install",
      { filename: file.name, data: await readFileB64(file) });
    if (r.ok) {
      setStatus($("moduleMgmtStatus"), "good",
        "Installed <strong>" + esc(r.name) + "</strong> (<code>" +
        esc(r.file) + "</code>)" + (r.replaced ? " — replaced existing file." : "."));
      const mr = await api("/api/modules");
      STATE.modules = mr.modules;
      renderModuleTable(); renderModuleCards();
    } else setStatus($("moduleMgmtStatus"), "bad", esc(r.error));
    $("moduleFile").value = "";
  });
}

/* ------------------------------------------------------------------ */
/* boot                                                                */
/* ------------------------------------------------------------------ */
(async function boot() {
  // Name this tab so module "back to suite" links (target="dcAdminSuiteHub")
  // reuse/focus it instead of opening duplicate Main Menu tabs.
  window.name = "dcAdminSuiteHub";
  STATE = await api("/api/state");
  applyBranding();
  renderInstructions();
  wire();
  route();
})();
</script>
</body>
</html>
"""
