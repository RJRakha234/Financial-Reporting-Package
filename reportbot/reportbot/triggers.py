"""Match incoming emails against trigger rules.

A rule fires only when *every* condition it specifies is satisfied (AND across
condition types). Within a single list condition (e.g. ``from_contains``) any
one match is enough (OR within the list). All text matching is
case-insensitive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mail.base import Message


@dataclass
class TriggerRule:
    name: str = "trigger"
    # Substrings that must appear in the sender address (any one matches).
    from_contains: list[str] = field(default_factory=list)
    # Substrings that must appear in the subject (any one matches).
    subject_contains: list[str] = field(default_factory=list)
    # Regex the subject must match (anywhere).
    subject_regex: str = ""
    # Substrings that must appear in the body (any one matches).
    body_contains: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TriggerRule":
        def as_list(v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v]

        return cls(
            name=str(data.get("name", "trigger")),
            from_contains=as_list(data.get("from_contains")),
            subject_contains=as_list(data.get("subject_contains")),
            subject_regex=str(data.get("subject_regex", "") or ""),
            body_contains=as_list(data.get("body_contains")),
        )

    def matches(self, message: "Message") -> bool:
        sender = (message.sender or "").lower()
        subject = (message.subject or "").lower()
        body = (message.body or "").lower()

        if self.from_contains and not any(
            s.lower() in sender for s in self.from_contains
        ):
            return False
        if self.subject_contains and not any(
            s.lower() in subject for s in self.subject_contains
        ):
            return False
        if self.subject_regex and not re.search(
            self.subject_regex, message.subject or "", re.IGNORECASE
        ):
            return False
        if self.body_contains and not any(
            s.lower() in body for s in self.body_contains
        ):
            return False

        # A rule with no conditions never fires — guards against a blank rule
        # matching every message.
        return bool(
            self.from_contains
            or self.subject_contains
            or self.subject_regex
            or self.body_contains
        )


def matching_rule(
    rules: list[TriggerRule], message: "Message"
) -> TriggerRule | None:
    """Return the first rule that matches the message, or ``None``."""
    for rule in rules:
        if rule.matches(message):
            return rule
    return None
