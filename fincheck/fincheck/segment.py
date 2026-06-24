"""Casting of segment-reporting matrices (note 2.23).

Segment tables don't fit the generic period-table parser: instead of a plain
``2025 2024`` year row they put the current year and the comparative year on two
*separate* rows (the second unlabelled), across one column per business segment
plus a Total. The current statement carries two such matrices (three-month and
six-month) and the prior statement one (three-month), so the same casting
identity can be checked cell by cell::

    six-month segment value == three-month (current) + three-month (prior)

The business-segment columns appear in the same order in every matrix, so values
are aligned by column position; column *names* are recovered, best-effort, from
the wrapped header above the figures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pdfplumber

from .casting import (
    Cell,
    _cluster_rows,
    _looks_like_heading,
    _merge_numberish,
    _row_text,
    normalize_label,
)
from .numbers import parse_number

# "Three months ended September 30, 2025 and September 30, 2024:" — the two-date
# header that introduces a segment matrix (distinct from the generic period
# header, which lists the periods side by side, and from the geography table,
# whose header starts with "For the ...").
_SEG_HEADER_RE = re.compile(
    r"(?i)^(three|six)\s+months?\s+ended\s+\w+\s+\d{1,2},\s*(\d{4})\s+and\s+"
    r"\w+\s+\d{1,2},\s*(\d{4})\s*:?\s*$"
)
_STOP_RE = re.compile(
    r"(?i)^(\(\d+\)\s|\*|significant clients|disclosure of revenue|"
    r"business segments|\(in )"
)

# Once the (very specific) segment header has matched, the first row carrying
# several figures is the first data row and defines the columns; the wrapped
# name band above it carries only stray footnote markers (one or two figures).
_MIN_WIDE = 3            # a row with at least this many figures defines columns
_ASSIGN_TOL = 16.0      # points: a figure this close to a column centre belongs


def _centre(word) -> float:
    return (word["x0"] + word["x1"]) / 2


@dataclass
class SegmentMetric:
    label: str
    cy: dict[int, Cell] = field(default_factory=dict)   # column -> current-year cell
    py: dict[int, Cell] = field(default_factory=dict)   # column -> prior-year cell


@dataclass
class SegmentTable:
    page_index: int
    months: int
    cy_year: int
    py_year: int
    centres: list[float]
    names: list[str]
    metrics: list[SegmentMetric]


def _cell(word) -> Cell:
    return Cell(
        value=parse_number(word["text"]),
        text=word["text"].strip(),
        x0=word["x0"], x1=word["x1"], top=word["top"], bottom=word["bottom"],
    )


_NAME_SKIP = {"in", "crore", "(in", "crore)", "₹", "and", "particulars", "(in₹"}
_FOOTMARK_RE = re.compile(r"\(\d+\)|\*|[†‡#]")


def _column_names(name_rows: list[list[dict]], centres: list[float]) -> list[str]:
    """Assign the wrapped header words to the nearest column centre, in order."""
    buckets: list[list[tuple[float, float, str]]] = [[] for _ in centres]
    for row in name_rows:
        for w in row:
            if parse_number(w["text"]) is not None:
                continue
            token = _FOOTMARK_RE.sub("", w["text"]).strip()
            if len(token) < 2 or token.lower() in _NAME_SKIP:
                continue
            c = _centre(w)
            i = min(range(len(centres)), key=lambda k: abs(centres[k] - c))
            if abs(centres[i] - c) <= 22:
                buckets[i].append((w["top"], w["x0"], token))
    names = []
    for i, items in enumerate(buckets):
        items.sort()
        text = " ".join(t for _, _, t in items).strip(" ,")
        names.append(text or ("Total" if i == len(centres) - 1 else f"Segment {i + 1}"))
    return names


def _assign(row: list[dict], centres: list[float]) -> dict[int, Cell]:
    cells: dict[int, Cell] = {}
    for w in row:
        if parse_number(w["text"]) is None:
            continue
        c = _centre(w)
        i = min(range(len(centres)), key=lambda k: abs(centres[k] - c))
        if abs(centres[i] - c) <= _ASSIGN_TOL and i not in cells:
            cells[i] = _cell(w)
    return cells


def extract_segment_tables(pdf_path: str) -> list[SegmentTable]:
    tables: list[SegmentTable] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            rows = [_merge_numberish(r) for r in _cluster_rows(page.extract_words())]
            i = 0
            while i < len(rows):
                m = _SEG_HEADER_RE.match(_row_text(rows[i]).strip())
                if not m:
                    i += 1
                    continue
                months = 3 if m.group(1).lower() == "three" else 6
                cy_year, py_year = int(m.group(2)), int(m.group(3))

                # Header/name band runs up to the first wide figure row.
                j = i + 1
                name_rows: list[list[dict]] = []
                while j < len(rows):
                    figs = [w for w in rows[j] if parse_number(w["text"]) is not None]
                    if len(figs) >= _MIN_WIDE:
                        break
                    name_rows.append(rows[j])
                    j += 1
                if j >= len(rows):
                    i = j
                    continue

                centres = sorted(_centre(w) for w in rows[j]
                                 if parse_number(w["text"]) is not None)
                names = _column_names(name_rows, centres)

                metrics: list[SegmentMetric] = []
                k = j
                while k < len(rows):
                    text = _row_text(rows[k]).strip()
                    if _SEG_HEADER_RE.match(text) or _looks_like_heading(text) \
                            or _STOP_RE.match(text):
                        break
                    figs = [w for w in rows[k] if parse_number(w["text"]) is not None]
                    label = " ".join(
                        w["text"] for w in rows[k]
                        if parse_number(w["text"]) is None
                    ).strip()
                    if figs:
                        cells = _assign(rows[k], centres)
                        alpha = sum(ch.isalpha() for ch in label)
                        if alpha >= 3:                       # labelled -> current year
                            metrics.append(SegmentMetric(label=label, cy=cells))
                        elif metrics and not metrics[-1].py:  # unlabelled -> prior year
                            metrics[-1].py = cells
                    k += 1

                if metrics:
                    tables.append(SegmentTable(
                        page_index=page_index, months=months,
                        cy_year=cy_year, py_year=py_year,
                        centres=centres, names=names, metrics=metrics,
                    ))
                i = k
    return tables


def _pick(tables: list[SegmentTable], months: int) -> SegmentTable | None:
    for t in tables:
        if t.months == months:
            return t
    return None


def segment_checks(current_pdf: str, prior_pdf: str):
    """Build casting checks for the segment matrices. Returns a list of CastChecks."""
    from .casting import CastCheck

    cur = extract_segment_tables(current_pdf)
    pri = extract_segment_tables(prior_pdf)
    six = _pick(cur, 6)
    three_cur = _pick(cur, 3)
    three_pri = _pick(pri, 3)
    if not (six and three_cur and three_pri):
        return []

    checks: list[CastCheck] = []
    n = min(len(six.metrics), len(three_cur.metrics), len(three_pri.metrics))
    for idx in range(n):
        m6, m3c, m3p = six.metrics[idx], three_cur.metrics[idx], three_pri.metrics[idx]
        # Only line items that clearly correspond across the matrices.
        if normalize_label(m6.label).split()[:2] != normalize_label(m3c.label).split()[:2]:
            continue
        name = six.names
        for band, y in ((("cy", six.cy_year)), (("py", six.py_year))):
            cells6 = getattr(m6, band)
            cells3c = getattr(m3c, band)
            cells3p = getattr(m3p, band)
            for col in sorted(set(cells6) & set(cells3c) & set(cells3p)):
                col_name = name[col] if col < len(name) else f"col{col}"
                checks.append(CastCheck(
                    note="2.23",
                    title="Segment reporting",
                    label=f"{m6.label} — {col_name}",
                    year=y,
                    page_index=six.page_index,
                    six_month=cells6[col].value,
                    current_quarter=cells3c[col].value,
                    prior_quarter=cells3p[col].value,
                    additive=True,
                    six_cell=cells6[col],
                    current_quarter_cell=cells3c[col],
                    quarter_page_index=three_cur.page_index,
                ))
    return checks
