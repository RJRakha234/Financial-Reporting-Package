"""Shared data model.

The whole content of a document is compared as a **token stream**: every word
and every number, in reading order. Comparing streams (rather than lines) makes
the comparison immune to how text is wrapped — the PDF breaks lines to the page
width, the HTML keeps a paragraph per block, but the sequence of words and
numbers is the same when the content is the same.

Each :class:`Token` keeps a back-pointer to its line (and, on the PDF side, its
page and bounding box) so a difference can be reported on the right page and the
outputs can be annotated in place.
"""

import re
from dataclasses import dataclass, field

from .textutil import word_norms

# Tokens are compared by these keys.
WORD = "word"
NUMBER = "number"

# Parenthesised single digits are footnote/superscript reference markers, not
# content; they extract inconsistently between PDF and HTML, so skip them.
_FOOTNOTE_RE = re.compile(r"^\(\d\)$")

_MONTHS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
}


def number_key(value: float) -> str:
    """Compare numbers by value, so 8,750 and 8750 match but 8,750 ≠ 8,570."""
    return f"{value:.2f}"


def _is_day_after_month(value: float, prev: str) -> bool:
    return (
        value == int(value)
        and 1 <= value <= 31
        and prev.strip().strip(".,").lower() in _MONTHS
    )


@dataclass
class Token:
    kind: str          # WORD or NUMBER
    key: str           # comparison key
    text: str          # original token text (for display)
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    line_id: int = -1
    status: str = "matched"  # set by the comparison: matched|changed|missing|added


@dataclass
class Line:
    id: int
    text: str
    tokens: list[Token] = field(default_factory=list)
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def keys(self) -> list[str]:
        return [t.key for t in self.tokens]


def build_line(line_id, full_text, raw_tokens, page=None, bbox=None) -> Line:
    """Build a Line from ``(token_text, value_or_None, bbox_or_None)`` triples.

    A non-None ``value`` is a number; everything else is split into normalised
    words. A small integer right after a month name is a date day, not a number.
    """
    tokens: list[Token] = []
    prev = ""
    for text, value, tbbox in raw_tokens:
        if _FOOTNOTE_RE.match(text.strip()):
            prev = text
            continue
        if value is not None and not _is_day_after_month(value, prev):
            tokens.append(Token(NUMBER, number_key(value), text, page, tbbox, line_id))
        elif value is None:
            for norm in word_norms(text):
                tokens.append(Token(WORD, norm, text, page, tbbox, line_id))
        prev = text
    return Line(line_id, full_text, tokens, page, bbox)
