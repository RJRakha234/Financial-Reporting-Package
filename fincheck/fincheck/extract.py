"""Reconstruct tabular structure from a financial-statement PDF.

Many statements are laid out as whitespace-aligned columns rather than ruled
tables, so relying on ruling lines is fragile. Instead we work from the word
boxes that PDF text carries: cluster words into rows by their vertical position,
detect which words are numbers, and group those numbers into columns by their
right edge (financial figures are right-aligned). Every cell keeps its bounding
box so inconsistencies can later be highlighted in the original PDF.
"""

import re
from dataclasses import dataclass, field

import pdfplumber

from .numbers import is_numberish, parse_number

# Phrases that mark a row as a column/period header rather than data.
# Kept deliberately specific: e.g. "statement of" catches the Cash Flow Statement
# title without misfiring on a "Cash flow hedge reserves" line item.
_HEADER_RE = re.compile(
    r"(?i)(year ended|months ended|quarter ended|period ended|as at|as of|"
    r"particulars|in ₹|in rs|in million|^note$|for the (year|period|quarter)|"
    r"balance sheet|statement of|^index$|page no)"
)


def _is_year_token(text: str) -> bool:
    t = text.strip().rstrip(",.")
    return t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2099

# Vertical slack (points) for treating two words as being on the same line.
_ROW_TOLERANCE = 3.0
# Horizontal gap (points) below which two number-ish tokens are one figure
# (handles space-separated thousands like "1 234 567").
_MERGE_GAP = 7.0
# Gap (points) between number right-edges that starts a new column.
_COLUMN_GAP = 18.0


@dataclass
class Cell:
    column: int
    value: float
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class Row:
    page_index: int
    top: float
    bottom: float
    label: str
    label_x0: float
    indent: int = 0
    is_header: bool = False
    cells: dict[int, Cell] = field(default_factory=dict)

    @property
    def is_heading(self) -> bool:
        return not self.cells


@dataclass
class Page:
    index: int
    width: float
    height: float
    rows: list[Row]
    n_columns: int


def _cluster_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        placed = False
        for row in rows:
            if abs(row[0]["top"] - word["top"]) <= _ROW_TOLERANCE:
                row.append(word)
                placed = True
                break
        if not placed:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _merge_numberish(words: list[dict]) -> list[dict]:
    """Glue adjacent number-ish tokens (e.g. ``1 234`` -> ``1234``)."""
    merged: list[dict] = []
    for word in words:
        if (
            merged
            and is_numberish(word["text"])
            and is_numberish(merged[-1]["text"])
            and word["x0"] - merged[-1]["x1"] <= _MERGE_GAP
            # Don't glue a date apart into a number: "31," + "2026" -> year stays.
            and not _is_year_token(word["text"])
        ):
            prev = merged[-1]
            prev["text"] = prev["text"] + " " + word["text"]
            prev["x1"] = word["x1"]
            prev["bottom"] = max(prev["bottom"], word["bottom"])
            prev["top"] = min(prev["top"], word["top"])
        else:
            merged.append(dict(word))
    return merged


def _column_edges(number_words: list[dict]) -> list[float]:
    """Cluster numeric right-edges (x1) into column reference positions."""
    edges = sorted(w["x1"] for w in number_words)
    if not edges:
        return []
    clusters: list[list[float]] = [[edges[0]]]
    for edge in edges[1:]:
        if edge - clusters[-1][-1] > _COLUMN_GAP:
            clusters.append([])
        clusters[-1].append(edge)
    return [sum(c) / len(c) for c in clusters]


def _assign_column(x1: float, edges: list[float]) -> int:
    return min(range(len(edges)), key=lambda i: abs(edges[i] - x1))


def _compute_indents(rows: list[Row]) -> None:
    """Map distinct label start positions to integer indent levels."""
    xs = sorted({round(r.label_x0, 0) for r in rows if r.label})
    levels = {x: i for i, x in enumerate(xs)}
    for row in rows:
        row.indent = levels.get(round(row.label_x0, 0), 0)


def _looks_like_header(label: str, number_words: list[dict]) -> bool:
    """A column/period header row that must not be treated as data."""
    if _HEADER_RE.search(label):
        return True
    # A row whose figures are all bare years (e.g. "2026  2025  2026  2025").
    if number_words and all(_is_year_token(w["text"]) for w in number_words):
        return True
    return False


def _build_page(page_index: int, page) -> Page:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    clustered = _cluster_rows(words)

    # First pass: merge tokens, split each row into label text + numeric words,
    # and flag header rows. Column layout is learned from data rows only so that
    # stray dates in headers do not define or shift columns.
    merged_rows: list[list[dict]] = []
    row_is_header: list[bool] = []
    number_words: list[dict] = []
    for raw_row in clustered:
        merged = _merge_numberish(raw_row)
        merged_rows.append(merged)
        nums = [w for w in merged if parse_number(w["text"]) is not None]
        label = " ".join(
            w["text"] for w in merged if parse_number(w["text"]) is None
        ).strip()
        header = _looks_like_header(label, nums)
        row_is_header.append(header)
        if not header:
            number_words.extend(nums)

    edges = _column_edges(number_words)

    rows: list[Row] = []
    for merged, is_header in zip(merged_rows, row_is_header):
        cells: dict[int, Cell] = {}
        label_tokens: list[str] = []
        label_x0 = merged[0]["x0"] if merged else 0.0
        for word in merged:
            value = parse_number(word["text"])
            if value is not None and edges:
                col = _assign_column(word["x1"], edges)
                # If two figures fall in one column on a row, keep the first.
                cells.setdefault(
                    col,
                    Cell(
                        column=col,
                        value=value,
                        text=word["text"].strip(),
                        x0=word["x0"],
                        x1=word["x1"],
                        top=word["top"],
                        bottom=word["bottom"],
                    ),
                )
            else:
                label_tokens.append(word["text"])
        if not (label_tokens or cells):
            continue
        top = min(w["top"] for w in merged)
        bottom = max(w["bottom"] for w in merged)
        rows.append(
            Row(
                page_index=page_index,
                top=top,
                bottom=bottom,
                label=" ".join(label_tokens).strip(),
                label_x0=label_x0,
                is_header=is_header,
                cells=cells,
            )
        )

    _compute_indents(rows)
    return Page(
        index=page_index,
        width=page.width,
        height=page.height,
        rows=rows,
        n_columns=len(edges),
    )


def extract_pages(pdf_path: str) -> list[Page]:
    pages: list[Page] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            pages.append(_build_page(i, page))
    return pages
