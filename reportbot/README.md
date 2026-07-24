# reportbot — Outlook-triggered report downloader

`reportbot` watches an Outlook mailbox and, when an email matching one of your
**trigger rules** arrives, logs into a portal and **downloads the report(s)**
for you — automatically, on a poll loop.

```
 mailbox  ──poll──▶  trigger rules  ──match──▶  portal login + download  ──▶  ./downloads/<rule>/
```

It is a configurable scaffold: the mail backend, the trigger conditions, and the
portal steps are all set in a YAML config file, so you adapt it to your account
and portal **without editing code**.

## What's included

| Piece | Backend / tech | Status |
|-------|----------------|--------|
| Read Outlook — Microsoft 365 / Outlook.com | Microsoft Graph API (`msal`) | works once you add an Azure app registration |
| Read Outlook — any IMAP mailbox | Python stdlib `imaplib` | works with email + app password |
| Match incoming mail | trigger rules (sender / subject / body / regex) | fully working, tested |
| Download from the portal | Playwright (headless Chromium) | works once you set the URL + selectors |
| Don't download twice | JSON de-dupe state | fully working, tested |

## Quick start

```bash
cd reportbot
pip install -r requirements.txt
playwright install chromium          # only if you use the portal downloader

cp config.example.yaml config.yaml   # then edit config.yaml
cp .env.example .env                  # then put your secrets here
set -a && source .env && set +a       # load secrets into the environment

# See what recent mail matches your rules (no downloads):
python -m reportbot -c config.yaml check

# Check once and download anything new:
python -m reportbot -c config.yaml once

# Run continuously on the poll loop:
python -m reportbot -c config.yaml run
```

## Configuration

Everything lives in `config.yaml` (start from `config.example.yaml`). Secrets are
written as `${ENV_VAR}` and read from the environment, so the file itself carries
no passwords. `config.yaml`, `.env`, and `downloads/` are git-ignored.

### 1. Choose how to read the mailbox

**Microsoft Graph (`backend: graph`)** — recommended for Microsoft 365 /
Outlook.com. Register an app in Azure AD, grant it the **`Mail.Read`
application** permission (with admin consent), and create a client secret. Put
`tenant_id`, `client_id`, `client_secret`, and the mailbox `user` in the config.

**IMAP (`backend: imap`)** — simplest if your account allows IMAP. Use your
email as `username` and an **app password** as `password`. No app registration
needed; uses only the standard library.

### 2. Define trigger rules

A rule fires only when **all** of its conditions match (matching is
case-insensitive):

```yaml
triggers:
  - name: daily-report
    from_contains: [reports@portal.example.com]   # any one sender substring
    subject_contains: ["Report ready"]            # any one subject substring
    subject_regex: "invoice #\\d+"                # optional
    body_contains: ["download"]                    # optional
```

### 3. Tell it how to get the report

```yaml
portal:
  login_url: https://portal.example.com/login
  reports_url: https://portal.example.com/reports
  username: ${PORTAL_USERNAME}
  password: ${PORTAL_PASSWORD}
  selectors:
    username: "#username"
    password: "#password"
    submit: "button[type=submit]"
    download: "a.download-report"
```

Find the right selectors by right-clicking each element in the portal and
choosing **Inspect**. If instead the report is a **direct link inside the
email**, set `link_regex` (with a capture group for the URL) and the bot fetches
that link directly rather than navigating the portal UI.

## Library use

```python
from reportbot import Config, Runner

config = Config.load("config.yaml")
runner = Runner(config)
events = runner.run_once()
for e in events:
    print(e.rule.name, "->", [str(f) for f in e.files])
```

The `Runner` accepts injected `mail_client`, `downloader`, or `download_fn`, so
it's easy to test or extend without touching the network.

## Running the tests

```bash
pip install pytest
python -m pytest reportbot/tests -q
```

The core logic (config parsing, trigger matching, de-dupe state, and the runner
orchestration) is covered by tests that use fakes — no mailbox, browser, or
network required.

## Security & privacy notes

- Secrets stay in `.env` / environment variables, never in committed files.
- The mail backend reads mail **read-only** (Graph `Mail.Read`; IMAP `readonly`
  select) — the bot never deletes or sends mail.
- Downloaded reports are written under `download_dir` and are git-ignored.
- Prefer app passwords / least-privilege app registrations over your primary
  credentials.

## Filling in the unknowns

This scaffold was built before the specific mailbox and portal were decided.
To go live, you need to supply three things in `config.yaml`:

1. **Mailbox access** — Graph app registration *or* IMAP app password.
2. **Trigger rules** — which sender/subject identifies the report email.
3. **Portal steps** — `login_url`, `reports_url`, and the CSS `selectors`
   (or a `link_regex` if the email carries a direct download link).

Run `python -m reportbot check` after each change to confirm the right emails
are being matched before enabling downloads.
