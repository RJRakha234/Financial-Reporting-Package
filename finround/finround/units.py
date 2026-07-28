"""Exact arithmetic between printed figures and integer rounding units.

Rounding is decided in *units of the rounding step*: a figure of 1,234,567
presented in thousands to one decimal is 1234.567 units of 0.1. Everything is
carried as :class:`~fractions.Fraction` so that a value like ``0.1`` is exactly
one tenth and never 0.1000000000000000055 — binary floats would otherwise make
"does this total foot exactly?" unanswerable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

CURRENCY = "₹$€£¥"
DASHES = "–—−‒"
_NIL = {"", "-", "--", "—", "–", "−", "nil", "n/a", "na"}
_CODE_RE = re.compile(r"(?i)\b(?:inr|usd|eur|gbp|aed|jpy|cny|sgd|rs|rmb)\.?")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_value(raw) -> Fraction | None:
    """Parse a figure as it appears in a statement; ``None`` if it is not one.

    Handles thousands separators, parenthesised negatives, currency symbols
    and codes, unicode minus signs, and a bare dash meaning nil.
    """
    if raw is None:
        return None
    if isinstance(raw, Fraction):
        return raw
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Fraction(raw)
    if isinstance(raw, float):
        return Fraction(str(raw))
    if isinstance(raw, Decimal):
        return Fraction(raw)

    text = str(raw).strip()
    if text.lower() in _NIL:
        return Fraction(0) if text else None
    if "%" in text:
        text = text.replace("%", "").strip()

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    first_digit = next((i for i, ch in enumerate(text) if ch.isdigit()), None)
    if first_digit is not None and any(
        ch == "-" or ch in DASHES for ch in text[:first_digit]
    ):
        negative = True

    text = _CODE_RE.sub("", text).strip(CURRENCY + DASHES + " \t")
    text = text.lstrip("+-" + DASHES + " ")
    compact = re.sub(r"(?<=\d)[  ](?=\d)", "", text.replace(",", ""))
    if not _NUMBER_RE.match(compact):
        return None

    value = Fraction(compact)
    return -value if negative else value


def to_fraction(raw) -> Fraction:
    """Like :func:`parse_value` but insists on a number."""
    value = parse_value(raw)
    if value is None:
        raise ValueError(f"not a number: {raw!r}")
    return value


def floor_div(a: Fraction) -> int:
    """Floor of a fraction (towards minus infinity)."""
    return a.numerator // a.denominator


def ceil_div(a: Fraction) -> int:
    return -((-a.numerator) // a.denominator)


def nearest(a: Fraction) -> int:
    """Round to the nearest integer, halves away from zero (the accounting rule)."""
    low = floor_div(a)
    rest = a - low
    if rest > Fraction(1, 2):
        return low + 1
    if rest < Fraction(1, 2):
        return low
    return low + 1 if a > 0 else low


@dataclass(frozen=True)
class RoundingSpec:
    """How figures are to be presented.

    ``scale`` divides the input (1,000 to show thousands, 10,000,000 for
    crores) and ``step`` is the granularity of the *presented* number: 1 for
    whole units, ``Fraction(1, 10)`` for one decimal, 5 or 25 for "nearest 5".
    """

    scale: Fraction = Fraction(1)
    step: Fraction = Fraction(1)
    places: int = 0

    @classmethod
    def make(cls, scale=1, decimals: int | None = None, step=None) -> "RoundingSpec":
        scale = to_fraction(scale)
        if scale == 0:
            raise ValueError("scale must not be zero")
        if step is None:
            step = Fraction(1, 10**decimals) if decimals else Fraction(1)
        else:
            step = to_fraction(step)
        if step <= 0:
            raise ValueError("step must be positive")
        places = decimals if decimals is not None else _places_for(step)
        return cls(scale=scale, step=step, places=places)

    def to_units(self, value) -> Fraction:
        return to_fraction(value) / self.scale / self.step

    def from_units(self, units: int) -> Fraction:
        return Fraction(units) * self.step

    def format(self, value: Fraction, thousands: bool = True) -> str:
        """Render a rounded figure, negatives in parentheses as accountants do."""
        quantum = Fraction(10) ** self.places
        scaled = nearest(value * quantum)
        sign = "-" if scaled < 0 else ""
        digits = str(abs(scaled)).rjust(self.places + 1, "0")
        whole, frac = digits[: len(digits) - self.places], digits[len(digits) - self.places :]
        if thousands:
            whole = f"{int(whole):,}"
        return sign + (f"{whole}.{frac}" if self.places else whole)

    def describe(self) -> str:
        parts = [f"nearest {self.format(self.step, thousands=False)}"]
        if self.scale != 1:
            parts.append(f"values divided by {int(self.scale):,}")
        return ", ".join(parts)


def _places_for(step: Fraction) -> int:
    for places in range(10):
        if (step * 10**places).denominator == 1:
            return places
    return 6
