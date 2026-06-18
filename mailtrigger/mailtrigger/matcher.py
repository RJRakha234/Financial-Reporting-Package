"""Decide whether an incoming email is the one that should trigger a run."""

from __future__ import annotations

from .mailbox import Email


def matches(mail: Email, trigger: dict) -> bool:
    """Return True when every configured condition is satisfied.

    Empty/missing conditions are ignored.  All text checks are
    case-insensitive substring matches.
    """
    from_contains = (trigger.get("from_contains") or "").strip().lower()
    if from_contains and from_contains not in mail.from_addr.lower():
        return False

    subject_contains = (trigger.get("subject_contains") or "").strip().lower()
    if subject_contains and subject_contains not in mail.subject.lower():
        return False

    body_contains = (trigger.get("body_contains") or "").strip().lower()
    if body_contains and body_contains not in mail.body.lower():
        return False

    if trigger.get("require_attachment") and not mail.attachments:
        return False

    return True
