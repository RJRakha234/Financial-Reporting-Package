#!/usr/bin/env python3
"""
outlook_watch.py — run the SAP Excel export whenever a matching mail arrives
in the Outlook desktop app.

Polls the local Outlook client (via COM, same mechanism VBA macros use — no
API registration, no admin rights) for unread Inbox messages whose subject or
body contains the configured trigger words. On a match it runs sap_export.py
and marks the mail with a category so it is never processed twice.

Requirements (Windows, Outlook desktop app installed and running):
    pip install pywin32

Configuration (environment variables):
    WATCH_KEYWORDS    Comma-separated trigger words/phrases. A mail matches
                      when ANY of them appears in the subject or body
                      (case-insensitive). Example:
                      WATCH_KEYWORDS=trial balance ready,TB refresh done
    WATCH_FROM        Optional. Only react to mails whose sender address
                      contains this text (e.g. a team mailbox or "@infosys").
    WATCH_INTERVAL    Seconds between Inbox checks. Default 60.
    WATCH_OUTPUT_DIR  Where exported files go, one per trigger mail,
                      timestamped. Default: ./sap_downloads
    ...plus everything sap_export.py itself needs (SAP_PORTAL_BASE,
    SAP_REPORT_PATH, SAP_USER, SAP_PASS, ...).

Run it in a console you leave open (or via Task Scheduler "At log on"):
    python outlook_watch.py
Stop with Ctrl+C.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

POLL_SECONDS = int(os.environ.get("WATCH_INTERVAL", "60"))
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("WATCH_KEYWORDS", "").split(",")
    if k.strip()
]
FROM_FILTER = os.environ.get("WATCH_FROM", "").lower()
OUTPUT_DIR = Path(os.environ.get("WATCH_OUTPUT_DIR", "sap_downloads")).absolute()
PROCESSED_CATEGORY = "SAP-Export-Done"

OL_FOLDER_INBOX = 6


def connect_outlook():
    try:
        import win32com.client  # pywin32
    except ImportError:
        sys.exit("pywin32 is required: pip install pywin32")
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        return outlook.GetNamespace("MAPI").GetDefaultFolder(OL_FOLDER_INBOX)
    except Exception as exc:  # noqa: BLE001 — COM raises various types
        sys.exit(
            f"Could not connect to Outlook ({exc}). Make sure the Outlook "
            "desktop app is installed and running. Note: the 'new Outlook' "
            "(web-based) does not expose COM — use classic Outlook."
        )


def mail_matches(mail) -> bool:
    try:
        if PROCESSED_CATEGORY in (mail.Categories or ""):
            return False
        sender = ""
        try:
            sender = (mail.SenderEmailAddress or "").lower()
        except Exception:  # some items (meeting invites) lack this
            pass
        if FROM_FILTER and FROM_FILTER not in sender:
            return False
        text = ((mail.Subject or "") + "\n" + (mail.Body or "")).lower()
        return any(k in text for k in KEYWORDS)
    except Exception:
        return False


def run_export(trigger_subject: str) -> bool:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = OUTPUT_DIR / f"trial_balance_{stamp}.xls"
    print(f"[{stamp}] Trigger mail: {trigger_subject!r} -> exporting ...")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "sap_export.py"),
         "--output", str(output)],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print("Export FAILED — the mail stays unmarked and will be retried "
              "on the next poll.")
        return False
    print(f"Export finished: {output}")
    return True


def mark_processed(mail) -> None:
    try:
        existing = mail.Categories or ""
        mail.Categories = (existing + ", " if existing else "") + PROCESSED_CATEGORY
        mail.Save()
    except Exception as exc:
        print(f"Warning: could not tag mail as processed: {exc}")


def main() -> None:
    if not KEYWORDS:
        sys.exit(
            "WATCH_KEYWORDS is not set. Example:\n"
            '  set "WATCH_KEYWORDS=trial balance ready,TB refresh done"'
        )
    inbox = connect_outlook()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"Watching Outlook Inbox every {POLL_SECONDS}s for keywords: "
        f"{KEYWORDS}" + (f" from senders matching {FROM_FILTER!r}" if FROM_FILTER else "")
    )
    while True:
        try:
            # Unread mails only, newest first; keyword matching happens here
            # in Python (Outlook's Restrict cannot search bodies reliably).
            items = inbox.Items.Restrict("[Unread] = True")
            items.Sort("[ReceivedTime]", True)
            for mail in list(items):
                if mail_matches(mail) and run_export(mail.Subject or ""):
                    mark_processed(mail)
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as exc:
            # Outlook restarting, COM hiccup, etc. — log and keep watching.
            print(f"Poll error (will retry): {exc}")
            inbox = connect_outlook()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
