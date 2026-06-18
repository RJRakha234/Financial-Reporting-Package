"""Normalisation: reduce a line of text to its *language* by masking out the
things that legitimately differ between two currency versions of the same
financial statement -- the numbers, and (optionally) the currency symbols and
unit words (INR/USD, crores/millions, ...).

The goal: ``"Cash and cash equivalents   6,75,000  7,20,000"`` (INR, in crores)
and ``"Cash and cash equivalents   8,750  9,200"`` (USD, in millions) both
collapse to ``"cash and cash equivalents #"`` so they compare as *equal*.
"""

import re

#: Currency symbols that appear glued to or beside figures.
CURRENCY_SYMBOLS = "$₹€£¥₩₪﷼"

#: Currency codes / scale words that differ across currency versions.
_CURRENCY_WORD_RE = re.compile(
    r"\b(?:INR|USD|EUR|GBP|JPY|CNY|AED|SGD|CHF|CAD|AUD|"
    r"RS|RUPEES?|DOLLARS?|EUROS?|POUNDS?|YEN|YUAN|DIRHAMS?|"
    r"CRORES?|LAKHS?|LACS?|MILLIONS?|THOUSANDS?|BILLIONS?|"
    r"CR|MN|MM|BN|LAC)\b\.?",
    re.IGNORECASE,
)

#: A single numeric value: optional open-paren/sign/currency, grouped digits and
#: decimals, optional trailing percent / close-paren. No surrounding whitespace
#: is consumed (so the space before a value is preserved); multi-column runs are
#: handled by the collapse step below.
_CUR = re.escape(CURRENCY_SYMBOLS)
_NUMBER_RE = re.compile(
    r"[\(\[]?[-–—+]?[" + _CUR + r"]?\d[\d,\.]*\d[\)\]]?%?"
    r"|[\(\[]?[-–—+]?[" + _CUR + r"]?\d[\)\]]?%?"
)

#: A standalone dash used as a "nil" value in a number column.
_NIL_RE = re.compile(r"(?<![\w])[-–—]+(?![\w])")

#: One or more placeholder markers (with optional spaces/parens) in a row.
_COLLAPSE_RE = re.compile(r"#(?:[\s\(\)\[\]]*#)+")

PLACEHOLDER = "#"


def normalize(text: str, mask_currency: bool = True, ignore_case: bool = True) -> str:
    """Return the language-only form of ``text``.

    Numbers are always masked to ``#``. When ``mask_currency`` is true (the
    default, for cross-currency comparisons) currency symbols and unit words are
    removed too, so ``"(₹ in crores)"`` and ``"($ in millions)"`` match.
    """
    s = text
    if mask_currency:
        s = s.translate({ord(c): " " for c in CURRENCY_SYMBOLS})
        s = _CURRENCY_WORD_RE.sub(" ", s)
    s = _NUMBER_RE.sub(PLACEHOLDER, s)
    s = _NIL_RE.sub(PLACEHOLDER, s)
    s = _COLLAPSE_RE.sub(PLACEHOLDER, s)
    s = " ".join(s.split())
    if ignore_case:
        s = s.lower()
    return s


def is_blank(norm_text: str) -> bool:
    """A normalised line that carries no language (empty, or only a marker)."""
    stripped = norm_text.replace(PLACEHOLDER, "").strip()
    return stripped == ""
