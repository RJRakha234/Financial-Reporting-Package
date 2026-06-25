"""Parse and classify the numbers that appear in financial statements.

Two jobs:

* :func:`parse_number` turns a token as printed (``1,234``, ``(1,234)`` for a
  negative, ``1 234`` with a space separator, a bare ``-`` for nil, a currency
  symbol/code glued on) into a plain float, or ``None`` when it is not a number.
* :func:`is_numberish` is the cheap pre-test for "could this token be a number".

Numbers are compared by value, so formatting (commas, leading zeros, currency
symbols) never matters. Only the standard library is used; no network calls.
"""

import re

CURRENCY_SYMBOLS = "₹$€£¥"
DASHES = "–—−‒"  # en, em, minus, figure dash
_NIL_TOKENS = {"-", "--", "—", "–", "−", "nil", "n/a", "na"}

_CODE_RE = re.compile(r"(?i)\b(?:inr|usd|eur|gbp|aed|jpy|cny|sgd|rs|rmb)\.?")
_PURE_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
_NUMBERISH_RE = re.compile(
    r"^[\(\)\-+,.\d" + re.escape(CURRENCY_SYMBOLS + DASHES) + r"]+$"
)


def _strip_decorations(s: str) -> str:
    s = _CODE_RE.sub("", s)
    s = s.strip(CURRENCY_SYMBOLS + DASHES + " \t")
    return s.strip()


def parse_number(raw) -> float | None:
    """Return the numeric value of a financial token, or ``None``."""
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
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    first_digit = next((i for i, ch in enumerate(s) if ch.isdigit()), None)
    if first_digit is not None and any(
        ch == "-" or ch in DASHES for ch in s[:first_digit]
    ):
        negative = True

    s = _strip_decorations(s)
    s = s.lstrip("+-" + DASHES + " ")

    compact = s.replace(",", "")
    compact = re.sub(r"(?<=\d)[  ](?=\d)", "", compact)
    if not _PURE_NUMBER_RE.match(compact):
        return None
    value = float(compact)
    return -value if negative else value


def is_numberish(token: str) -> bool:
    """True if a token looks like (part of) a number (has a digit)."""
    token = token.strip()
    if not token or not any(ch.isdigit() for ch in token):
        return False
    return bool(_NUMBERISH_RE.match(token))


def format_number(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"
