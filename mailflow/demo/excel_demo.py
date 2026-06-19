"""Live demo of the Excel-driven mode.

Builds a small spreadsheet (one row per mail), sends the due rows FOR REAL
through the local capture relay, prints the sheet after each stage, then
simulates an inbound revert so you can see the Received columns fill in.

The IMAP matching logic itself is real and unit-tested; here we feed it one
canned reply so the demo needs no live mailbox.

    python excel_demo.py --port 8025
"""

from __future__ import annotations

import argparse
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from mailflow import Spreadsheet, send_due
from mailflow.config import Config, ImapConfig, SmtpConfig
from mailflow.excel import check_replies
from mailflow.replies import ReplyHit
from mailflow.sender import SmtpSender

SHEET = "acme_mails.xlsx"

SHOW = ["To", "Subject", "Send Time", "Sent Status", "Sent Path",
        "Received Status", "Received From", "Received Path"]


def build_sheet() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mails"
    headers = ["To", "CC", "Subject", "Body", "Attachments", "Send Time", "Region"]
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        ws.cell(1, c).font = Font(bold=True)
    ws.append([
        "cfo@acme.example, controller@acme.example", "audit@acme.example",
        "Acme Weekly Report - {Region} - {date}",
        "Hi team, please find the {Region} figures attached. Kindly revert.",
        "", "", "Group",
    ])
    ws.append([
        "ap@acme.example", "",
        "Vendor Statement - {Region}",
        "Please reconcile the attached {Region} vendor statement.",
        "", "", "EMEA",
    ])
    ws.append([
        "board@acme.example", "",
        "Board Pack - {Region}",
        "Board pack for review.",
        "", "2999-01-01 08:00", "Group",   # far future -> not due
    ])
    wb.save(SHEET)


def dump(title: str) -> None:
    wb = openpyxl.load_workbook(SHEET)
    ws = wb.active
    head = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    print(f"\n--- spreadsheet: {title} ---")
    widths = {"To": 34, "Subject": 30, "Send Time": 18, "Sent Status": 11,
              "Sent Path": 22, "Received Status": 15, "Received From": 18,
              "Received Path": 22}

    def cell(r, name):
        v = ws.cell(r, head[name]).value if name in head else ""
        v = "" if v is None else str(v)
        if name.endswith("Path") and v:
            v = ".../" + Path(v).name
        return v[: widths[name]].ljust(widths[name])

    print(" | ".join(n[: widths[n]].ljust(widths[n]) for n in SHOW))
    print("-" * 160)
    for r in range(2, ws.max_row + 1):
        if not ws.cell(r, head["To"]).value:
            continue
        print(" | ".join(cell(r, n) for n in SHOW))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8025)
    args = ap.parse_args()

    config = Config(
        smtp=SmtpConfig(
            host="127.0.0.1", port=args.port, security="none", auth=False,
            username="finance-automation@acme.example", password_env="",
            from_addr="finance-automation@acme.example",
        ),
        # A placeholder IMAP block so check_replies is allowed; the fake checker
        # below stands in for a real connection.
        imap=ImapConfig(host="imap.local", port=993, security="ssl",
                        username="x", password_env="X"),
        jobs=[], save_dir="archive_sent", received_dir="archive_received",
    )

    print("=== 1. Build the control spreadsheet (one row = one mail) ===")
    build_sheet()
    dump("BEFORE — three mails queued")

    print("\n=== 2. Send all DUE rows for real (through the relay) ===")
    sheet = Spreadsheet(SHEET)
    summary = send_due(sheet, config, SmtpSender(config.smtp))
    print(f"send summary: sent={summary.sent} not-due={summary.skipped_not_due}")
    dump("AFTER SEND — status written back, board row not yet due")

    print("\n=== 3. The CFO reverts. Reconcile reverts from the mailbox ===")
    wb = openpyxl.load_workbook(SHEET)
    ws = wb.active
    head = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    cfo_msgid = ws.cell(2, head["Message ID"]).value  # row 2 = the CFO mail

    class FakeChecker:
        def connect(self):
            return object()

        def find_reply(self, conn, message_id):
            if message_id == cfo_msgid:
                return ReplyHit(
                    from_addr="cfo@acme.example",
                    raw=b"From: CFO <cfo@acme.example>\r\n"
                        b"Subject: Re: Acme Weekly Report\r\n\r\nApproved.",
                    received_at="2026-06-19 09:41:00",
                )
            return None

        def _logout(self, conn):
            pass

    r = check_replies(Spreadsheet(SHEET), config, checker=FakeChecker())
    print(f"revert summary: checked={r.checked} received={r.received}")
    dump("AFTER REVERT — CFO row now Received, with sender + saved path")

    print("\n=== 4. On-disk archives ===")
    for label, d in [("sent", "archive_sent"), ("received", "archive_received")]:
        files = sorted(Path(d).glob("*.eml")) if Path(d).exists() else []
        print(f"  {label}: " + (", ".join(f.name for f in files) or "(none)"))


if __name__ == "__main__":
    main()
