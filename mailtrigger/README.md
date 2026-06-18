# mailtrigger — run a report when a specific Outlook email arrives

`mailtrigger` watches your Outlook inbox. When a message that matches your rules
lands, it **fetches a report from a URL** (with the parameters you define),
processes the response, and delivers the result — saved to a folder and,
optionally, emailed back.

```
watch inbox  →  match trigger email  →  fetch report from URL (+params)  →  process  →  save / email
```

Everything is driven by one `config.yaml`, so the same tool can be pointed at
any sender, any URL, and any parameters without touching the code.

## Install

```bash
pip install -r requirements.txt
```

## Configure

```bash
cp config.example.yaml config.yaml
# edit config.yaml, then export your secrets so they stay out of the file:
export MAILTRIGGER_PASSWORD='your-outlook-app-password'
export REPORT_TOKEN='your-report-api-token'   # only if your URL needs auth
```

Key things to set in `config.yaml`:

| Section    | What it controls                                                        |
|------------|------------------------------------------------------------------------|
| `mailbox`  | Which inbox to watch (IMAP host, username, app password, poll interval) |
| `trigger`  | Which message fires a run (sender / subject / body / has-attachment)    |
| `report`   | The URL to fetch, the HTTP method, and your **defined parameters**      |
| `process`  | How to shape the response (json field picks, csv column picks, or raw)  |
| `output`   | Where the result goes (folder, and optional email-back)                 |

> **Outlook note:** use an **app password** (Account → Security → App passwords),
> not your normal password, if MFA is enabled. IMAP must be allowed on the
> account — many Microsoft 365 *work* tenants disable it, in which case the
> Microsoft Graph backend (see "Roadmap") is the way to go.

## Run

```bash
# 1. Validate the config without connecting
python -m mailtrigger check-config

# 2. Do a single inbox check (great for testing)
python -m mailtrigger run-once

# 3. Watch forever, polling every `poll_seconds`
python -m mailtrigger watch
```

To keep it running in the background, use a process manager (systemd, `pm2`,
Task Scheduler, a container, etc.) pointed at `python -m mailtrigger watch`.

## How matching works

All of the conditions you set under `trigger` must be true (case-insensitive
"contains"); blank ones are ignored. So this only fires on mail from
`reports@vendor.com` whose subject contains `Run report`:

```yaml
trigger:
  from_contains: "reports@vendor.com"
  subject_contains: "Run report"
```

Processed messages are marked **read** so they never run twice. If a run fails,
the message is left **unread** and retried on the next poll.

## Where the report URL comes from

By default the fixed `report.url` is fetched. If instead the **email itself
carries the link**, set a regex and the first match in the body is used:

```yaml
report:
  url_from_email_regex: 'https?://\S+'
```

## Tests

```bash
pytest
```

Tests cover the trigger matching and the response processing — the pure logic,
no network or mailbox needed.

## Roadmap / extension points

- **Microsoft Graph backend** — for work tenants where IMAP is disabled, or to
  use push subscriptions instead of polling. `mailbox.py` is intentionally small
  and returns a plain `Email` object, so a Graph implementation can sit behind
  the same interface.
- **More processors** — Excel/PDF generation, templated HTML, etc., by adding to
  `processor.py`.
- **More outputs** — post to Teams/Slack by adding to `output.py`.
