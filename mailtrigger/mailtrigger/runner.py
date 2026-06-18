"""Orchestrate the pipeline: watch inbox, match, fetch, process, deliver."""

from __future__ import annotations

import logging
import time

from . import fetcher, output, processor
from .mailbox import Email, Mailbox
from .matcher import matches

log = logging.getLogger("mailtrigger")


def handle_email(mail: Email, cfg: dict) -> str | None:
    """Run the report pipeline for a single matched email. Returns saved path."""
    fetched = fetcher.fetch(cfg["report"], mail)
    log.info("fetched %d bytes from %s", len(fetched.content), fetched.url)

    result = processor.process(fetched.content, fetched.content_type, cfg.get("process", {}))
    saved = output.save_to_disk(result, cfg["output"])
    log.info("saved report to %s", saved)

    output.email_result(result, saved, cfg["output"], mail)
    return str(saved)


def run_once(cfg: dict) -> int:
    """Check the inbox a single time. Returns the number of reports run."""
    count = 0
    with Mailbox(cfg["mailbox"]) as box:
        for mail in box.fetch_unseen():
            if not matches(mail, cfg["trigger"]):
                continue
            log.info("trigger matched: from=%r subject=%r", mail.from_addr, mail.subject)
            try:
                handle_email(mail, cfg)
                box.mark_processed(mail)
                count += 1
            except Exception:
                log.exception("failed to process message %s; leaving it unread", mail.uid)
    return count


def run_forever(cfg: dict) -> None:
    poll = int(cfg["mailbox"].get("poll_seconds", 60))
    log.info("watching %s every %ds", cfg["mailbox"].get("username"), poll)
    while True:
        try:
            run_once(cfg)
        except Exception:
            log.exception("poll cycle failed; will retry")
        time.sleep(poll)
