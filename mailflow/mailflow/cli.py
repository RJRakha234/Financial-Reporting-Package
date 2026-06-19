"""Command-line interface: ``python -m mailflow <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, ConfigError, load_config
from .excel import (
    ExcelError,
    Spreadsheet,
    check_replies as xlsx_check_replies,
    make_template,
    send_due,
)
from .replies import ImapReplyChecker
from .scheduler import next_run, run_due, run_forever, send_job
from .sender import DryRunSender, Sender, SmtpSender
from .tracker import Tracker

EXAMPLE_CONFIG = """\
# mailflow configuration. Passwords are NEVER stored here — set the named
# environment variables instead (e.g. export MAILFLOW_SMTP_PASSWORD=...).

database: mailflow.db          # where send/revert status is recorded
poll_seconds: 60               # how often the daemon checks for due jobs

smtp:
  host: smtp.office365.com
  port: 587
  security: starttls           # starttls | ssl | none
  username: reports@example.com
  password_env: MAILFLOW_SMTP_PASSWORD
  from_addr: reports@example.com   # optional; defaults to username

# Optional — only needed to auto-detect replies ("reverts").
imap:
  host: outlook.office365.com
  port: 993
  security: ssl
  username: reports@example.com
  password_env: MAILFLOW_IMAP_PASSWORD
  mailbox: INBOX

defaults:
  save_dir: sent_mail          # every message is archived here as .eml
  track_replies: true
  reply_window_days: 14

jobs:
  - name: weekly-financials
    to: [cfo@example.com, controller@example.com]
    cc: [audit@example.com]
    subject: "Weekly financial report — {date}"
    body: |
      Hi team,

      Please find this week's financial report attached.
      Kindly revert with any corrections.

      Regards,
      Reporting Automation
    attachments:
      - reports/weekly_*.pdf
    schedule:
      every: weekly            # interval | daily | weekly | once
      weekday: mon
      at: "08:00"
    save_dir: sent_mail/weekly

  - name: daily-heartbeat
    to: ops@example.com
    subject: "Daily pipeline status — {datetime}"
    body: "Automated daily status. No reply needed."
    track_replies: false
    schedule:
      every: daily
      at: "07:30"
"""


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _load(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def _make_sender(config: Config, dry_run: bool) -> Sender:
    return DryRunSender() if dry_run else SmtpSender(config.smtp)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote example config to {path}")
    print("Next: edit it, then set the password env vars and run "
          "`python -m mailflow validate`.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args)
    print(f"✓ config OK — {len(config.jobs)} job(s):")
    now = datetime.now()
    with Tracker(config.database) as tracker:
        for job in config.jobs:
            sched = job.schedule.describe() if job.schedule else "manual only"
            nxt = (
                next_run(job.schedule, tracker.last_run(job.name), now)
                if job.schedule
                else None
            )
            nxt_s = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "—"
            print(f"  • {job.name}: {len(job.to)} recipient(s), {sched}; next: {nxt_s}")
    print(f"IMAP reply detection: {'enabled' if config.imap else 'disabled'}")
    return 0


def cmd_test_connection(args: argparse.Namespace) -> int:
    import imaplib
    import smtplib
    import ssl

    config = _load(args)
    ok = True

    cfg = config.smtp
    try:
        if cfg.security == "ssl":
            s: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout)
        else:
            s = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        with s:
            s.ehlo()
            if cfg.security == "starttls":
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if cfg.auth:
                s.login(cfg.username, cfg.password())
        verb = "login OK" if cfg.auth else "connection OK (no auth)"
        print(f"✓ SMTP {verb} ({cfg.username}@{cfg.host})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"✗ SMTP failed: {exc}", file=sys.stderr)

    if config.imap:
        icfg = config.imap
        try:
            if icfg.security == "ssl":
                c: imaplib.IMAP4 = imaplib.IMAP4_SSL(icfg.host, icfg.port, timeout=icfg.timeout)
            else:
                c = imaplib.IMAP4(icfg.host, icfg.port, timeout=icfg.timeout)
                if icfg.security == "starttls":
                    c.starttls(ssl.create_default_context())
            c.login(icfg.username, icfg.password())
            c.select(icfg.mailbox, readonly=True)
            c.logout()
            print(f"✓ IMAP login OK ({icfg.username}@{icfg.host})")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"✗ IMAP failed: {exc}", file=sys.stderr)

    return 0 if ok else 1


def cmd_send(args: argparse.Namespace) -> int:
    config = _load(args)
    job = config.job(args.job)
    sender = _make_sender(config, args.dry_run)
    with Tracker(config.database) as tracker:
        send_id = send_job(job, config, sender, tracker, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] would send {job.name} to "
                  f"{', '.join(job.all_recipients())} (nothing written)")
            return 0
        record = tracker.find_by_id(send_id)
    if record and record.status == "failed":
        print(f"✗ {job.name} failed: {record.error}", file=sys.stderr)
        return 1
    print(f"✓ sent {job.name} (id={send_id})"
          + (f", saved to {record.saved_path}" if record and record.saved_path else ""))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    sender = _make_sender(config, args.dry_run)
    with Tracker(config.database) as tracker:
        ids = run_due(config, sender, tracker, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    if not ids:
        print(f"{prefix}no jobs due.")
    elif args.dry_run:
        print(f"[dry-run] {len(ids)} job(s) would fire now (nothing written)")
    else:
        print(f"fired {len(ids)} job(s): ids {ids}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    config = _load(args)
    sender = _make_sender(config, args.dry_run)
    tracker = Tracker(config.database)
    reply_check = None
    if config.imap and not args.dry_run:
        checker = ImapReplyChecker(config.imap)
        reply_check = lambda: checker.check(tracker, _max_window(config))  # noqa: E731
    try:
        run_forever(config, sender, tracker, reply_check=reply_check)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        tracker.close()
    return 0


def cmd_check_replies(args: argparse.Namespace) -> int:
    config = _load(args)
    if not config.imap:
        print("error: no [imap] section in config; cannot auto-detect replies.",
              file=sys.stderr)
        return 2
    checker = ImapReplyChecker(config.imap)
    with Tracker(config.database) as tracker:
        result = checker.check(tracker, _max_window(config))
    print(f"checked {result.checked} pending send(s); "
          f"{result.newly_replied} new revert(s) recorded.")
    return 0


def cmd_mark_replied(args: argparse.Namespace) -> int:
    config = _load(args)
    with Tracker(config.database) as tracker:
        record = tracker.find_by_id(args.id)
        if not record:
            print(f"error: no send with id {args.id}", file=sys.stderr)
            return 2
        if tracker.mark_replied(args.id, reply_from=args.From):
            print(f"✓ marked send id={args.id} as replied.")
        else:
            print(f"send id={args.id} was already marked replied.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    with Tracker(config.database) as tracker:
        records = tracker.all_sends(job=args.job, limit=args.limit)
        counts = tracker.counts()
    if not records:
        print("no sends recorded yet.")
        return 0
    icon = {"sent": "…", "replied": "✓", "failed": "✗"}
    print(f"{'id':>4}  {'status':<8} {'job':<22} {'sent':<16} subject")
    print("-" * 78)
    for r in records:
        sent = r.sent_at.replace("T", " ")[:16]
        mark = icon.get(r.status, "?")
        subject = (r.subject or "")[:32]
        print(f"{r.id:>4}  {mark} {r.status:<6} {r.job[:22]:<22} {sent:<16} {subject}")
        if r.status == "replied" and r.reply_from:
            print(f"       ↳ revert from {r.reply_from} at "
                  f"{(r.replied_at or '').replace('T', ' ')[:16]}")
        if r.status == "failed" and r.error:
            print(f"       ↳ error: {r.error[:60]}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print("-" * 78)
    print(f"totals: {summary}")
    return 0


def _max_window(config: Config) -> int:
    return max((j.reply_window_days for j in config.jobs), default=14)


# ---------------------------------------------------------------------------
# Excel-driven commands
# ---------------------------------------------------------------------------


def cmd_xlsx_init(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force)", file=sys.stderr)
        return 2
    make_template(path)
    print(f"Wrote spreadsheet template to {path}")
    print("Fill in To / Subject / Body / Send Time rows, then run "
          "`python -m mailflow xlsx send <file>`.")
    return 0


def cmd_xlsx_send(args: argparse.Namespace) -> int:
    config = _load(args)
    sheet = Spreadsheet(args.file, sheet=args.sheet)
    sender = _make_sender(config, args.dry_run)
    s = send_due(sheet, config, sender, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}sent={s.sent} failed={s.failed} "
          f"not-due={s.skipped_not_due} already-sent={s.already_sent}")
    if not args.dry_run:
        print(f"status written back to {sheet.path}")
    return 1 if s.failed else 0


def cmd_xlsx_check_replies(args: argparse.Namespace) -> int:
    config = _load(args)
    if not config.imap:
        print("error: no 'imap' section in config; cannot detect reverts.",
              file=sys.stderr)
        return 2
    sheet = Spreadsheet(args.file, sheet=args.sheet)
    r = xlsx_check_replies(sheet, config)
    print(f"checked {r.checked} awaiting row(s); {r.received} revert(s) recorded.")
    print(f"status written back to {sheet.path}")
    return 0


def cmd_xlsx_run(args: argparse.Namespace) -> int:
    """Send due rows, then (if IMAP configured) reconcile reverts — for cron."""
    rc = cmd_xlsx_send(args)
    config = _load(args)
    if config.imap and not args.dry_run:
        sheet = Spreadsheet(args.file, sheet=args.sheet)
        r = xlsx_check_replies(sheet, config)
        print(f"reverts: checked {r.checked}, recorded {r.received}.")
    return rc


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailflow",
        description="Schedule outgoing e-mail, archive each message, and "
        "track whether recipients reverted.",
    )
    parser.add_argument(
        "-c", "--config", default="mailflow.yaml", help="path to config (default: mailflow.yaml)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write an example config file")
    p.add_argument("--force", action="store_true", help="overwrite if it exists")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="validate config and show each job's next run")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("test-connection", help="verify SMTP (and IMAP) login")
    p.set_defaults(func=cmd_test_connection)

    p = sub.add_parser("send", help="send one job now, ignoring its schedule")
    p.add_argument("job", help="job name")
    p.add_argument("--dry-run", action="store_true", help="don't actually send")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="send all currently-due jobs once (for cron)")
    p.add_argument("--dry-run", action="store_true", help="don't actually send")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("daemon", help="run continuously, firing jobs as they come due")
    p.add_argument("--dry-run", action="store_true", help="don't actually send")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("check-replies", help="poll IMAP and update revert status")
    p.set_defaults(func=cmd_check_replies)

    p = sub.add_parser("mark-replied", help="manually mark a send as reverted")
    p.add_argument("id", type=int, help="send id (see `status`)")
    p.add_argument("--from", dest="From", default=None, help="who replied")
    p.set_defaults(func=cmd_mark_replied)

    p = sub.add_parser("status", help="show sends and their revert status")
    p.add_argument("--job", help="filter to one job")
    p.add_argument("--limit", type=int, default=50, help="max rows (default: 50)")
    p.set_defaults(func=cmd_status)

    # -- Excel-driven group ------------------------------------------------
    xl = sub.add_parser(
        "xlsx", help="drive mail from an Excel spreadsheet (row = one mail)"
    )
    xlsub = xl.add_subparsers(dest="xlsx_command", required=True)

    x = xlsub.add_parser("init", help="write a ready-to-edit .xlsx template")
    x.add_argument("file", help="path for the new .xlsx")
    x.add_argument("--force", action="store_true", help="overwrite if it exists")
    x.set_defaults(func=cmd_xlsx_init)

    x = xlsub.add_parser("send", help="send all due rows; write status back")
    x.add_argument("file", help="path to the .xlsx")
    x.add_argument("--sheet", help="worksheet name (default: first)")
    x.add_argument("--dry-run", action="store_true", help="don't actually send")
    x.set_defaults(func=cmd_xlsx_send)

    x = xlsub.add_parser("check-replies", help="reconcile reverts via IMAP; write back")
    x.add_argument("file", help="path to the .xlsx")
    x.add_argument("--sheet", help="worksheet name (default: first)")
    x.set_defaults(func=cmd_xlsx_check_replies)

    x = xlsub.add_parser("run", help="send due rows then reconcile reverts (for cron)")
    x.add_argument("file", help="path to the .xlsx")
    x.add_argument("--sheet", help="worksheet name (default: first)")
    x.add_argument("--dry-run", action="store_true", help="don't actually send")
    x.set_defaults(func=cmd_xlsx_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log_setup(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ExcelError as exc:
        print(f"spreadsheet error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
