# Automating the SAP report download — start here

This guide takes you from nothing to an automated Excel download of a report
out of SAP, and — optionally — having it fire by itself when a mail arrives.

There are two very different situations, and the first job is telling them
apart, because the right approach is not the same:

| Your report is… | How to tell | Use |
|---|---|---|
| **BusinessObjects Web Intelligence** | Its properties panel shows a **CUID** | `opendoc_export.py` — a single URL, no browser automation |
| **BEx Web (BW)** | No CUID; a BEx "Variable Entry" screen | `sap_export.py` — Selenium drives the UI |

**Check for the CUID first.** If it is there, you are on the easy path and can
skip most of this document.

---

## The picture

```
   (optional)              (the core)                 (optional)
  mail arrives   ──▶   download the report   ──▶   check the numbers
 outlook_watch.py     opendoc_export.py  or          fincheck/
                        sap_export.py
```

Only the middle piece is essential. **Build and prove it first.**

---

# Path A — BusinessObjects (has a CUID)

BusinessObjects ships an official URL API called **OpenDocument**. The
document, the output format and every prompt value go into one URL, and the
server replies with the file. No iframes, no prompt dialog, no Export menu, no
clicking — and nothing that breaks when the portal is patched.

## Step 1 — Collect three things

**The CUID.** Open the report, find its properties panel (the ⓘ / properties
icon), and copy the value labelled **CUID** — something like
`ARdnN66G7cBGg.5DtIoAuDE`. The numeric **ID** next to it works as a fallback.

**The BusinessObjects server.** This is usually *not* the portal address. With
the report open, press F12 → **Console**, set the context dropdown (top-left of
the console, normally reads `top`) to the report's frame, then run:

```js
location.href
```

The host in the result is your BOE server — e.g. `https://boe.example.com`.
Ignore any `/NNNNNNNNNN/` version segment in the path; the canonical
`/BOE/OpenDocument/opendoc/openDocument.jsp` normally works and does not change
when BI is patched.

**The prompt names.** Open the report's Prompts dialog and note each prompt's
label exactly as displayed, plus the value you want.

## Step 2 — Build the URL and test it in a browser

```
https://<boe-host>/BOE/OpenDocument/opendoc/openDocument.jsp
  ?sIDType=CUID&iDocID=<cuid>
  &sOutputFormat=E
  &sRefresh=Y
  &lsSFirst+Prompt=value&lsSSecond+Prompt=value
```

| Parameter | Meaning |
|---|---|
| `sIDType=CUID` | Look the document up by CUID (`InfoObjectID` to use the numeric ID) |
| `sOutputFormat` | `E` Excel `.xls` · `X` Excel `.xlsx` · `P` PDF · `C` CSV · `H` HTML |
| `sRefresh=Y` | Re-run the query rather than returning cached data |
| `lsS<Prompt Name>` | A single-value prompt. Spaces become `+` |
| `lsM<Prompt Name>` | A multi-value prompt, values comma-separated |

**Paste it into a browser where you are already signed in.** Two behaviours are
worth knowing:

- **Paste it twice.** The first OpenDocument request of a session redirects
  through logon, and the redirect drops the query parameters — so you get the
  interactive viewer instead of a file. The redirect leaves a session behind,
  so the second attempt works. The script handles this automatically.
- **A prompt page appearing is good news.** It means the document opened and
  only the prompt *names* need correcting. Compare them character for character
  with the Prompts dialog, including capitalisation and any trailing colon;
  some documents prefix them with `Enter value for `.

When Excel downloads, you are done — that URL is the whole automation.

## Step 3 — Run it from Python

```bat
pip install requests selenium
```

Copy `run_boe_export.example.bat` to `run_boe_export.bat`, fill in the values,
and run it. The real file is git-ignored, so hostnames and credentials stay out
of the repository.

```bat
set "BOE_BASE=https://boe.example.com"
set "BOE_DOC_CUID=ARdnN66G7cBGg.5DtIoAuDE"
set "BOE_FORMAT=E"
set "BOE_PROMPTS=Consol Group=G_GRUP;Fiscal Year=2027;From Period=1;From To=3"
set "SAP_PORTAL_BASE=https://portal.example.com"

python opendoc_export.py --print-url     :: check the URL first
python opendoc_export.py --output pl.xls
```

The script signs in with Selenium once — first at the portal so corporate SSO
runs, then at the BOE server itself, because cookies are per-host and the file
comes from the BOE host. It then hands those cookies to `requests` and streams
the file down. The browser closes before the download starts.

If the direct request is ever refused, `--browser` makes the browser fetch the
document instead, inheriting whatever authentication it already has.

### Troubleshooting

**An HTML page comes back instead of a file** — the script retries once
automatically. If it still happens, a prompt has no value, or the session lacks
rights on that document. The error prints the first part of the response.

**404** — the OpenDocument servlet is mounted elsewhere. Set
`BOE_OPENDOC_PATH`, including the version segment from `location.href` if
needed.

**A login page** — the portal session did not extend to the BOE host. Open
`https://<boe-host>/BOE/BI` once in the same browser, then retry.

## Before building any of this: check scheduling

BusinessObjects can do this itself. In BI Launchpad, right-click the document →
**Schedule** → set a recurrence, format Excel, and a destination (email, or a
file share). The server runs it and delivers the file — no scripts, no browser,
no machine that has to be switched on.

That is the sanctioned answer to "automate this report" and it sidesteps every
governance question. Ask whether scheduling is enabled for your account before
committing to a script. Use OpenDocument when you need ad-hoc parameters that a
fixed schedule cannot express.

---

# Path B — BEx Web (no CUID)

A BEx report is rendered by the portal itself, and there is no document API to
call, so `sap_export.py` drives the UI with Selenium. Three things defeat naive
automation, and the script handles each:

1. **The report sits inside nested iframes** (`contentAreaFrame` →
   `isolatedWorkArea`). Selenium only sees the top document until you switch
   into them — this is the usual reason people conclude "Selenium can't see the
   Export button".
2. **Element IDs are generated per session**, so locators built on them rot
   within days. The script matches visible text and `title` attributes instead.
3. **The report renders asynchronously**, so every step waits for its element
   rather than sleeping a fixed time.

## Setup

Get the report's direct URL: open it, press F12 → **Elements**, Ctrl+F for
`isolatedWorkArea`, and copy the iframe's `src`. Keep the path and
`ExecuteLocally=true`; drop `windowId`, `NavMode` and `PrevNavTarget` — they are
one-off session breadcrumbs.

Then copy `run_export.example.bat` to `run_export.bat`, set `SAP_PORTAL_BASE`
and `SAP_REPORT_PATH`, and run it. Leave the credentials commented out; under
SSO they are not needed, and the script says so plainly if a logon form appears.

A successful run prints each stage separately — open URL, login, variable
screen, toolbar, export, download — so the first failing line tells you exactly
what to fix.

**Report parameters:** the script clicks **OK** on the Variable Entry screen,
accepting whatever selection BW remembered. To change the period, run the report
manually once with the values you want; the automation inherits them.

---

# Optional — fire it automatically when a mail arrives

`outlook_watch.py` polls your Outlook Inbox once a minute for unread mail whose
subject or body contains your trigger words, runs the export, and tags the mail
with the category `SAP-Export-Done` so it is never handled twice. A failed
export leaves the mail untagged, so it retries on the next poll.

```bat
pip install pywin32
set "WATCH_KEYWORDS=trial balance ready,TB refresh done"
python outlook_watch.py
```

Honest limits: **classic Outlook desktop only** (the new Outlook is web-based
and exposes no COM interface), and the machine must be on, logged in, with
Outlook running. For hands-off starts, add it to Task Scheduler with the trigger
*At log on*.

Wire this up only after the export itself has succeeded twice in a row. A
trigger attached to a broken script fails silently on a schedule.

---

# Approaches considered and rejected

Recorded so nobody re-treads them:

- **A VBA macro doing the download.** VBA's built-in web automation drives only
  Internet Explorer, which no longer exists. VBA *is* fine as an event-driven
  trigger that shells out to the Python script — a drop-in alternative to
  `outlook_watch.py` where macro settings permit it.
- **Screen-coordinate recorders (PyAutoGUI and similar).** They replay mouse
  positions, so any popup, resolution change or slow render breaks them
  silently.
- **Record-and-replay RPA** (Power Automate Desktop, Playwright `codegen`,
  Selenium IDE). Genuinely useful as a *capture device* for learning real
  selectors. Do not ship the raw recording: it memorises literal steps against
  per-session IDs and rots quickly.
- **Full RPA platforms** (UiPath, Power Automate, AssistEdge). The right answer
  when this becomes a *team* asset needing unattended runs, credential vaults,
  audit logs and central monitoring. Overkill for one person on one laptop —
  but a working script is the ideal specification to hand an automation team.

---

# A note on what is safe to share

Screenshots of dialogs, element HTML, and parameter *names* are fine to paste
into a ticket or a chat. Never share cookie values, anything called
token/SSO/session, passwords, or a screenshot of the *populated* report — that
is live financial data.

Keep this repository private if your configuration files carry report paths, and
remember that an automation signing into a financial system, even under your own
account, is worth a heads-up to your manager once it becomes routine — every run
is logged as you.
