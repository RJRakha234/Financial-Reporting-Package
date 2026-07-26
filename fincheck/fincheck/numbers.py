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


def _normalise_separators(s: str) -> str:
    """Rewrite a figure's grouping so the decimal separator is a dot.

    Conventions collide: ``1.234.567,89`` and ``1,234,567.89`` are the same
    amount. Stripping commas and hoping, as this used to, turned the first into
    ``456.78912`` -- a wrong number that still looks like a number, which is the
    one failure mode a reconciliation must never have.

    Only the unambiguous cases are rewritten, so nothing that parsed correctly
    before changes meaning:

    * both separators present -- whichever comes last is the decimal one;
    * one separator repeated -- it must be grouping, not a decimal point;
    * space-grouped digits with a single comma -- space grouping never pairs
      with comma-as-thousands, so the comma is decimal.

    A single separator on its own stays ambiguous (``1.234`` is 1.234 in one
    convention and 1234 in another) and is left to the caller's default.
    """
    dot, comma = s.rfind("."), s.rfind(",")
    if dot != -1 and comma != -1:
        if comma > dot:
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")
    if s.count(",") > 1:
        return s.replace(",", "")
    if s.count(".") > 1:
        return s.replace(".", "")
    if s.count(",") == 1 and re.search(r"\d[\u00a0\u202f ]\d", s):
        return s.replace(",", ".")
    return s


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

    s = _normalise_separators(s)

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
