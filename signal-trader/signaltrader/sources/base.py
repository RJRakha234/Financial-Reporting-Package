"""Signal-source interface.

A source delivers raw message text to a callback. The Telegram source is the
concrete implementation; the design leaves room for a WhatsApp source (via the
WhatsApp Business Cloud API) behind the same contract.
"""

from __future__ import annotations

import abc
from typing import Callable

# Called with the raw text of each incoming message.
MessageHandler = Callable[[str], None]


class SignalSource(abc.ABC):
    @abc.abstractmethod
    def run(self, handler: MessageHandler) -> None:  # pragma: no cover - interface
        """Block, delivering each incoming message to ``handler``."""
        ...
