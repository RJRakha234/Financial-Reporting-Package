"""Reconstruct paragraphs and tables from a PDF, for side-by-side comparison.

Everything in :mod:`fincheck.compare` avoids this deliberately: a PDF holds
glyphs at coordinates, not paragraphs and tables, so any block structure is
inferred and can be wrong. Comparing two documents *as documents* needs that
structure anyway — you cannot put a paragraph beside its counterpart without
first deciding what a paragraph is — so this module makes the inference
explicitly, in one place, with its rules written down:

1. Spans sharing a text baseline form a **row**. Baselines are exact
   coordinates from the file, so this step is reliable.
2. A row is a **figure row** when its numbers sit to the right of its label —
   two or more numbers, or one number in the right-hand part of the page under
   a short label. Prose with a figure quoted mid-sentence stays prose.
3. Consecutive figure rows form a **table**; consecutive prose rows form a
   **paragraph**.
4. A block continues across a page break when the text before the break does
   not end a sentence, or when a table resumes with the same column count.
   Statements set landscape in one document and portrait in another split at
   different points, so without this they would never line up.

Each rule can misfire. The blocks are a reading aid for a human reviewer, not
evidence — treat a difference as "look at this", and use the exact layers when
you need proof.
"""

import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .numbers import parse_number

# Baselines within this many points are the same row.
_BASELINE_TOLERANCE = 2.0
# A lone figure this far across the page (as a fraction of width) reads as a
# column entry rather than as a number quoted in a sentence.
_COLUMN_ZONE = 0.55
# Labels longer than this are prose, however many numbers they contain.
_MAX_LABEL_WORDS = 16
# A baseline gap this many times the page's usual line pitch starts a new block.
_PARAGRAPH_GAP = 1.6
# Tables carry deliberate internal spacing around their sections, so they need a
# wider gap than prose before it means "a different table".
_TABLE_GAP = 3.2
# How far to look ahead for a figure row before deciding a label-only row ended
# the table rather than heading a section inside it.
_SECTION_LOOKAHEAD = 3
# Sentence-final punctuation, used to decide whether a paragraph continues.
_ENDS_SENTENCE = re.compile(r"[.:;!?…]['\")\]]?\s*$")


@dataclass
class Figure:
    """One numeric cell of a reconstructed table row."""

    text: str
    value: float
    x0: float
    x1: float


@dataclass
class Row:
    page: int
    baseline: float
    label: str
    figures: list[Figure] = field(default_factory=list)
    # Distance from the previous baseline on this page, and the page's usual
    # line pitch. Their ratio is what separates a new paragraph from a new line.
    gap: float = 0.0
    pitch: float = 0.0

    def breaks_after(self, kind: str) -> bool:
        """Is the gap before this row wide enough to end a block of ``kind``?"""
        if self.pitch <= 0:
            return False
        limit = _TABLE_GAP if kind == "table" else _PARAGRAPH_GAP
        return self.gap > limit * self.pitch

    @property
    def is_figure_row(self) -> bool:
        return bool(self.figures)

    def as_text(self) -> str:
        cells = "  ".join(f.text for f in self.figures)
        return f"{self.label}  {cells}".strip()


@dataclass
class Block:
    kind: str  # "paragraph" | "table"
    rows: list[Row]
    page_start: int
    page_end: int

    @property
    def text(self) -> str:
        if self.kind == "paragraph":
            return " ".join(r.label for r in self.rows if r.label).strip()
        return "\n".join(r.as_text() for r in self.rows)

    @property
    def labels(self) -> list[str]:
        return [r.label for r in self.rows if r.label]

    @property
    def label_text(self) -> str:
        return " ".join(self.labels)

    @property
    def figures(self) -> list[Figure]:
        return [f for r in self.rows for f in r.figures]

    @property
    def values(self) -> list[float]:
        return [f.value for f in self.figures]

    @property
    def n_columns(self) -> int:
        return max((len(r.figures) for r in self.rows), default=0)

    @property
    def pages(self) -> str:
        if self.page_start == self.page_end:
            return str(self.page_start)
        return f"{self.page_start}–{self.page_end}"

    @property
    def tokens(self) -> set:
        """Word set used to judge similarity against a block in the other file."""
        return _tokenise(self.label_text)


def _tokenise(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


_FOLIO_RE = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)


def _is_folio(row: "Row") -> bool:
    return not row.figures and bool(_FOLIO_RE.match(row.label.strip()))


def _rows_on_page(page: "fitz.Page", index: int) -> list[Row]:
    """Cluster the page's words into rows by vertical position.

    Words, not spans: a spreadsheet exported to PDF often writes a whole table
    row as one show-text operation, so testing spans for numberhood would file
    every figure on the row under its label. Word boxes come from the file
    either way, so nothing is being guessed here.
    """
    words = [w for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []

    # (x0, y0, x1, y1, text, block, line, word_no)
    buckets: list[list] = []
    for word in sorted(words, key=lambda w: (w[3], w[0])):
        if buckets and abs(buckets[-1][0][3] - word[3]) <= _BASELINE_TOLERANCE:
            buckets[-1].append(word)
        else:
            buckets.append([word])

    width = page.rect.width
    rows: list[Row] = []
    for bucket in buckets:
        bucket.sort(key=lambda w: w[0])
        text_parts, figures = [], []
        for word in bucket:
            text = word[4].strip()
            value = parse_number(text)
            if value is None:
                text_parts.append(text)
            else:
                figures.append(
                    Figure(
                        text=text,
                        value=value,
                        x0=round(word[0], 2),
                        x1=round(word[2], 2),
                    )
                )
        label = re.sub(r"\s+", " ", " ".join(text_parts)).strip()

        # Decide whether those numbers are table cells or prose.
        tabular = False
        if figures and len(label.split()) <= _MAX_LABEL_WORDS:
            if len(figures) >= 2:
                tabular = True
            elif figures[0].x0 > _COLUMN_ZONE * width:
                tabular = True
        if not tabular:
            # Keep the digits in the sentence they belong to.
            label = re.sub(
                r"\s+", " ", " ".join(w[4].strip() for w in bucket)
            ).strip()
            figures = []

        rows.append(
            Row(page=index + 1, baseline=round(bucket[0][3], 2),
                label=label, figures=figures)
        )

    # A bare number alone in the top or bottom margin is the folio, not content;
    # left in, it would be glued onto the first or last paragraph of the page.
    while rows and _is_folio(rows[0]):
        rows.pop(0)
    while rows and _is_folio(rows[-1]):
        rows.pop()

    # Line pitch as the median gap on the page: robust to the handful of large
    # gaps that separate blocks, which is exactly what we want to detect.
    gaps = [
        round(b.baseline - a.baseline, 2) for a, b in zip(rows, rows[1:])
    ]
    positive = sorted(g for g in gaps if g > 0)
    pitch = positive[len(positive) // 2] if positive else 0.0
    for row, gap in zip(rows[1:], gaps):
        row.gap = gap
        row.pitch = pitch
    return rows


def _continues(previous: Block, row: Row) -> bool:
    """Does ``row`` carry on the block interrupted by a page break?"""
    if previous.kind == "table":
        return row.is_figure_row and len(row.figures) == previous.n_columns
    return bool(previous.labels) and not _ENDS_SENTENCE.search(previous.labels[-1])


def _heads_a_section(rows: list[Row], i: int) -> bool:
    """Is ``rows[i]`` a label-only row heading a section inside a table?

    A statement reads "Financial assets" and then lists figures beneath it.
    Treating that heading as the end of the table would cut one balance sheet
    into a handful of fragments that no longer resemble the other document's.
    """
    row = rows[i]
    if row.is_figure_row or row.breaks_after("table"):
        return False
    for ahead in rows[i + 1 : i + 1 + _SECTION_LOOKAHEAD]:
        if ahead.is_figure_row:
            return not ahead.breaks_after("table")
        if ahead.breaks_after("table"):
            return False
    return False


def segment(pdf_path: str) -> list[Block]:
    """Split a PDF into an ordered list of paragraph and table blocks."""
    doc = fitz.open(pdf_path)
    try:
        rows: list[Row] = []
        for index, page in enumerate(doc):
            rows.extend(
                r for r in _rows_on_page(page, index) if r.label or r.figures
            )
    finally:
        doc.close()

    blocks: list[Block] = []
    for i, row in enumerate(rows):
        current = blocks[-1] if blocks else None
        in_table = current is not None and current.kind == "table"

        if row.is_figure_row:
            kind = "table"
        elif in_table and _heads_a_section(rows, i):
            kind = "table"
        else:
            kind = "paragraph"

        if current is not None and current.kind == kind:
            if current.page_end == row.page:
                joins = not row.breaks_after(kind)
            else:
                joins = _continues(current, row)
            if joins:
                current.rows.append(row)
                current.page_end = row.page
                continue

        blocks.append(
            Block(kind=kind, rows=[row], page_start=row.page, page_end=row.page)
        )
    return blocks
