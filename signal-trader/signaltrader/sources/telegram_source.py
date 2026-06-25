"""Telegram signal source built on Telethon.

Logs in as a user (or bot) and forwards every message from the configured chats
to the handler. Telethon is imported lazily so the package works without it.

Credentials come from https://my.telegram.org (api_id + api_hash). On first run
Telethon will prompt for your phone number and a login code to create a reusable
session file.
"""

from __future__ import annotations

import logging

from ..config import TelegramConfig
from .base import MessageHandler, SignalSource

log = logging.getLogger("signaltrader.telegram")


class TelegramSource(SignalSource):
    def __init__(self, config: TelegramConfig) -> None:
        if not config.api_id or not config.api_hash:
            raise ValueError(
                "Telegram needs api_id and api_hash. Get them from "
                "https://my.telegram.org and set TELEGRAM_API_ID / TELEGRAM_API_HASH."
            )
        self.config = config

    def run(self, handler: MessageHandler) -> None:  # pragma: no cover - network
        try:
            from telethon import TelegramClient, events  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "TelegramSource requires 'telethon'. "
                "Install it with: pip install telethon"
            ) from exc

        client = TelegramClient(
            self.config.session, self.config.api_id, self.config.api_hash
        )

        # Restrict to specific chats when configured, else listen to everything.
        chats = list(self.config.chats) or None

        @client.on(events.NewMessage(chats=chats))
        async def _on_message(event):  # noqa: ANN001 - telethon event
            text = event.message.message or ""
            if not text.strip():
                return
            log.debug("incoming message from %s", event.chat_id)
            try:
                handler(text)
            except Exception:  # never let one bad message kill the listener
                log.exception("handler failed for message")

        log.info("connecting to Telegram (chats=%s)...", chats or "ALL")
        client.start()
        log.info("listening for signals. Press Ctrl+C to stop.")
        client.run_until_disconnected()
