"""Remember which messages have already been handled, so a report is downloaded
once even though the same email keeps showing up on every poll."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class StateStore:
    """A tiny JSON-backed set of processed message ids.

    Kept deliberately simple: the file maps ``message_id -> ISO timestamp`` of
    when it was handled. Losing the file just means the bot may re-download
    reports it has already seen — it never loses mail.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._processed: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._processed = json.loads(
                    self.path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                self._processed = {}

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed

    def mark(self, message_id: str) -> None:
        self._processed[message_id] = datetime.now(timezone.utc).isoformat()
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._processed, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._processed)
