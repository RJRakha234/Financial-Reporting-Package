"""Detect replies ("reverts") to sent mail via IMAP.

A reply is identified by matching the original outgoing ``Message-ID`` against
the ``In-Reply-To`` / ``References`` headers of inbox messages — the reliable,
threading-safe signal. :class:`ImapReplyChecker` is used in two ways:

* ``check(tracker, ...)`` reconciles replies for the SQLite tracker;
* ``connect()`` + ``find_reply(conn, message_id)`` return a :class:`ReplyHit`
  (sender **and** raw bytes) so callers such as the Excel front-end can archive
  the received message.
"""

from __future__ import annotations

import email
import imaplib
import logging
import ssl
from dataclasses import dataclass
from email.utils import parsedate_to_datetime, parseaddr

from .config import ImapConfig
from .tracker import Tracker

log = logging.getLogger("mailflow")


@dataclass
class ReplyHit:
    from_addr: str
    raw: bytes
    received_at: str | None = None


@dataclass
class ReplyCheckResult:
    checked: int
    newly_replied: int


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapReplyChecker:
    """Connect to an IMAP mailbox and find replies to sent messages."""

    def __init__(self, config: ImapConfig):
        self.config = config

    def connect(self) -> imaplib.IMAP4:
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

    def find_reply(self, conn: imaplib.IMAP4, message_id: str) -> ReplyHit | None:
        """Return the most recent reply to ``message_id``, or ``None``."""
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
            if uids:
                return self._fetch_hit(conn, uids[-1])
        return None

    def check(self, tracker: Tracker, window_days: int | None = None) -> ReplyCheckResult:
        pending = tracker.pending_reply_sends(window_days)
        if not pending:
            return ReplyCheckResult(checked=0, newly_replied=0)

        conn = self.connect()
        newly = 0
        try:
            for record in pending:
                hit = self.find_reply(conn, record.message_id or "")
                tracker.touch_checked(record.id)
                if hit is not None and tracker.mark_replied(
                    record.id, reply_from=hit.from_addr, when=hit.received_at
                ):
                    newly += 1
                    log.info(
                        "revert received for send id=%s from %s",
                        record.id,
                        hit.from_addr,
                    )
        finally:
            self._logout(conn)
        return ReplyCheckResult(checked=len(pending), newly_replied=newly)

    # -- helpers -----------------------------------------------------------
    def _fetch_hit(self, conn: imaplib.IMAP4, uid: bytes) -> ReplyHit:
        typ, data = conn.fetch(uid, "(RFC822)")
        if typ == "OK" and data and isinstance(data[0], tuple):
            raw = data[0][1]
            parsed = email.message_from_bytes(raw)
            _, addr = parseaddr(parsed.get("From", ""))
            received_at = None
            try:
                if parsed.get("Date"):
                    received_at = parsedate_to_datetime(parsed["Date"]).isoformat(
                        timespec="seconds"
                    )
            except (TypeError, ValueError):
                received_at = None
            return ReplyHit(from_addr=addr or "unknown", raw=raw, received_at=received_at)
        return ReplyHit(from_addr="unknown", raw=b"")

    @staticmethod
    def _logout(conn: imaplib.IMAP4) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
