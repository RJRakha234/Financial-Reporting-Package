"""Split a financial-statement PDF into its numbered note sections.

Each statement is organised as numbered sections — ``1. Overview`` and its
``1.x`` sub-notes, then ``2. Notes to the ... Financial Statements`` and its
``2.x`` notes (Property plant and equipment, Investments, Leases, ...). The
goal here is to find those section boundaries and, for each section, isolate
the **narrative prose** (accounting policy / disclosure text) from the numeric
table rows so the prose can be compared across documents.

We build on :func:`fincheck.extract.extract_pages`, which already reconstructs
rows and right-aligned numeric cells from the PDF word boxes. A row with no
numeric cells is prose (or a heading); a row with cells is table data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..extract import Page, Row, extract_pages

# A note heading carries a two-level number ("2.7", "2.10", "1.2") followed by
# a title. Exactly two levels: this skips the one-level container headings
# ("1. Overview", "2. Notes to ...") — groupings, not comparable notes — and
# the deeper "2.10.1 Initial recognition" sub-headings inside a note.
#
# The number turns up two ways depending on the document:
#   * as a *numeric cell* at the far left of the row (the extractor treats it
#     as a right-aligned figure), with the title in the row label — e.g.
#     standalone/IFRS "TRADE RECEIVABLES" + cell "2.7"; or
#   * embedded in the label text, sometimes behind a decorative bullet glyph
#     that the PDF renders as junk — e.g. "XXX2.1 BUSINESS COMBINATIONS",
#     "X2.10 FINANCIAL INSTRUMENTS", "X10AO2.7 Property, plant and equipment".
_NOTE_NUM_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")
# Number embedded in the label behind a short run of non-space glyph junk
# ("XXX2.1", "X10AO2.7", "X20AOX20AO2.19"). The junk run must contain the "X"
# bullet glyph (checked in code) so ordinary words can't pose as a prefix. The
# *required* separator after the number (space/paren) is what distinguishes a
# heading from run-together prose that merely contains a cross-reference like
# "...note2.10for..." (no separator follows the number there).
_EMBEDDED_RE = re.compile(
    r"^(?P<pre>[A-Za-z0-9]{0,12}?)(?P<num>\d{1,2}\.\d{1,2})(?!\.\d)[\s.\)]+(?P<title>[A-Za-z].*)$"
)
# A glyph spliced *inside* the number, e.g. "2X .4 Prepayments" -> 2.4.
_SPLIT_NUM_RE = re.compile(
    r"^(?P<maj>\d{1,2})X[\sX]*\.[\sX]*(?P<min>\d{1,2})\s+(?P<title>[A-Za-z].*)$"
)
# A leading decorative bullet glyph to strip from a title ("X INVESTMENTS").
_BULLET_RE = re.compile(r"^X{1,3}\s+")

# Horizontal slack (points) for treating the number cell as the row's leading
# element rather than a figure in a data column.
_LEADING_SLACK = 12.0

# A row that is just a units caption ("(In ₹ crore)", "(Dollars in millions)"),
# or a bare page number carries no comparable prose.
_UNITS_RE = re.compile(
    r"(?i)^\(?\s*(?:in\s+)?(?:₹|rs|inr|us\$?|usd|\$|dollars?|rupees?)\b.*?"
    r"(?:crore|million|lakh|thousand)?s?\s*\)?$"
)
_PAGE_NO_RE = re.compile(r"^\d{1,3}$")

# Below this length a no-cell row is treated as table scaffolding (a column
# header, a line-item label, or a sub-heading) rather than narrative prose,
# unless it stands clear of any numeric (table) row.
_PROSE_MIN_LEN = 25
# A longer row may still be a wide table header (many columns spread across the
# page); if it sits directly beside a numeric row and is under this length it is
# dropped as scaffolding.
_TABLE_ADJ_MAX_LEN = 90


@dataclass
class Section:
    """A numbered note (or overview sub-note) within one document."""

    number: str
    title: str
    page_start: int
    page_end: int
    rows: list[Row] = field(default_factory=list)

    @property
    def is_overview(self) -> bool:
        return self.number.startswith("1")


def _valid_title(title: str) -> bool:
    """A real note heading has a short title (a few words), not a sentence."""
    return bool(title) and title[0].isalpha() and len(title) <= 80 and not title.endswith(".")


def _heading_of(row: Row) -> tuple[str, str] | None:
    """Return ``(number, title)`` if ``row`` is a note heading, else ``None``."""
    label = row.label.strip()

    # Case 1: the number is embedded in the label (behind an "X" bullet glyph).
    m = _EMBEDDED_RE.match(label)
    if m and ("X" in m.group("pre") or not m.group("pre")):
        title = m.group("title").strip()
        if _valid_title(title):
            return m.group("num"), title

    # Case 1b: a glyph spliced inside the number itself ("2X .4 Prepayments").
    m = _SPLIT_NUM_RE.match(label)
    if m:
        title = m.group("title").strip()
        if _valid_title(title):
            return f"{m.group('maj')}.{m.group('min')}", title

    # Case 2: the number is a left-positioned numeric cell, title in the label.
    title = _BULLET_RE.sub("", label).strip()
    if _valid_title(title):
        for cell in row.cells.values():
            if (
                _NOTE_NUM_RE.match(cell.text)
                and cell.x0 <= row.label_x0 + _LEADING_SLACK
            ):
                return cell.text, title
    return None


def _strip_leaders(text: str) -> str:
    """Drop dotted index leaders and trailing page numbers from a TOC line."""
    return re.sub(r"\s*\.{3,}.*$", "", text).strip()


def split_into_sections(pages: list[Page]) -> list[Section]:
    """Group a document's rows into :class:`Section` objects by note number.

    Rows that appear before the first heading (cover page, statements) are
    ignored. The table-of-contents index pages are skipped because their
    "heading" lines carry dotted leaders / page numbers, which we detect and
    treat as non-headings.
    """
    sections: list[Section] = []
    current: Section | None = None

    for page in pages:
        for row in page.rows:
            label = row.label.strip()
            # Index/TOC lines look like "2.7 Trade receivables ........ 14".
            if "…" in label or re.search(r"\.{3,}", label):
                continue
            heading = _heading_of(row)
            if heading:
                num, title = heading
                current = Section(
                    number=num,
                    title=_strip_leaders(title),
                    page_start=page.index,
                    page_end=page.index,
                )
                sections.append(current)
                continue
            if current is not None:
                current.rows.append(row)
                current.page_end = page.index

    return sections


def narrative_rows(section: Section) -> list[Row]:
    """Prose rows of a section, with table scaffolding filtered out.

    Tables are interleaved with prose in these notes. We drop:

    * rows carrying numeric cells (table data);
    * units captions and bare page numbers;
    * short rows (column headers, line-item labels, sub-headings) — aggressively
      when they sit directly beside a numeric row, and always when very short.

    What survives is the accounting-policy / disclosure prose, which is what the
    comparison cares about. Some short sentence tails are lost, but because the
    surviving rows are joined and compared character-wise, this trades a little
    completeness for prose that lines up reliably across documents.
    """
    rows = section.rows
    has_cells = [bool(r.cells) for r in rows]
    out: list[Row] = []
    for i, row in enumerate(rows):
        if row.cells:
            continue
        label = row.label.strip()
        if not label or _UNITS_RE.match(label) or _PAGE_NO_RE.match(label):
            continue
        if len(label) < _PROSE_MIN_LEN:
            continue
        # A shortish row sitting directly beside a numeric (table) row is most
        # likely a column header or line-item label — unless it reads like a
        # sentence (ends with terminal punctuation), in which case it is prose.
        beside_table = (i > 0 and has_cells[i - 1]) or (
            i + 1 < len(rows) and has_cells[i + 1]
        )
        if beside_table and len(label) < _TABLE_ADJ_MAX_LEN and label[-1] not in ".:":
            continue
        out.append(row)
    return out


def narrative_text(section: Section) -> str:
    """The section's narrative prose joined into a single string.

    Rows are joined with spaces; intra-word spacing is unreliable in these PDFs,
    so downstream comparison strips whitespace entirely (see
    :func:`fincheck.notes.normalize.fingerprint`).
    """
    return " ".join(r.label.strip() for r in narrative_rows(section))


def load_sections(pdf_path: str) -> list[Section]:
    """Convenience: extract a PDF and split it into sections in one call."""
    return split_into_sections(extract_pages(pdf_path))
