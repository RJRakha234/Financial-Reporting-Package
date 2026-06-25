"""Persist a checker's accept/ignore decisions across quarters.

A *decision ledger* is a small JSON file keyed by each difference's stable hash
(see :attr:`fincheck.notes.compare.Difference.hash`). When the tool re-runs next
quarter, a difference whose wording is unchanged keeps the same hash and so
keeps its prior decision (and stays out of the reviewer's way); a difference
whose wording has changed gets a new hash and re-surfaces as ``open``.

Statuses:

* ``open`` — not yet decided (the default for an unseen difference);
* ``accepted`` — a known/expected difference the checker has signed off;
* ``ignored`` — not relevant, suppress it.

The HTML report reads and writes the same JSON shape, so a reviewer can export
their decisions from the browser and feed them straight back in next time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

OPEN = "open"
ACCEPTED = "accepted"
IGNORED = "ignored"
_RESOLVED = {ACCEPTED, IGNORED}
_VALID = {OPEN, ACCEPTED, IGNORED}


@dataclass
class Ledger:
    """A mapping of difference hash -> decision record."""

    entries: dict[str, dict] = field(default_factory=dict)

    def status_of(self, diff_hash: str) -> str:
        entry = self.entries.get(diff_hash)
        status = entry.get("status") if entry else None
        return status if status in _VALID else OPEN

    def is_resolved(self, diff_hash: str) -> bool:
        return self.status_of(diff_hash) in _RESOLVED

    def set(self, diff_hash: str, status: str, note: str = "", comment: str = "") -> None:
        if status not in _VALID:
            raise ValueError(f"invalid status {status!r}; expected one of {sorted(_VALID)}")
        self.entries[diff_hash] = {"status": status, "note": note, "comment": comment}

    def to_dict(self) -> dict:
        return {"version": 1, "decisions": self.entries}

    def as_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def load_ledger(path: str | None) -> Ledger:
    """Load a ledger from ``path``; return an empty ledger if missing/empty."""
    if not path:
        return Ledger()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return Ledger()
    decisions = raw.get("decisions", raw) if isinstance(raw, dict) else {}
    entries = {
        k: v for k, v in decisions.items() if isinstance(v, dict) and "status" in v
    }
    return Ledger(entries=entries)


def save_ledger(ledger: Ledger, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ledger.as_json())


def summarize(differences, ledger: Ledger) -> dict[str, int]:
    """Count differences by their (ledger-resolved) status."""
    counts = {OPEN: 0, ACCEPTED: 0, IGNORED: 0}
    for d in differences:
        counts[ledger.status_of(d.hash)] += 1
    return counts
