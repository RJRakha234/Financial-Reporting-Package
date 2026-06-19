"""Pluggable send backends. The default talks SMTP; a dry-run backend is
provided for testing and ``--dry-run`` previews."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from .config import SmtpConfig


class Sender(Protocol):
    """Anything that can deliver an :class:`EmailMessage` to recipients."""

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:  # noqa: D401
        ...


class SmtpSender:
    """Deliver mail through an SMTP server (STARTTLS, SSL, or plain)."""

    def __init__(self, config: SmtpConfig):
        self.config = config

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        cfg = self.config
        password = cfg.password()
        if cfg.security == "ssl":
            context = ssl.create_default_context()
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=cfg.timeout, context=context
            )
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)

        with client:
            client.ehlo()
            if cfg.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(cfg.username, password)
            client.send_message(
                msg, from_addr=cfg.sender_address(), to_addrs=recipients
            )


class DryRunSender:
    """A backend that records what *would* be sent without contacting a server."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def send(self, msg: EmailMessage, recipients: list[str]) -> None:
        self.sent.append((msg["Subject"], list(recipients)))
