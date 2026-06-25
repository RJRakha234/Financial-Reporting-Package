"""Shared data model: a document is a list of logical lines, each with figures."""

from dataclasses import dataclass, field

from .textutil import has_leader, label_key, word_norms

# A label longer than this (in words) is prose, not a financial line item.
MAX_LABEL_WORDS = 12

_MONTHS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
}


def _is_day_after_month(value: float, prev: str) -> bool:
    """A small integer right after a month name is a date day, not a figure."""
    return (
        value == int(value)
        and 1 <= value <= 31
        and prev.strip().strip(".,").lower() in _MONTHS
    )


@dataclass
class Figure:
    """A real financial figure read from one document."""

    value: float
    text: str  # as printed, e.g. "12,450"
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Line:
    """One logical line (a table row / heading / paragraph) of a document."""

    index: int  # reading order within the document
    text: str
    figures: list[Figure] = field(default_factory=list)
    word_norms: list[str] = field(default_factory=list)
    word_texts: list[str] = field(default_factory=list)
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def label_key(self) -> str:
        return label_key(self.word_norms)

    @property
    def is_financial(self) -> bool:
        """A real financial table row: a short label with at least one figure,
        that is not a table-of-contents (dot-leader) entry and not a narrative
        sentence (a statement line item never ends in a full stop)."""
        return (
            bool(self.figures)
            and bool(self.word_norms)
            and len(self.word_norms) <= MAX_LABEL_WORDS
            and not has_leader(self.text)
            and not self.text.rstrip().endswith(".")
        )


def build_line(
    index: int,
    full_text: str,
    raw_tokens,
    page: int | None = None,
    bbox: tuple | None = None,
):
    """Construct a Line from ``(token_text, value_or_None, bbox_or_None)`` triples.

    ``value`` is non-None only for tokens that are *real* figures (years,
    identifiers, clause refs and footnote markers having been filtered upstream).
    """
    figures: list[Figure] = []
    norms: list[str] = []
    texts: list[str] = []
    prev = ""
    for text, value, tbbox in raw_tokens:
        if value is not None and not _is_day_after_month(value, prev):
            figures.append(Figure(value, text, page, tbbox))
        elif value is None:
            ns = word_norms(text)
            if ns:
                norms.extend(ns)
                texts.append(text)
        prev = text
    return Line(index, full_text, figures, norms, texts, page, bbox)
