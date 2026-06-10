"""Parsing of numbers as they appear in financial statements.

Financial figures show up in many shapes: ``1,234``, ``(1,234)`` for negatives,
``1 234`` with space separators, a bare ``-`` / em-dash meaning *nil*, currency
symbols or ISO codes glued on, and trailing percent signs. This module turns all
of those into plain floats (or ``None`` when a token is not a number at all).
"""

import re

CURRENCY_SYMBOLS = "₹$€£¥"
# en-dash, em-dash, unicode minus, figure dash
DASHES = "–—−‒"
_NIL_TOKENS = {"-", "--", "—", "–", "−", "nil", "n/a", "na"}

# ISO codes / common prefixes that hug a figure in disclosures.
_CODE_RE = re.compile(r"(?i)\b(?:inr|usd|eur|gbp|aed|jpy|cny|sgd|rs|rmb)\.?")
_PURE_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _strip_decorations(s: str) -> str:
    s = _CODE_RE.sub("", s)
    s = s.strip(CURRENCY_SYMBOLS + DASHES + " \t")
    return s.strip()


def parse_number(raw) -> float | None:
    """Return the numeric value of a financial token, or ``None``.

    A lone dash (nil) parses as ``0.0``. Percentages return ``None`` so that
    percentage columns are not pulled into money footing checks.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.lower() in _NIL_TOKENS:
        return 0.0
    if "%" in s:
        return None

    negative = False
    # Accountants wrap negatives in parentheses.
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # A minus / dash anywhere before the first digit marks a negative, even
    # when a currency symbol or code sits in front of it (e.g. "INR -500").
    first_digit = next((i for i, ch in enumerate(s) if ch.isdigit()), None)
    if first_digit is not None and any(
        ch == "-" or ch in DASHES for ch in s[:first_digit]
    ):
        negative = True

    s = _strip_decorations(s)
    # Drop a leading sign now that negativity has been recorded.
    s = s.lstrip("+-" + DASHES + " ")

    # Thousands separators: commas, or spaces sitting between digit groups.
    compact = s.replace(",", "")
    compact = re.sub(r"(?<=\d)[  ](?=\d)", "", compact)

    if not _PURE_NUMBER_RE.match(compact):
        return None

    value = float(compact)
    return -value if negative else value


_NUMBERISH_RE = re.compile(
    r"^[\(\)\-+,.\d" + re.escape(CURRENCY_SYMBOLS + DASHES) + r"]+$"
)


def is_numberish(token: str) -> bool:
    """True if a token looks like (part of) a number worth merging/parsing.

    Used to glue space-separated figures like ``1 234 567`` back together
    before parsing. Requires at least one digit so stray punctuation is ignored.
    """
    token = token.strip()
    if not token:
        return False
    if not any(ch.isdigit() for ch in token):
        return False
    return bool(_NUMBERISH_RE.match(token))


def format_number(value: float) -> str:
    """Human-friendly rendering used in reports and PDF annotations."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"
