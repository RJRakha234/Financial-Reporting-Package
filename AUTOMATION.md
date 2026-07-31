# Automating the SAP report download — start here

This guide takes you from nothing to a working, automated download of a BEx Web
report (Excel) out of the SAP NetWeaver portal, and — optionally — having it fire
by itself when a mail arrives.

Read it top to bottom the first time. Each stage is testable on its own, so when
something breaks you know exactly which stage to fix.

---

## The picture

The end state is a chain of three independent pieces:

```
   (optional)              (the core)                 (optional)
  mail arrives   ──▶   download the report   ──▶   check the numbers
 outlook_watch.py         sap_export.py               fincheck/
```

Only the middle piece is essential. **Build and prove it first.** The other two
are conveniences that plug in once the core works.

### Why the download needs a script at all

The report is a BEx Web report displayed inside an *iView* in the portal. Three
things defeat naive automation, and the script handles each:

1. **The report is inside nested iframes.** The portal wraps it in
   `contentAreaFrame` → `isolatedWorkArea`. Selenium looks only at the top
   document by default, so the Export button genuinely does not exist as far as
   it is concerned until you switch into those frames. This is the single most
   common reason people conclude "Selenium can't see the button".
2. **Element IDs are generated per session**, so any locator based on them rots
   within days. The script matches on stable things instead: visible text and
   `title` attributes.
3. **The report renders asynchronously.** The toolbar only exists after the data
   comes back, so everything needs explicit waits, not fixed sleeps.

---

## Stage 0 — What you need before starting

- A Windows machine on the corporate network (VPN on) that can open the portal in
  a browser.
- Python 3.9 or newer. Check with `python --version`.
- Microsoft Edge (already on the machine) or Chrome.
- This repository cloned locally.

```bat
pip install selenium
```

Selenium 4.6+ fetches the matching browser driver by itself — no manual
chromedriver/msedgedriver download needed.

---

## Stage 1 — Find your report's direct URL (one time, 5 minutes)

The script loads the report directly rather than clicking through the portal
menus. To get that URL:

1. Open the report in the portal the way you normally do, until it is on screen.
2. Press **F12** to open DevTools, and click the **Elements** tab.
3. Press **Ctrl+F** (the small search box at the bottom of the Elements panel)
   and search for `isolatedWorkArea`.
4. You will land on a tag like `<iframe src="/irj/servlet/prt/portal/prtroot/pcd!3a..." id="isolatedWorkArea">`.
5. Right-click that tag → **Copy** → **Copy outerHTML**, and paste it into
   Notepad so you can read the whole `src` value.

   *Console alternative (faster):* on the **Console** tab, run
   `copy(document.getElementById('isolatedWorkArea').src)` — the full URL goes
   straight to your clipboard.

6. From that `src`, keep the path and the `ExecuteLocally=true` parameter.
   **Drop `windowId=...`, `NavMode=...` and `PrevNavTarget=...`** — those are
   one-off session breadcrumbs and will be stale next time.

You end up with two values:

| Setting | Example |
|---|---|
| `SAP_PORTAL_BASE` | `https://portal.example.com` |
| `SAP_REPORT_PATH` | `/irj/servlet/prt/portal/prtroot/pcd!3aportal_content!2f...!2fMy_Report?ExecuteLocally=true&sapDocumentRenderingMode=Edge` |

**Sanity check before writing any code:** paste `SAP_PORTAL_BASE` +
`SAP_REPORT_PATH` into a fresh browser tab. If the report (or its Variable Entry
screen) appears, the rest will work. If it does not, nothing downstream can —
sort this out first.

---

## Stage 2 — Configure and run the download

Copy `run_export.example.bat` to `run_export.bat`, open it in Notepad, and fill
in your two values (plus credentials only if the portal shows a login form —
under SSO you leave them blank).

`run_export.bat` is listed in `.gitignore`, so your settings never get committed.

Then just run it:

```bat
run_export.bat
```

A browser window opens and drives itself. **Do not click in it while it works.**
A successful run prints:

```
Opening report: https://portal.example.com/irj/servlet/...
No login form detected (SSO or already signed in).
Variable screen confirmed (OK).
Waiting for the report toolbar ...
Clicked 'Export to Microsoft Excel' — waiting for the file ...
Downloaded: C:\...\sap_downloads\trial_balance.xls
```

Run it **twice**. Once is luck; twice is working.

### What each stage does

| Step | What happens | If it fails |
|---|---|---|
| Open URL | Loads the report iView directly | Check Stage 1's sanity check |
| Login | Fills the portal logon form — skipped under SSO | Set `SAP_USER` / `SAP_PASS` |
| Variable screen | Clicks **OK**, accepting the selection BW remembered | See "Changing parameters" below |
| Export | Clicks **Export to Microsoft Excel** | See troubleshooting |
| Download | Waits for the finished file (ignores partial `.crdownload`) | Allow popups for the portal host |

### Troubleshooting

**"Element not found in any frame"** — the OK or Export button did not match any
known locator. Right-click that button in the browser → Inspect → right-click the
highlighted node → Copy → **Copy outerHTML**, and add its real `id`/`title` to
the `OK_BUTTON_XPATHS` or `EXPORT_BUTTON_XPATHS` list at the top of
`sap_export.py`.

**No file ever arrives** — the export opens a popup; if the browser blocks it,
allow popups for your portal host once, then rerun.

**Times out on a slow report** — raise the budget: `set SAP_TIMEOUT=180`.

**A login form appears unexpectedly** — SSO did not carry over. Set `SAP_USER`
and `SAP_PASS` in `run_export.bat`.

### Changing the report parameters (fiscal period, ledger, …)

Today the script clicks **OK** on the Variable Entry screen, which accepts
whatever selection BW saved from your last manual run. To change the period,
run the report manually once with the values you want — BW remembers them, and
the automation picks them up from then on.

To have the *script* set the period instead, we need the real field names.
Capture them once: open DevTools → **Network** tab, tick **Preserve log**, then
click **OK** on the variable screen, and look at the request that fires — its
**Payload** tab lists the field names. With those, the period can become a
proper script parameter.

---

## Stage 3 (optional) — Fire it automatically when a mail arrives

`outlook_watch.py` checks your Outlook Inbox once a minute for unread mail whose
subject or body contains your trigger words, then runs the export and tags the
mail with the category `SAP-Export-Done` so it is never handled twice. A failed
export leaves the mail untagged, so it retries on the next poll.

```bat
pip install pywin32
```

Add to your `run_watch.bat` (copy the same settings from `run_export.bat`, plus):

```bat
set "WATCH_KEYWORDS=trial balance ready,TB refresh done"
set "WATCH_FROM=@example.com"
python outlook_watch.py
```

Requirements and limits, honestly:

- **Classic Outlook desktop only.** The "new Outlook" is web-based and exposes no
  COM interface. Toggle back to classic while the watcher runs.
- **The machine must be on, logged in, with Outlook running.** This is a local
  bot, not a cloud service. For hands-off starts, add it to Task Scheduler with
  the trigger *At log on*.
- Each run opens a visible browser window for a minute or two.

Test it by sending yourself a mail containing one of your trigger phrases.

---

## Stage 4 (optional) — Check the numbers automatically

`fincheck/` in this repository verifies that totals and subtotals in a financial
statement actually foot. It reads PDFs today, so feeding the exported Excel into
it needs a conversion step — worth adding once the download is reliable.

---

## Approaches that were considered and rejected

Useful to know so nobody re-treads them:

- **Calling the export URL directly with `requests`** (log in via Selenium, hand
  the session cookies to Python, download without a browser). This is the most
  robust design *if* you can capture the export request — but the export happens
  in a popup window that DevTools does not record by default, and the browser
  offered no "Copy download link", which suggests it is a POST. Still worth
  revisiting: in DevTools press **F1** → Preferences → Global → tick **Auto-open
  DevTools for popups**, export once, and copy the request from the popup's own
  DevTools window as cURL. With that, the browser can be dropped entirely.
- **A VBA macro doing the download.** VBA's built-in web automation drives only
  Internet Explorer, which no longer exists. VBA *is* useful as an event-driven
  trigger that shells out to `sap_export.py` — a drop-in replacement for
  `outlook_watch.py` if your macro settings permit it.
- **Screen-coordinate recorders (PyAutoGUI and similar).** They replay mouse
  positions, so any popup, resolution change, or slow render breaks them
  silently.
- **Record-and-replay RPA** (Power Automate Desktop, Playwright `codegen`,
  Selenium IDE). Genuinely useful as a *capture device* — `playwright codegen`
  writes Python for every click, which is the quickest way to learn the real
  selectors for the variable screen. Just do not ship the raw recording: it
  memorises literal steps against per-session IDs and rots quickly.
- **Full RPA platforms** (UiPath, Power Automate, AssistEdge). Right answer when
  this becomes a *team* asset needing unattended runs, credential vaults, audit
  logs and central monitoring. Overkill for one person on one laptop — but this
  working script is the ideal specification to hand an automation team later.

---

## A note on what is safe to share

Screenshots of the portal, element HTML, and parameter *names* are fine to paste
into a ticket or a chat. Never share cookie values, anything called
token/SSO/session, passwords, or a screenshot of the *populated* report — that is
live financial data. Keep this repository private if the report path is in your
configuration files, and remember that an automation logging into a financial
system, even under your own account, is worth a heads-up to your manager once it
becomes part of a routine.
