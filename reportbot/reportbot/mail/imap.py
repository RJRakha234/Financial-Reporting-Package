"""IMAP mail backend — works with any Outlook/Exchange or generic mailbox that
allows IMAP access. Uses only the Python standard library.

For Outlook.com / Microsoft 365 you typically need an *app password* (basic
auth over IMAP) and IMAP enabled on the account.
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message as EmailMessage
from email.utils import parsedate_to_datetime

from .base import Message

_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: EmailMessage) -> str:
    """Best-effort plain-text body; falls back to stripped HTML."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(parts)


class ImapMailClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
    ):
        if not username or not password:
            raise ValueError(
                "IMAP backend needs a username and password "
                "(set them in config / environment)."
            )
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def fetch_recent(self, folder: str, lookback_days: int) -> list[Message]:
        since = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 0))
        criterion = since.strftime("%d-%b-%Y")

        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.username, self.password)
            conn.select(folder, readonly=True)
            typ, data = conn.search(None, "SINCE", criterion)
            if typ != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()
            messages: list[Message] = []
            for uid in uids:
                typ, raw = conn.fetch(uid, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                messages.append(self._to_message(uid.decode(), msg))
            messages.sort(
                key=lambda m: m.received or datetime.min.replace(
                    tzinfo=timezone.utc
                ),
                reverse=True,
            )
            return messages
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _to_message(self, uid: str, msg: EmailMessage) -> Message:
        body = _body_text(msg)
        received: datetime | None = None
        if msg.get("Date"):
            try:
                received = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                received = None
        # Prefer the stable Message-ID as the dedupe key; fall back to the UID.
        message_id = _decode(msg.get("Message-ID")) or f"uid:{uid}"
        return Message(
            id=message_id,
            sender=_decode(msg.get("From")),
            subject=_decode(msg.get("Subject")),
            body=body,
            received=received,
            links=_URL_RE.findall(body),
        )
