"""Numeric token detection and canonicalisation.

Handles the formats found in Indian + SEC financial documents:

* western grouping        1,234,567.89
* Indian (lakh/crore)     12,34,567
* parenthesised negative  (2,318)
* currency prefixes       ₹ 4,204   $12
* percentages             21.1%
* nil dashes are ignored (they carry no value to compare)
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# A token is either a parenthesised amount or a bare amount, optionally with a
# currency prefix and/or a trailing %.  The number itself must start and end
# with a digit so trailing commas/periods of the sentence are not swallowed.
_AMOUNT = r"\d(?:[\d,]*\d)?(?:\.\d+)?"
TOKEN_RE = re.compile(
    rf"\(\s?(?:[₹$]\s?)?{_AMOUNT}\s?\)%?"  # (1,234) possibly (₹ 1,234)
    rf"|[-−]?(?:[₹$]\s?)?{_AMOUNT}%?"      # 1,234  -1,234  ₹1,234  21.1%
)


def canonical_value(token: str) -> Decimal | None:
    """Return the Decimal value of a token, negative when parenthesised.

    Returns None when the token is not actually a parseable amount.
    """
    t = token.strip()
    negative = (
        (t.startswith("(") and t.endswith((")", ")%")))
        or t.startswith(("-", "−"))
    )
    t = t.strip("()%").lstrip("-−")
    t = t.replace("₹", "").replace("$", "").replace(",", "").strip()
    if not t:
        return None
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return -value if negative else value


def token_attrs(token: str) -> tuple[int, str, bool]:
    """Return ``(sign, currency, is_percent)`` of a figure token.

    * ``sign``   — -1 when parenthesised or lead by a minus, else +1;
    * ``currency`` — "₹", "$" or "" ;
    * ``is_percent`` — the token carries a trailing ``%``.

    These attributes are dropped by :func:`canonical_key` (so magnitudes
    still match across formatting) but are compared *within a row* so a lost
    negative, a swapped currency, or a dropped ``%`` cannot pass unseen.
    """
    t = token.strip()
    negative = (
        (t.startswith("(") and t.endswith((")", ")%")))
        or t.startswith(("-", "−"))
    )
    currency = "₹" if "₹" in t else ("$" if "$" in t else "")
    is_percent = t.rstrip().endswith("%")
    return (-1 if negative else 1, currency, is_percent)


def canonical_key(value: Decimal) -> str:
    """A comparison key for a value, ignoring sign and trailing zeros.

    Sign is ignored because the same figure may be shown as ``(240)`` in one
    rendering and ``-240`` or ``240`` (under a "deductions" heading) in the
    other.  ``25.30`` and ``25.3`` compare equal.
    """
    v = abs(value).normalize()
    if v == v.to_integral_value():
        v = v.quantize(Decimal(1))
    return str(v)


def valid_grouping(token: str) -> bool:
    """Whether the comma grouping of *token* is a real thousands grouping.

    Accepts western (1,234,567) and Indian lakh/crore (12,34,567) styles.
    ``30,2025`` (a date rendered without a space: "June 30,2025") is not a
    grouped number and must be treated as two separate values.
    """
    digits = token.strip("()% ").replace("₹", "").replace("$", "").strip()
    int_part = digits.split(".", 1)[0]
    if "," not in int_part:
        return True
    groups = int_part.split(",")
    if any(not g for g in groups):
        return False
    western = len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])
    # Indian lakh/crore style; the leading group may run to 3 digits in the
    # hybrid form used for share counts (e.g. 415,42,72,628).
    indian = (
        len(groups[-1]) == 3
        and len(groups[0]) <= 3
        and all(len(g) == 2 for g in groups[1:-1])
    )
    return western or indian


def iter_tokens(text: str):
    """Yield ``(match_start, match_end, token, key)`` for amounts in *text*."""
    for m in TOKEN_RE.finditer(text):
        token = m.group(0)
        start = m.start()
        # A leading minus glued to a preceding letter/digit is a hyphen /
        # separator inside a code ("W-100018", "2019-20"), not a negative
        # sign — drop it so the figure is read as positive.
        if token[:1] in "-−" and start > 0 and text[start - 1].isalnum():
            token = token[1:]
            start += 1
        if not valid_grouping(token):
            # Not a grouped amount (e.g. "30,2025" in a date): treat each
            # digit run as its own value.
            for sub in re.finditer(r"\d+(?:\.\d+)?", token):
                value = canonical_value(sub.group(0))
                if value is not None:
                    yield (
                        start + sub.start(),
                        start + sub.end(),
                        sub.group(0),
                        canonical_key(value),
                    )
            continue
        value = canonical_value(token)
        if value is None:
            continue
        yield start, m.end(), token, canonical_key(value)


def is_significant(key: str, token: str) -> bool:
    """Whether a PDF figure is worth reporting if absent from the HTML.

    Filters out the noise that legitimately differs between renderings:
    small integers (note numbers, list indices), year-like values, and page
    numbers — anything without a comma, a decimal part, or 3+ digits.
    """
    try:
        value = Decimal(key)
    except InvalidOperation:
        return False
    if "," in token or "." in key:
        return True
    if value == value.to_integral_value() and 1900 <= value <= 2100:
        return False  # looks like a year
    return abs(value) >= 100
