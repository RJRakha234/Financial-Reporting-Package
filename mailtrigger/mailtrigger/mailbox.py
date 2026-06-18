"""IMAP mailbox watcher.

Connects to Outlook (or any IMAP server), fetches unseen messages, and yields
them as simple ``Email`` objects.  Kept deliberately small so a Microsoft Graph
backend could be dropped in later behind the same ``Email`` shape.
"""

from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from typing import Iterator


@dataclass
class Attachment:
    filename: str
    content: bytes


@dataclass
class Email:
    uid: str
    from_addr: str
    subject: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: Message) -> str:
    """Return the plain-text body (falling back to any text part)."""
    if not msg.is_multipart():
        return _payload_text(msg)

    text = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp:
            continue
        if ctype == "text/plain":
            return _payload_text(part)
        if ctype == "text/html" and not text:
            text = _payload_text(part)
    return text


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_attachments(msg: Message) -> list[Attachment]:
    out: list[Attachment] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" not in disp:
            continue
        name = _decode(part.get_filename()) or "attachment"
        data = part.get_payload(decode=True) or b""
        out.append(Attachment(filename=name, content=data))
    return out


class Mailbox:
    """Context manager around an IMAP connection."""

    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 993))
        self.username = cfg["username"]
        self.password = cfg["password"]
        self.folder = cfg.get("folder", "INBOX")
        self.mark_seen = bool(cfg.get("mark_seen", True))
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Mailbox":
        self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        self._conn.login(self.username, self.password)
        self._conn.select(self.folder)
        return self

    def __exit__(self, *exc) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def fetch_unseen(self) -> Iterator[Email]:
        assert self._conn is not None, "use Mailbox as a context manager"
        typ, data = self._conn.search(None, "UNSEEN")
        if typ != "OK":
            return
        for uid in data[0].split():
            # Peek so reading does not implicitly mark the message seen; we
            # decide that ourselves once it has been successfully processed.
            typ, raw = self._conn.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not raw or raw[0] is None:
                continue
            msg = email.message_from_bytes(raw[0][1])
            yield Email(
                uid=uid.decode(),
                from_addr=_decode(msg.get("From")),
                subject=_decode(msg.get("Subject")),
                body=_extract_body(msg),
                attachments=_extract_attachments(msg),
            )

    def mark_processed(self, mail: Email) -> None:
        if self._conn is not None and self.mark_seen:
            self._conn.store(mail.uid.encode(), "+FLAGS", "\\Seen")
