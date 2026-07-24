"""Orchestration: poll the mailbox, match trigger rules, download reports.

The runner is deliberately small and injectable — you can hand it a fake mail
client and downloader in tests, or let it build the real ones from config.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .mail import build_mail_client
from .mail.base import MailClient, Message
from .portal import PortalDownloader
from .state import StateStore
from .triggers import TriggerRule, matching_rule

log = logging.getLogger("reportbot")


@dataclass
class DownloadEvent:
    """Record of one triggered download (returned so callers/tests can inspect)."""

    message: Message
    rule: TriggerRule
    files: list[Path]


class Runner:
    def __init__(
        self,
        config: Config,
        mail_client: MailClient | None = None,
        downloader: PortalDownloader | None = None,
        state: StateStore | None = None,
        download_fn: Callable[[Message, TriggerRule], list[Path]] | None = None,
    ):
        self.config = config
        # `is None`, not `or`: an empty StateStore is falsy (it defines
        # __len__), so `state or ...` would discard an injected empty store.
        self.state = state if state is not None else StateStore(config.state_file)
        self._mail_client = mail_client
        self._downloader = downloader
        # Lets tests bypass the browser entirely.
        self._download_fn = download_fn

    @property
    def mail_client(self) -> MailClient:
        if self._mail_client is None:
            self._mail_client = build_mail_client(self.config.mail)
        return self._mail_client

    @property
    def downloader(self) -> PortalDownloader:
        if self._downloader is None:
            self._downloader = PortalDownloader(self.config.portal)
        return self._downloader

    def _download(self, message: Message, rule: TriggerRule) -> list[Path]:
        if self._download_fn is not None:
            return self._download_fn(message, rule)
        dest = Path(self.config.download_dir) / _safe_name(rule.name)
        return self.downloader.download(dest, message)

    def run_once(self) -> list[DownloadEvent]:
        """Check the mailbox once and download reports for any new matches."""
        events: list[DownloadEvent] = []
        messages = self.mail_client.fetch_recent(
            self.config.mail.folder, self.config.mail.lookback_days
        )
        log.info("Fetched %d recent message(s)", len(messages))

        # Oldest first so downloads happen in the order mail arrived.
        for message in sorted(
            messages,
            key=lambda m: m.received.timestamp() if m.received else 0,
        ):
            if not message.id or self.state.is_processed(message.id):
                continue
            rule = matching_rule(self.config.triggers, message)
            if rule is None:
                # Not a triggering email — mark it so we don't re-scan forever.
                self.state.mark(message.id)
                continue
            log.info(
                "Match on rule %r: %s", rule.name, message.subject or "(no subject)"
            )
            try:
                files = self._download(message, rule)
            except Exception:  # noqa: BLE001 - one bad download must not stop the loop
                log.exception(
                    "Download failed for message %s; will retry next poll",
                    message.id,
                )
                continue
            self.state.mark(message.id)
            log.info(
                "Downloaded %d file(s) for %r: %s",
                len(files),
                rule.name,
                ", ".join(str(f) for f in files) or "(none)",
            )
            events.append(DownloadEvent(message=message, rule=rule, files=files))
        return events

    def run_forever(self) -> None:
        """Poll on a loop until interrupted."""
        interval = max(self.config.poll_interval_seconds, 5)
        log.info("Starting poll loop every %ds (Ctrl-C to stop)", interval)
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the loop alive across errors
                log.exception("Poll failed; continuing")
            time.sleep(interval)


def _safe_name(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in name)
    return cleaned.strip() or "trigger"
