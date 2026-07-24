"""Mail backends and the factory that picks one from config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import MailClient, Message

if TYPE_CHECKING:
    from ..config import MailConfig

__all__ = ["MailClient", "Message", "build_mail_client"]


def build_mail_client(config: "MailConfig") -> MailClient:
    """Return a mail client for the configured backend."""
    backend = (config.backend or "graph").lower()
    if backend == "graph":
        from .graph import GraphMailClient

        return GraphMailClient(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            user=config.user,
        )
    if backend == "imap":
        from .imap import ImapMailClient

        return ImapMailClient(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
        )
    raise ValueError(
        f"Unknown mail backend {config.backend!r}; use 'graph' or 'imap'."
    )
