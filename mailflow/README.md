# mailflow — scheduled mail automation with revert tracking

`mailflow` is a standalone tool for **corporate, recurring e-mail**. It:

* **sends** mail on a schedule (interval / daily / weekly / one-shot) over SMTP,
  with To/Cc/Bcc, HTML bodies, `{placeholder}` templating, and glob attachments;
* **archives** a copy of every message it sends as a `.eml` file in a path you
  choose;
* **tracks the revert status** of each message — i.e. whether the recipient
  replied — automatically by polling IMAP, or manually via a command;
* keeps a durable **SQLite record** of every send so status survives restarts.

It is a self-contained Python package (no framework, no external services).
Its only third-party dependency is `PyYAML`; everything else is the standard
library (`smtplib`, `imaplib`, `email`, `sqlite3`).

> **Secrets stay out of the config.** The YAML names an environment variable
> for each password; the file itself never holds a credential.

## Install

```bash
cd mailflow
pip install -r requirements.txt
```

Run it as a module from this directory (or after adding it to `PYTHONPATH`):

```bash
python -m mailflow --help
```

## Quick start

```bash
# 1. Write a starter config and edit it
python -m mailflow init                 # creates mailflow.yaml

# 2. Provide the passwords via environment (never in the file)
export MAILFLOW_SMTP_PASSWORD='app-or-account-password'
export MAILFLOW_IMAP_PASSWORD='app-or-account-password'   # only if using IMAP

# 3. Sanity-check config + connectivity
python -m mailflow validate             # shows each job's next run time
python -m mailflow test-connection      # logs into SMTP (and IMAP)

# 4. Try it safely, then for real
python -m mailflow run --dry-run        # show what would fire, send nothing
python -m mailflow send weekly-financials   # send one job now, ignoring schedule

# 5. See what happened and who reverted
python -m mailflow status
python -m mailflow check-replies        # poll IMAP, update revert status
```

## Two ways to drive it

| | **Excel-driven** (`xlsx` commands) | **Config jobs** (YAML) |
|---|---|---|
| Control surface | a spreadsheet — one row per mail | `jobs:` in `mailflow.yaml` |
| Best for | business users, ad-hoc / mail-merge batches, a visible status board | fixed recurring jobs under version control |
| Status lives in | the sheet itself (written back) | a SQLite database |

Both share the same engine (sending, archiving, IMAP revert detection) and the
same server config; **credentials are never in the spreadsheet** — they stay in
the YAML/env.

## Excel-driven mode

Each **row is one e-mail**. You fill in what to send; mailflow sends the rows
whose time has come and **writes the status back into the same sheet**.

```bash
python -m mailflow xlsx init mails.xlsx        # create a template to fill in
# ... edit rows in Excel: To, Subject, Body, Send Time, attachments ...
export MAILFLOW_SMTP_PASSWORD=...              # creds still come from env
python -m mailflow xlsx send mails.xlsx        # send due rows, write status back
python -m mailflow xlsx check-replies mails.xlsx   # reconcile reverts via IMAP
python -m mailflow xlsx run mails.xlsx         # send + reconcile (wire to cron)
```

**Input columns** (case-insensitive, with aliases):

| Column | Meaning |
|---|---|
| `To` | recipient(s), separated by `,` or `;` |
| `CC` | optional cc recipient(s) |
| `Subject` | subject line — supports `{Column}` placeholders |
| `Body` | message body — supports `{Column}` placeholders |
| `Attachments` | path(s)/glob(s), separated by `;` or new lines |
| `Send Time` | when to send (e.g. `2026-06-22 08:00`); **blank = send now** |

Any **other column** can be used as a `{Column Name}` placeholder in the Subject
or Body — i.e. **mail-merge straight from the sheet** (e.g. a `Region` column and
`Subject: "Weekly Report - {Region}"`).

**Columns mailflow writes back** (created automatically if absent):

`Sent Status` · `Sent At` · `Message ID` · `Sent Path` · `Received Status` ·
`Received From` · `Received At` · `Received Path` · `Notes`

* a row is sent only when `Send Time` ≤ now and `Sent Status` isn't already `Sent`
  (so re-running is **idempotent** — it never double-sends);
* every sent message is archived as `.eml` under `defaults.save_dir`; its path is
  written to `Sent Path`;
* when a reply is detected (matched by `Message ID` over IMAP) the row flips to
  `Received`, recording who replied and saving the reply under
  `defaults.received_dir`, with its path in `Received Path`.

Run `xlsx send --dry-run` to preview without sending or writing anything.

## Live demo & presentation

See it work end to end (no internet or real mailbox needed):

```bash
bash demo/run_demo.sh          # full lifecycle against a local capture relay
```

Open [`demo/presentation.html`](demo/presentation.html) in a browser for a
corporate slide deck. Details in [`demo/README.md`](demo/README.md).

## Commands

| Command | What it does |
|---|---|
| `init` | Write an example `mailflow.yaml`. |
| `validate` | Parse the config and print each job's schedule and next run. |
| `test-connection` | Verify SMTP (and IMAP) login. |
| `send <job>` | Send one named job **now**, ignoring its schedule. |
| `run` | Send every job that is currently **due** once, then exit (for cron). |
| `daemon` | Run continuously, firing jobs as they come due and polling for reverts. |
| `check-replies` | Poll IMAP once and mark any sends that received a reply. |
| `mark-replied <id>` | Manually record a revert (when you don't use IMAP). |
| `status` | Table of every send with its revert status. |

Add `--dry-run` to `send` / `run` / `daemon` to archive and record without
actually contacting the SMTP server. Use `-c PATH` to point at a non-default
config, and `-v` for debug logging.

## How scheduling works

Each job has a `schedule`:

```yaml
schedule: {every: interval, minutes: 30}     # or seconds:/hours:
schedule: {every: daily,    at: "07:30"}
schedule: {every: weekly,   weekday: mon, at: "08:00"}
schedule: {every: once,     at: "2026-07-01T09:00:00"}
```

A job is **due** when its next scheduled moment is at or before now, based on
when it last fired (stored in SQLite). Clock-based schedules catch up a single
missed slot, so a daily 08:00 job still fires once even if `run` is invoked a
bit late.

You have two ways to actually drive the schedule:

* **One-shot for cron / Task Scheduler** — let the OS scheduler decide *when to
  check*, and let `mailflow` decide *what is due*:

  ```cron
  # check every 15 minutes; mailflow fires only the jobs that are due
  */15 * * * *  cd /opt/mailflow && python -m mailflow run >> mailflow.log 2>&1
  ```

* **Long-running daemon** — one process that polls on `poll_seconds` and also
  refreshes revert status each cycle:

  ```bash
  python -m mailflow daemon
  ```

  (Wrap it in systemd / supervisor / a Windows service for resilience.)

## Revert (reply) tracking

For every send, `mailflow` stores the outgoing `Message-ID`. A reply is matched
**by thread headers** (`In-Reply-To` / `References`) — the reliable signal that
survives subject edits — and the send is flipped from `sent` to `replied`,
recording who replied and when.

* **Automatic:** configure the optional `imap:` section and run
  `check-replies` (or the `daemon`, which does it each cycle). Each job's
  `reply_window_days` bounds how long a send is considered to be awaiting a
  revert.
* **Manual:** if you don't grant mailbox access, use
  `mailflow mark-replied <id> --from someone@corp.com`.

`mailflow status` shows the current state of each send: `sent` (awaiting),
`replied` (reverted), or `failed`.

## Archiving

Every message is written to disk as a timestamped `.eml`
(`20260619-080000_weekly-financials.eml`) under the job's `save_dir` (falling
back to the global `defaults.save_dir`). `.eml` opens in Outlook, Thunderbird,
Apple Mail, etc., giving you a verbatim audit copy of exactly what was sent.

## Library use

```python
from mailflow import load_config, Tracker, SmtpSender, run_due, send_job

config = load_config("mailflow.yaml")
with Tracker(config.database) as tracker:
    sender = SmtpSender(config.smtp)
    run_due(config, sender, tracker)                          # all due jobs
    send_job(config.job("weekly-financials"), config, sender, tracker)  # one now
```

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for a fully-commented example.
Top-level keys:

* `database` — SQLite file for send/revert status (default `mailflow.db`).
* `poll_seconds` — daemon poll interval (default `60`).
* `smtp` — `host`, `port`, `security` (`starttls`/`ssl`/`none`), `username`,
  `password_env`, optional `from_addr`, `timeout`.
* `imap` *(optional)* — same shape plus `mailbox`; enables auto revert detection.
* `defaults` — `save_dir`, `track_replies`, `reply_window_days` applied to jobs.
* `jobs[]` — `name`, `to`, `cc`, `bcc`, `subject`, `body`, `html_body`,
  `attachments` (globs), `variables`, `schedule`, `save_dir`, `track_replies`.

Templating: `{date}`, `{time}`, `{datetime}`, `{job}`, plus any keys under a
job's `variables`. Unknown `{placeholders}` are left untouched.

## Security notes

* Passwords are read only from the environment variables you name — keep your
  live `mailflow.yaml` and `mailflow.db` out of version control (the bundled
  `.gitignore` already excludes them).
* SMTP/IMAP use `starttls`/`ssl` with certificate verification by default.
* Many enterprises (e.g. Microsoft 365) disable basic SMTP/IMAP auth and require
  OAuth2 / Graph. The send and reply layers are isolated behind small classes
  (`SmtpSender`, `ImapReplyChecker`) so an OAuth/Graph backend can be added
  without touching the scheduler or tracker.

## Tests

```bash
python -m pytest
```

Covers config parsing/validation, message building and templating, schedule
arithmetic, the SQLite tracker, and an end-to-end send via a dry-run backend —
all offline, with no network access required.
```
