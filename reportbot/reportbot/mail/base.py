"""Shared mail types: a backend-agnostic :class:`Message` and the
:class:`MailClient` protocol every backend implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class Message:
    """A single email, normalised across backends.

    Only the fields the bot actually needs are kept: enough to match trigger
    rules and to extract a download link from the body.
    """

    id: str
    sender: str
    subject: str
    body: str = ""
    received: datetime | None = None
    links: list[str] = field(default_factory=list)


@runtime_checkable
class MailClient(Protocol):
    """Anything that can list recent messages from a folder."""

    def fetch_recent(self, folder: str, lookback_days: int) -> list[Message]:
        """Return messages received within the lookback window, newest first."""
        ...
