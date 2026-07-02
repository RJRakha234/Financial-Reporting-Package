"""Cross-period *casting* of interim financial statements.

Casting is the review check that, in a current-period interim statement, the
**year-to-date (six-month) figure equals the current-quarter (three-month)
figure plus the prior-quarter (three-month) figure** taken from the previous
interim statement::

    six months ended 30-Sep-2025  ==  three months ended 30-Sep-2025
                                      + three months ended 30-Jun-2025

The same identity holds for the comparative year shown in both statements
(six months 30-Sep-2024 == three months 30-Sep-2024 + three months 30-Jun-2024),
so every line item is cast for *both* the current and the comparative year.

This module reads two PDFs — the *current* statement (which carries both a
three-month and a six-month column for each year) and the *prior* statement
(which carries the earlier three-month column) — locates every table that has a
``Three months ended`` / ``Six months ended`` period header, lines the line
items up by label, and checks the identity for every figure.

Unlike :mod:`fincheck.extract`, column geometry is learned *per table* from that
table's own period header and year row, so stray figures elsewhere on the page
(signatory DINs, page numbers, narrative amounts) cannot pollute the columns.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

import pdfplumber

from .numbers import is_numberish, parse_number

# ---------------------------------------------------------------------------
# Row / word geometry helpers (kept local so casting is self-contained)
# ---------------------------------------------------------------------------

_ROW_TOLERANCE = 3.0      # points: words within this vertical band are one row
_MERGE_GAP = 7.0          # points: glue space-separated thousands ("1 234")
_COLUMN_MATCH = 16.0      # points: a figure this close to a column edge belongs

# A period header row: "... Three months ended December 31, Nine months ended ..."
# Interim statements use three-month and a year-to-date column that is six, nine
# or twelve months ("year ended") depending on the quarter.
_MONTHS_RE = re.compile(
    r"(?i)(three|six|nine|twelve)\s+months?\s+ended|year\s+ended")
_NUMWORD = {"three": 3, "six": 6, "nine": 9, "twelve": 12}

# A note heading such as "2.16 REVENUE FROM OPERATIONS" or "2.21.2 Legal ...".
# A few statements prefix headings with an internal tag like "X13AO"; tolerate it.
_NOTE_RE = re.compile(r"^\s*(?:X\d+\w*\s*)?(\d+\.\d+(?:\.\d+)?)\s+(.+)$")
# Table of contents lines carry a dotted leader; never treat them as headings.
_LEADER_RE = re.compile(r"[.…]{4,}")

# Footnote / reference decorations stripped when matching labels across files.
_FOOTNOTE_RE = re.compile(r"\((?:\d+|[a-z])\)|[*#§†‡]|\bRefer(?:ence)?\b.*", re.I)
_WS_RE = re.compile(r"\s+")

# The boilerplate that closes a primary statement (signatory block). A table's
# body must not run into it, or the signatories' DINs pollute the columns.
_FOOTER_RE = re.compile(
    r"(?i)(the accompanying notes|as per our report|for and on behalf of|"
    r"chartered accountants|for Deloitte|membership no|chief financial officer|"
    r"company secretary|firm.?s registration)"
)


def _is_year(text: str) -> bool:
    t = text.strip().rstrip(",.")
    return t.isdigit() and len(t) == 4 and 1990 <= int(t) <= 2099


@dataclass
class Cell:
    """A single figure placed in a period column, with its PDF bounding box."""

    value: float
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page_index: int | None = None   # page the figure sits on (for highlighting)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class PeriodColumn:
    """One value column of a period table, e.g. (months=6, year=2025)."""

    index: int
    months: int          # 3 or 6
    year: int
    edge_x1: float       # right edge the column's figures align to


@dataclass
class TableRow:
    label: str
    page_index: int
    top: float
    bottom: float
    cells: dict[int, Cell] = field(default_factory=dict)   # column index -> Cell


@dataclass
class PeriodTable:
    """A table whose columns are three-/six-month periods across two years."""

    page_index: int
    note: str            # "2.16", "PL", "2.24", ... — used to pair tables
    title: str
    columns: list[PeriodColumn]
    rows: list[TableRow]

    def column(self, months: int, year: int) -> PeriodColumn | None:
        for c in self.columns:
            if c.months == months and c.year == year:
                return c
        return None

    @property
    def years(self) -> list[int]:
        return sorted({c.year for c in self.columns}, reverse=True)


# ---------------------------------------------------------------------------
# Low-level extraction
# ---------------------------------------------------------------------------

def _cluster_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for row in rows:
            if abs(row[0]["top"] - word["top"]) <= _ROW_TOLERANCE:
                row.append(word)
                break
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _merge_numberish(words: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for word in words:
        prev = merged[-1] if merged else None
        # A parenthesised negative typeset with a space after the opening paren
        # ("( 488)") is split by the extractor into a lone "(" and a "488)" that
        # neither parse on their own, so the figure is lost. Glue them back into
        # "(488)". This shows up in the three-month movement schedules.
        if (
            prev is not None
            and prev["text"].strip() == "("
            and is_numberish(word["text"])
            and word["text"].rstrip().endswith(")")
            and word["x0"] - prev["x1"] <= _MERGE_GAP
        ):
            prev["text"] = "(" + word["text"].strip()
            prev["x1"] = word["x1"]
            prev["top"] = min(prev["top"], word["top"])
            prev["bottom"] = max(prev["bottom"], word["bottom"])
            continue
        if (
            prev is not None
            and is_numberish(word["text"])
            and is_numberish(prev["text"])
            and word["x0"] - prev["x1"] <= _MERGE_GAP
            and not _is_year(word["text"])
            and not _is_year(prev["text"])
        ):
            prev["text"] = prev["text"] + " " + word["text"]
            prev["x1"] = word["x1"]
            prev["top"] = min(prev["top"], word["top"])
            prev["bottom"] = max(prev["bottom"], word["bottom"])
        else:
            merged.append(dict(word))
    return merged


def _row_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


@lru_cache(maxsize=8)
def _pdf_rows(pdf_path: str) -> tuple[tuple[list[dict], ...], ...]:
    """Word boxes of a PDF, clustered into merged rows per page (cached).

    Reading and laying out a big scanned filing is the slow step, and the
    period, segment and schedule passes each need the same rows, so the result
    is memoised per path — one parse of each statement instead of six.
    """
    pages: list[tuple[list[dict], ...]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            pages.append(tuple(_merge_numberish(r) for r in _cluster_rows(words)))
    return tuple(pages)


_MONTH_RE = re.compile(
    r"(?i)\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b")


def _is_year_row(words: list[dict]) -> bool:
    """A period header row identifying the columns by their year.

    Usually bare years (``2025 2024 2025 2024``), but some statements spell the
    period end out in full (``September 30, 2025  September 30, 2024 ...``); the
    only non-year numbers then are days of the month, so a row of years plus
    month names and day numbers still counts.
    """
    years = [w for w in words if _is_year(w["text"])]
    if len(years) < 2:
        return False
    others = [
        w for w in words
        if not _is_year(w["text"]) and parse_number(w["text"]) is not None
    ]
    if not others:
        return True
    has_month = any(_MONTH_RE.search(w["text"]) for w in words)
    return has_month and all(0 < (parse_number(w["text"]) or 0) <= 31 for w in others)


def _header_above(rows: list[list[dict]], year_index: int) -> int | None:
    """Nearest row above the year row that is a period column header.

    A narrative sentence ("Other income for the three months and six months
    ended ...") also matches the month pattern, so we take the *closest* header
    to the year row — the real column header always sits just above it.
    """
    for j in range(year_index - 1, max(year_index - 6, -1), -1):
        if _MONTHS_RE.search(_row_text(rows[j])):
            return j
    return None


def _period_blocks(header_words: list[dict]) -> list[tuple[float, int]]:
    """The (x-start, months) of each period block named in a column header.

    A header reads e.g. "Three months ended ... Nine months ended ..." or
    "Three months ended ... Year ended ...". Each leading word ("Three", "Nine",
    "Year", …) marks where that block's columns begin; "year" counts as twelve
    months. Returned sorted left-to-right.
    """
    blocks: list[tuple[float, int]] = []
    for w in header_words:
        t = w["text"].strip().lower()
        if t in _NUMWORD:
            blocks.append((w["x0"], _NUMWORD[t]))
        elif t == "year":
            blocks.append((w["x0"], 12))
    return sorted(blocks)


def _months_at(x: float, blocks: list[tuple[float, int]]) -> int:
    """Months of the period block a column at right-edge ``x`` sits under."""
    months = blocks[0][1] if blocks else 3
    for x_start, m in blocks:
        if x_start <= x:
            months = m
    return months


_COLUMN_GAP = 18.0   # points between numeric right-edges that start a new column


def _cluster_edges(edges: list[float]) -> list[tuple[float, int]]:
    """Cluster numeric right-edges into (centre, membership-count) columns."""
    edges = sorted(edges)
    if not edges:
        return []
    clusters: list[list[float]] = [[edges[0]]]
    for e in edges[1:]:
        if e - clusters[-1][-1] > _COLUMN_GAP:
            clusters.append([])
        clusters[-1].append(e)
    return [(sum(c) / len(c), len(c)) for c in clusters]


def _build_columns(header_words, year_words, body_rows) -> list[PeriodColumn]:
    """Learn the value columns of a table.

    Column *positions* are taken from the right edges of the figures in the body
    rows — financial figures are right-aligned, and the period/year header tokens
    are often shifted a few points off that edge (so the header alone is not a
    reliable guide to where the figures sit). The real value columns are the ones
    that recur on most rows, so when more edge clusters appear than there are
    year headers (a stray figure in a footnote, a wide share count) we keep the
    busiest clusters. Each kept column is then labelled with a year (from the
    year row) and a period: the period blocks named in the header ("Three months
    ended", "Nine months ended", "Year ended", …) say how many months each column
    covers. A prior interim statement's only block is its own year-to-date one.
    """
    years = sorted(year_words, key=lambda w: w["x1"])
    if not years:
        return []
    blocks = _period_blocks(header_words)

    # Right edges of figures in the value region (right of the leftmost year), so
    # note references like "2.16" in the label area are excluded.
    floor = years[0]["x0"] - 8.0
    value_edges = [
        w["x1"]
        for row in body_rows
        for w in row
        if parse_number(w["text"]) is not None and w["x1"] >= floor
    ]
    clusters = _cluster_edges(value_edges)

    if len(clusters) >= len(years):
        # Keep the busiest clusters (the genuine columns), then restore order.
        clusters.sort(key=lambda c: c[1], reverse=True)
        edges = sorted(c[0] for c in clusters[: len(years)])
    else:
        # Fewer figure-columns than headers (a very short table): trust headers.
        edges = [w["x1"] for w in years]

    columns: list[PeriodColumn] = []
    for idx, edge in enumerate(edges):
        yw = min(years, key=lambda w: abs(w["x1"] - edge))
        columns.append(
            PeriodColumn(
                index=idx,
                months=_months_at(edge, blocks),
                year=int(yw["text"].strip().rstrip(",.")),
                edge_x1=edge,
            )
        )
    return columns


def _assign_cells(words: list[dict], columns: list[PeriodColumn]):
    """Split a row into (label, {col_index: Cell}) using column right edges."""
    cells: dict[int, Cell] = {}
    label_tokens: list[str] = []
    for w in words:
        value = parse_number(w["text"])
        if value is None:
            label_tokens.append(w["text"])
            continue
        # nearest column by right edge, but only if genuinely aligned
        col = min(columns, key=lambda c: abs(c.edge_x1 - w["x1"]))
        if abs(col.edge_x1 - w["x1"]) <= _COLUMN_MATCH:
            if col.index not in cells:
                cells[col.index] = Cell(
                    value=value, text=w["text"].strip(),
                    x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
                )
        else:
            # an aligned-looking number that is really part of the label
            # (e.g. a note reference "2.11") — keep it in the label
            label_tokens.append(w["text"])
    return " ".join(label_tokens).strip(), cells


def _looks_like_heading(text: str) -> bool:
    return bool(_NOTE_RE.match(text)) and not _LEADER_RE.search(text)


def _table_note(prev_heading: str, header_text: str) -> tuple[str, str]:
    """Resolve a (note-key, title) for a table from context."""
    low = header_text.lower()
    m = _NOTE_RE.match(prev_heading or "")
    if not m:
        # Not inside a numbered note, so this may be the primary income statement
        # (Ind AS "Statement of Profit and Loss"; IFRS "Statement of
        # Comprehensive Income").
        if "statement of profit and loss" in low:
            return "PL", "Statement of Profit and Loss"
        if "comprehensive income" in low or "income statement" in low:
            return "PL", "Statement of Comprehensive Income"
    if "function wise" in low or "function-wise" in low:
        return "2.24", "Function-wise classification of P&L"
    if m:
        return m.group(1), m.group(2).strip()
    return prev_heading.strip()[:40] or "?", prev_heading.strip()[:60]


def extract_period_tables(pdf_path: str) -> list[PeriodTable]:
    """Find every three-/six-month period table in a statement PDF.

    Extraction is driven by the *year row* (``2025 2024 ...``): for each one we
    look just above it for the real column header and just below it for the body
    rows, learning the column geometry from that table alone.
    """
    tables: list[PeriodTable] = []
    # The note heading a table belongs to often sits on an earlier page (a note
    # spanning several pages, or its sub-tables), so the running note carries
    # across pages rather than resetting each one.
    current_note = ""
    for page_index, page_rows in enumerate(_pdf_rows(pdf_path)):
            rows = list(page_rows)

            # Running note context for titling tables (display only).
            note_at: list[str] = [""] * len(rows)
            for i, row in enumerate(rows):
                text = _row_text(row)
                if _looks_like_heading(text):
                    current_note = text
                note_at[i] = current_note

            i = 0
            while i < len(rows):
                if not _is_year_row(rows[i]):
                    i += 1
                    continue
                header_j = _header_above(rows, i)
                if header_j is None:
                    i += 1
                    continue

                # First pass: gather the body rows (until the next header, year
                # row or note heading) so column geometry can be learned from the
                # figures themselves.
                k = i + 1
                body: list[list[dict]] = []
                while k < len(rows):
                    ktext = _row_text(rows[k])
                    if (
                        _is_year_row(rows[k])
                        or _MONTHS_RE.search(ktext)
                        or _looks_like_heading(ktext)
                        or _FOOTER_RE.search(ktext)
                    ):
                        break
                    body.append(rows[k])
                    k += 1

                year_words = [w for w in rows[i] if _is_year(w["text"])]
                columns = _build_columns(rows[header_j], year_words, body)
                if len(columns) < 2:
                    i = k
                    continue

                # Context around the header — the few rows above it plus the rows
                # down to the year row (the statement title sometimes sits between
                # the period header and the year row). Used both to skip tables
                # that aren't additive ₹ figures (share counts, option-pricing
                # grids) and to title the table.
                context = " ".join(
                    _row_text(rows[r]) for r in range(max(0, header_j - 3), i + 1)
                )
                if _EXCLUDE_TABLE_RE.search(context):
                    i = k
                    continue

                # The statement title may sit a row or two above the period
                # header, so title detection looks at the same context window.
                note, title = _table_note(note_at[header_j], context)
                table = PeriodTable(
                    page_index=page_index, note=note, title=title,
                    columns=columns, rows=[],
                )
                # Collect rows, tracking the sub-group headings above them (by
                # indent), so that line items repeated under several groups — the
                # ESOP grants table lists "Key Management Personnel (KMP)" once per
                # plan — can be told apart and matched to the right prior row.
                heads: dict[float, str] = {}
                collected = []
                for brow in body:
                    label, cells = _assign_cells(brow, columns)
                    x0 = brow[0]["x0"] if brow else 0.0
                    if not cells:
                        if label and not _looks_like_heading(label):
                            heads = {k: v for k, v in heads.items() if k < x0 - 3}
                            heads[x0] = label
                        continue
                    path = [heads[k] for k in sorted(heads) if k < x0 - 3]
                    collected.append((label, path, brow, cells))

                dupes = {lab for lab, n in
                         Counter(normalize_label(l) for l, _, _, _ in collected).items()
                         if n > 1}
                for label, path, brow, cells in collected:
                    if normalize_label(label) in dupes and path:
                        label = " · ".join([*path, label] if label else path)
                    table.rows.append(
                        TableRow(
                            label=label, page_index=page_index,
                            top=min(w["top"] for w in brow),
                            bottom=max(w["bottom"] for w in brow),
                            cells=cells,
                        )
                    )

                if table.rows:
                    tables.append(table)
                i = k
    return tables


# ---------------------------------------------------------------------------
# Matching line items across the two statements
# ---------------------------------------------------------------------------

# Per-share amounts, weighted-average share counts and option-pricing inputs are
# ratios / averages / point-in-time assumptions, not additive flows, so the
# three-plus-prior == year-to-date identity does not hold for them.
_NON_ADDITIVE_RE = re.compile(
    r"(?i)(per share|per equity share|earnings per|weighted average|in shares|"
    r"number of shares|shares outstanding|par value|\bbasic\b|\bdiluted\b|"
    r"exercise price|share price|expected volatility|expected life|expected term|"
    r"expected dividend|risk.?free|fair value of|weighted average fair value)"
)


def is_additive_label(label: str) -> bool:
    """False for per-share / share-count rows that must not be cast by addition."""
    return not _NON_ADDITIVE_RE.search(label)


# A whole table is skipped when its introducing sentence shows it is a point-in-
# time disclosure rather than additive flows: a share-count reconciliation or an
# option-pricing assumption grid. (The grants-made-during table IS additive —
# grants in the quarter plus the prior period equal the year-to-date — so it is
# cast, not skipped.)
_EXCLUDE_TABLE_RE = re.compile(
    r"(?i)(reconciliation of the number|number of (equity )?shares|"
    r"fair value of each|following assumptions|share price)")


def normalize_label(label: str) -> str:
    """Canonical form used to line a line item up across the two statements."""
    s = _FOOTNOTE_RE.sub(" ", label)
    s = s.replace("’", "'")
    s = _WS_RE.sub(" ", s).strip(" :.-")
    return s.lower()


def _base_label(label: str) -> str:
    # The line item without its sub-group qualifier, so two tables still pair
    # when one qualified a repeated label ("Current taxes · Domestic taxes") and
    # the other left it plain ("Domestic taxes").
    return normalize_label(label).split(" · ")[-1]


def _label_set(table: PeriodTable) -> set[str]:
    # Pair tables on their additive line items only — a table that merges a
    # castable block (e.g. stock-compensation expense) with a non-castable one
    # (option-pricing assumptions, whose labels carry period-specific numbers)
    # would otherwise score too low to pair with its prior-period twin.
    additive = {_base_label(r.label) for r in table.rows
                if r.label and is_additive_label(r.label)}
    return additive or {_base_label(r.label) for r in table.rows if r.label}


def _overlap(a: PeriodTable, b: PeriodTable) -> float:
    sa, sb = _label_set(a), _label_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _pair_tables(
    current: list[PeriodTable], prior: list[PeriodTable], min_overlap: float = 0.34
) -> list[tuple[PeriodTable, PeriodTable | None]]:
    """Pair each current table with the prior table it shares the most labels.

    Matching on the *content* (the set of line-item labels) rather than note
    numbers is robust to the messy, inconsistent heading text in real filings.
    Pairing is greedy and one-to-one: the highest-overlap pairs are taken first.
    A current table left without a counterpart is returned with ``None`` so it
    can be reported as un-castable.
    """
    candidates = []
    for ci, c in enumerate(current):
        for pi, p in enumerate(prior):
            score = _overlap(c, p)
            if score >= min_overlap:
                candidates.append((score, ci, pi))
    candidates.sort(reverse=True)

    matched_current: dict[int, int] = {}
    used_prior: set[int] = set()
    for score, ci, pi in candidates:
        if ci in matched_current or pi in used_prior:
            continue
        matched_current[ci] = pi
        used_prior.add(pi)

    return [
        (c, prior[matched_current[ci]] if ci in matched_current else None)
        for ci, c in enumerate(current)
    ]


def _match_rows(
    current: PeriodTable, prior: PeriodTable
) -> list[tuple[TableRow, TableRow | None]]:
    """Line current rows up with prior rows, by label then by position.

    Labels are matched first (a value column can have several rows with the same
    label — e.g. a repeated "Total" — so equal labels are consumed in order).
    Any current row still unmatched is then paired, in document order, with a
    prior row that no label claimed; this recovers items whose label wrapped or
    was dropped in one of the two PDFs.
    """
    buckets: dict[str, list[TableRow]] = {}
    for r in prior.rows:
        buckets.setdefault(normalize_label(r.label), []).append(r)
    cursor: dict[str, int] = {}
    matched_prior: set[int] = set()

    out: list[tuple[TableRow, TableRow | None]] = []
    for r in current.rows:
        key = normalize_label(r.label)
        bucket = buckets.get(key, [])
        idx = cursor.get(key, 0)
        match = bucket[idx] if (key and idx < len(bucket)) else None
        if match is not None:
            cursor[key] = idx + 1
            matched_prior.add(id(match))
        out.append((r, match))

    # Positional fallback for labels that wrapped or were dropped in one PDF —
    # but only when the two tables have the *same* unmatched rows in the same
    # order (a label glitch), never when their structure differs (a line item
    # added or renamed between quarters), which would force a wrong pairing.
    cur_unmatched = [(n, row) for n, (row, m) in enumerate(out) if m is None]
    pri_unmatched = [r for r in prior.rows if id(r) not in matched_prior]
    if len(cur_unmatched) == len(pri_unmatched):
        for (n, cur_row), cand in zip(cur_unmatched, pri_unmatched):
            a, b = normalize_label(cur_row.label), normalize_label(cand.label)
            if not a or not b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.5:
                out[n] = (cur_row, cand)
    return out


# ---------------------------------------------------------------------------
# The casting check
# ---------------------------------------------------------------------------

@dataclass
class CastCheck:
    note: str
    title: str
    label: str
    year: int
    page_index: int
    six_month: float | None          # year-to-date figure (the one verified)
    current_quarter: float | None    # current 3-month figure
    prior_quarter: float | None      # prior 3-month figure (from prior PDF)

    additive: bool = True            # False for per-share / share-count rows
    mode: str = "sum"                # how the six-month figure is reconciled:
                                     #   "sum"           -> 3M current + 3M prior
                                     #   "equal_current" -> equals 3M current
                                     #                      (closing balance, same date)
                                     #   "equal_prior"   -> equals 3M prior
                                     #                      (opening balance, same date)
    six_cell: Cell | None = None
    current_quarter_cell: Cell | None = None
    quarter_page_index: int | None = None   # page of the current-quarter cell
                                             # (differs from page_index for segments)

    @property
    def basis(self) -> str:
        return {
            "sum": "3M current + 3M prior",
            "equal_current": "= 3M current (same date)",
            "equal_prior": "= 3M prior (same date)",
        }.get(self.mode, "3M current + 3M prior")

    @property
    def expected(self) -> float | None:
        if self.mode == "equal_current":
            return self.current_quarter
        if self.mode == "equal_prior":
            return self.prior_quarter
        if self.current_quarter is None or self.prior_quarter is None:
            return None
        return self.current_quarter + self.prior_quarter

    @property
    def difference(self) -> float | None:
        if self.six_month is None or self.expected is None:
            return None
        return round(self.six_month - self.expected, 4)

    def status(self, tolerance: float) -> str:
        if not self.additive:
            return "not_additive"
        diff = self.difference
        if diff is None or self.six_month is None:
            return "unverified"
        if abs(diff) <= tolerance:
            return "ok"
        return "mismatch"


@dataclass
class CastResult:
    current_pdf: str
    prior_pdf: str
    tolerance: float
    checks: list[CastCheck]
    uncast_tables: list[PeriodTable]   # current tables with no prior match

    def by_status(self, status: str) -> list[CastCheck]:
        return [c for c in self.checks if c.status(self.tolerance) == status]

    @property
    def mismatches(self) -> list[CastCheck]:
        return self.by_status("mismatch")

    @property
    def consistent(self) -> bool:
        return not self.mismatches


def _checks_for_pair(
    current: PeriodTable, prior: PeriodTable | None
) -> list[CastCheck]:
    """Cast a current table's year-to-date column against 3M current + prior YTD.

    The current statement's long column may be six, nine or twelve months; the
    prior statement supplies the year-to-date column three months shorter (the
    half-year before a nine-month period, etc.). The current and prior period-end
    years can differ (a twelve-month "year ended March 2026" reconciles against a
    "nine months ended December 2025"), so the prior year-to-date columns are
    paired with the current ones by recency rank rather than by year value.
    """
    n = max((c.months for c in current.columns), default=0)
    if n <= 3:
        return []
    long_years = sorted({c.year for c in current.columns if c.months == n},
                        reverse=True)
    prior_ytd = (
        sorted([c for c in prior.columns if c.months == n - 3],
               key=lambda c: c.year, reverse=True)
        if prior is not None else []
    )

    checks: list[CastCheck] = []
    row_matches = (
        _match_rows(current, prior) if prior is not None
        else [(r, None) for r in current.rows]
    )
    for cur_row, prior_row in row_matches:
        for rank, year in enumerate(long_years):
            ytd = current.column(n, year)
            three = current.column(3, year)
            if ytd is None or three is None:
                continue
            six_cell = cur_row.cells.get(ytd.index)
            three_cell = cur_row.cells.get(three.index)
            if six_cell is None and three_cell is None:
                continue
            prior_col = prior_ytd[rank] if rank < len(prior_ytd) else None
            prior_cell = (
                prior_row.cells.get(prior_col.index)
                if (prior_row is not None and prior_col is not None)
                else None
            )
            # When the tables paired and the prior period-to-date column exists
            # but this line is absent there, the line *may* be nil in the prior
            # period (e.g. an interim dividend declared this quarter). Only trust
            # that when the year-to-date figure equals the current quarter: a line
            # that first appears this quarter has no earlier contribution, so its
            # YTD is exactly its current-quarter value and casting holds with a
            # prior of 0. If instead the YTD differs from the current quarter, the
            # prior figure is real but we simply failed to locate it (a relabelled
            # row, say) — leave it unverified rather than inventing a 0 that would
            # read as a false mismatch. With no prior column at all it is likewise
            # genuinely unverifiable.
            if prior_cell is not None:
                prior_quarter = prior_cell.value
            elif (prior is not None and prior_col is not None
                  and six_cell is not None and three_cell is not None
                  and six_cell.value == three_cell.value):
                prior_quarter = 0.0
            else:
                prior_quarter = None
            checks.append(
                CastCheck(
                    note=current.note,
                    title=current.title,
                    label=cur_row.label,
                    year=year,
                    page_index=cur_row.page_index,
                    six_month=six_cell.value if six_cell else None,
                    current_quarter=three_cell.value if three_cell else None,
                    prior_quarter=prior_quarter,
                    additive=is_additive_label(cur_row.label),
                    six_cell=six_cell,
                    current_quarter_cell=three_cell,
                )
            )
    return checks


def cast(
    current_pdf: str,
    prior_pdf: str,
    tolerance: float = 1.0,
) -> CastResult:
    """Cast a current-period statement against the prior-period statement.

    Works for any interim quarter: the current statement carries a three-month
    column and a year-to-date column (six, nine or twelve months — "year ended"),
    and the prior statement supplies the year-to-date column three months shorter
    (Q2 adds the prior three-month, Q3 the prior six-month, Q4 the prior
    nine-month). The two statements' period-end years may differ (a twelve-month
    "year ended March 2026" reconciles against "nine months ended December 2025"),
    so columns pair by recency, not by the year printed.

    Args:
        current_pdf: the current interim statement (3-month + year-to-date column).
        prior_pdf: the immediately preceding interim statement.
        tolerance: absolute slack (in the statement's units, e.g. ₹ crore) before
            a difference is reported as a mismatch. ``1.0`` absorbs the ±1
            rounding drift that is normal when quarters are rounded
            independently; set ``0`` for a strict, exact-match cast.
    """
    current_tables = [
        t for t in extract_period_tables(current_pdf)
        if any(c.months == 3 for c in t.columns)
        and any(c.months > 3 for c in t.columns)
    ]
    prior_tables = extract_period_tables(prior_pdf)

    checks: list[CastCheck] = []
    uncast: list[PeriodTable] = []
    for cur, prior in _pair_tables(current_tables, prior_tables):
        pair_checks = _checks_for_pair(cur, prior)
        if prior is None:
            uncast.append(cur)
        checks.extend(pair_checks)

    # Segment-reporting matrices (note 2.23) need their own two-line parser.
    from .segment import segment_checks
    checks.extend(segment_checks(current_pdf, prior_pdf))

    # Movement schedules (PP&E note 2.2, ROU note 2.19) cast flow lines and
    # reconcile opening/closing balances by date.
    from .schedule import schedule_checks
    checks.extend(schedule_checks(current_pdf, prior_pdf))

    return CastResult(
        current_pdf=current_pdf,
        prior_pdf=prior_pdf,
        tolerance=tolerance,
        checks=checks,
        uncast_tables=uncast,
    )
