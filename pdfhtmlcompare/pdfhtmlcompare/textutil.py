"""Word/label normalisation shared by the PDF and HTML readers.

Comparison is about whether the *content* renders the same, not the formatting.
So when we compare wording we fold away cosmetic differences that a PDF→HTML
conversion legitimately introduces: curly vs straight quotes, hyphen vs space
("Non-current" / "Non current"), case, and surrounding punctuation.
"""

import re

from .numbers import is_numberish

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def word_norms(token: str) -> list[str]:
    """Normalised sub-words of a token (possibly several, possibly none).

    Splits on any non-alphanumeric so "Non-current" and "Non current" both give
    ``["non", "current"]``. Figures and dotted index leaders yield nothing.
    """
    if is_numberish(token):
        return []
    t = token.translate(_QUOTES)
    if "…" in t or "..." in t:
        return []
    out = []
    for part in re.split(r"[^a-z0-9]+", t.lower()):
        if len(part) >= 2 and any(ch.isalpha() for ch in part):
            out.append(part)
    return out


def label_key(words: list[str]) -> str:
    """A stable key for matching a row by its label wording."""
    return " ".join(words)


def has_leader(text: str) -> bool:
    return "…" in text or "..." in text
