"""Detect replies ("reverts") to sent mail via IMAP.

For each tracked send still awaiting a reply, the inbox is searched for a
message that references the original ``Message-ID`` (via the ``In-Reply-To`` or
``References`` headers — the reliable, threading-safe signal). When one is
found the send is marked ``replied`` together with who it came from.
"""

from __future__ import annotations

import email
import imaplib
import logging
import ssl
from dataclasses import dataclass
from email.utils import parseaddr

from .config import ImapConfig
from .tracker import Tracker

log = logging.getLogger("mailflow")


@dataclass
class ReplyCheckResult:
    checked: int
    newly_replied: int


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapReplyChecker:
    """Connect to an IMAP mailbox and reconcile replies against the tracker."""

    def __init__(self, config: ImapConfig):
        self.config = config

    def _connect(self) -> imaplib.IMAP4:
        cfg = self.config
        if cfg.security == "ssl":
            context = ssl.create_default_context()
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                cfg.host, cfg.port, ssl_context=context, timeout=cfg.timeout
            )
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=cfg.timeout)
            if cfg.security == "starttls":
                conn.starttls(ssl.create_default_context())
        conn.login(cfg.username, cfg.password())
        conn.select(cfg.mailbox, readonly=True)
        return conn

    def check(self, tracker: Tracker, window_days: int | None = None) -> ReplyCheckResult:
        pending = tracker.pending_reply_sends(window_days)
        if not pending:
            return ReplyCheckResult(checked=0, newly_replied=0)

        conn = self._connect()
        newly = 0
        try:
            for record in pending:
                msg_id = record.message_id or ""
                reply_from = self._find_reply(conn, msg_id)
                tracker.touch_checked(record.id)
                if reply_from is not None:
                    if tracker.mark_replied(record.id, reply_from=reply_from):
                        newly += 1
                        log.info(
                            "revert received for send id=%s from %s",
                            record.id,
                            reply_from,
                        )
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return ReplyCheckResult(checked=len(pending), newly_replied=newly)

    def _find_reply(self, conn: imaplib.IMAP4, message_id: str) -> str | None:
        """Return the From address of a reply to ``message_id``, or None."""
        if not message_id:
            return None
        quoted = _imap_quote(message_id)
        for header in ("IN-REPLY-TO", "REFERENCES"):
            try:
                typ, data = conn.search(None, "HEADER", header, quoted)
            except imaplib.IMAP4.error as exc:
                log.debug("IMAP search failed (%s): %s", header, exc)
                continue
            if typ != "OK" or not data or not data[0]:
                continue
            uids = data[0].split()
            if not uids:
                continue
            return self._sender_of(conn, uids[-1])
        return None

    def _sender_of(self, conn: imaplib.IMAP4, uid: bytes) -> str:
        typ, data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if typ == "OK" and data and isinstance(data[0], tuple):
            parsed = email.message_from_bytes(data[0][1])
            name, addr = parseaddr(parsed.get("From", ""))
            return addr or name or "unknown"
        return "unknown"
