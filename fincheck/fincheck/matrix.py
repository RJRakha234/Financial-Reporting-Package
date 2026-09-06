"""Reconcile *stacked* multi-period note tables (e.g. segment reporting).

Some notes are matrices: the columns are a secondary dimension (business
segments, asset classes) and each metric is shown as a labelled row for the
latest period followed by one or more **unlabelled** rows for the comparative
periods::

    Three months ended September 30, 2025 and September 30, 2024
                              FinSvcs   Mfg   ...   Total
    Revenue                   24,116  14,151  ...  86,769     <- Sep 30, 2025
                              21,971  12,201  ...  80,300     <- Sep 30, 2024

Such a table can't be matched cell-by-cell by period column (the columns aren't
periods). But the comparative is still a verbatim copy of the prior filing's
current-period figures, so we reconcile **per row, as a value multiset**: for a
given metric and period, the set of numbers across all segment columns must be
identical between the two filings — independent of column order or segment
identification.
"""

import re
from dataclasses import dataclass
from datetime import date

from .periods import parse_date
from .rollforward import _normalize, _section_of

# Period descriptor in a table title, mapped to a period type.
_TITLE_TYPE = [
    (r"three months ended|3 months ended", "quarter"),
    (r"six months ended|half[- ]year ended", "half-year"),
    (r"nine months ended", "nine-month"),
    (r"twelve months ended|year ended", "year"),
    (r"quarter ended", "quarter"),
    (r"as at|as on|as of", "point-in-time"),
]

# Dates inside a title line: "September 30, 2025", "30 September 2025",
# "31.03.2025" / "31-03-2025" / "2025-03-31".
_DATE_IN_TEXT = re.compile(
    r"(?i)(?:[A-Za-z]+\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+[A-Za-z]+\s+\d{4}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{4}"
    r"|\d{4}[./-]\d{1,2}[./-]\d{1,2})"
)


def parse_title_periods(text: str) -> tuple[str, list[date]] | None:
    """From a table title, return (period_type, [dates newest-first]) or None."""
    low = text.lower()
    ptype = next((t for pat, t in _TITLE_TYPE if re.search(pat, low)), None)
    if ptype is None:
        return None
    dates = []
    for m in _DATE_IN_TEXT.finditer(text):
        d = parse_date(m.group(0))
        if d is not None and d not in dates:
            dates.append(d)
    return (ptype, dates) if dates else None


def _row_text(row) -> str:
    return " ".join(t["text"] for t in (row.tokens or []))


def _is_datepart(v) -> bool:
    return (1900 <= v <= 2099) or (float(v).is_integer() and 1 <= v <= 31)


def title_of(row) -> tuple[str, list[date]] | None:
    """Period title a row announces, or None.

    A genuine title carries no real figures — only the date parts (a day like
    "30" or a year like "2025"). This distinguishes the segment title
    "Three months ended September 30, 2025 and September 30, 2024" from a
    movement-table data row such as "Gross carrying value as at April 1, 2025
    1,477 11,721 …", which also says "as at <date>" but holds real numbers.
    """
    t = parse_title_periods(_row_text(row))
    if t is None:
        return None
    if any(not _is_datepart(c.value) for c in row.cells.values()):
        return None
    return t


class _SectionTracker:
    """Track the current section the way ``_figures_from_pages`` does.

    Numbered notes (``2.4``) win; a primary-statement title is honoured only
    before the notes begin, so prose in a note that references "the consolidated
    statement of comprehensive income" doesn't masquerade as a heading.
    """

    def __init__(self):
        self.section = ""
        self._seen_statement = False
        self._in_notes = False

    def feed(self, row) -> bool:
        heading = _section_of(row)
        if heading is None:
            return False
        if heading[0].isdigit():
            if self._seen_statement:
                self._in_notes = True
            self.section = heading
        elif not self._in_notes:
            self.section = heading
            self._seen_statement = True
        return True


def _started_pages(pages):
    """Yield pages from the first one carrying data, skipping cover/contents."""
    started = False
    for page in pages:
        if not started:
            if not any(c for r in page.rows for c in r.cells):
                continue
            started = True
        yield page


@dataclass
class MatrixRow:
    section: str
    metric: str
    norm_metric: str
    end_date: date
    period_type: str
    values: tuple  # sorted multiset of the row's figures
    page_index: int
    cell: object


def _all_years(values) -> bool:
    return bool(values) and all(
        float(v).is_integer() and 1900 <= v <= 2099 for v in values
    )


def stacked_rows(pages, allowed: set | None = None) -> list[MatrixRow]:
    """Extract per-metric, per-period value multisets from *stacked* tables.

    A stacked metric is a labelled data row immediately followed by one
    unlabelled data row with the same number of figures (the latest period then
    its comparative). Only sections in ``allowed`` are considered, and header /
    bare-year rows are ignored.
    """
    out: list[MatrixRow] = []
    tracker = _SectionTracker()
    title: tuple[str, list[date]] | None = None

    def usable(row):
        if row.is_header or not row.cells:
            return None
        vals = [round(c.value, 2) for c in row.cells.values()]
        if _all_years(vals):
            return None
        return vals

    for page in _started_pages(pages):
        rows = page.rows
        i = 0
        while i < len(rows):
            row = rows[i]
            if tracker.feed(row):
                i += 1
                continue
            t = title_of(row)
            if t is not None and len(t[1]) >= 2:
                title = t
                i += 1
                continue
            section = tracker.section
            if (
                title is None
                or (allowed is not None and section not in allowed)
                or not row.label.strip()
            ):
                i += 1
                continue
            cur_vals = usable(row)
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            nxt_vals = usable(nxt) if nxt is not None and not nxt.label.strip() else None
            # A clean two-period metric: labelled row + unlabelled row, same width.
            if cur_vals is not None and nxt_vals is not None and len(cur_vals) == len(
                nxt_vals
            ):
                norm = _normalize(row.label)
                cell = next(iter(row.cells.values()))
                for sub, vals in ((0, cur_vals), (1, nxt_vals)):
                    if sub < len(title[1]):
                        out.append(MatrixRow(
                            section=section, metric=row.label.strip(),
                            norm_metric=norm, end_date=title[1][sub],
                            period_type=title[0], values=tuple(sorted(vals)),
                            page_index=page.index, cell=cell,
                        ))
                i += 2
                continue
            i += 1
    return out


def block_rows(pages, allowed: set | None = None) -> list[MatrixRow]:
    """Extract rows from single-period *block* tables.

    Some matrix notes repeat the whole grid under a one-date title — financial
    instruments "as at March 31, 2025", a property/lease movement "for the six
    months ended September 30, 2024". Every labelled row in such a block is a
    line item whose figures across the category/asset columns form a multiset
    for that one period.
    """
    out: list[MatrixRow] = []
    tracker = _SectionTracker()
    period: tuple[str, date] | None = None
    for page in _started_pages(pages):
        for row in page.rows:
            if tracker.feed(row):
                continue
            t = title_of(row)
            if t is not None:
                # One date => a block we can reconcile; two dates => a stacked
                # table, handled elsewhere, so clear the block context.
                period = (t[0], t[1][0]) if len(t[1]) == 1 else None
                continue
            section = tracker.section
            if (
                period is None
                or (allowed is not None and section not in allowed)
                or row.is_header
                or not row.cells
                or not row.label.strip()
            ):
                continue
            vals = [round(c.value, 2) for c in row.cells.values()]
            if _all_years(vals):
                continue
            out.append(MatrixRow(
                section=section, metric=row.label.strip(),
                norm_metric=_normalize(row.label), end_date=period[1],
                period_type=period[0], values=tuple(sorted(vals)),
                page_index=page.index, cell=next(iter(row.cells.values())),
            ))
    return out


@dataclass
class MatrixCheck:
    status: str  # "ok" | "mismatch"
    section: str
    metric: str
    period_desc: str
    current_values: tuple
    prior_values: tuple
    page_index: int
    source: str
    cell: object = None


def reconcile_matrix(
    current_pages,
    prior_named_pages: list[tuple[str, list]],
    tolerance: float,
    allowed: set | None = None,
) -> tuple[list[MatrixCheck], list[MatrixCheck]]:
    """Reconcile stacked tables in ``allowed`` sections.

    Returns (ok_checks, review_checks). Each stacked row is reconciled as a value
    multiset against the prior filing; rows that match are validated, rows that
    don't are surfaced for manual review (matrix extraction is noisy, so a
    non-match is flagged to look at, not asserted as an error).
    """
    def occurts(rows):
        """Assign an occurrence index within (section, metric, period)."""
        seen: dict[tuple, int] = {}
        out = []
        for r in rows:
            k = (r.section, r.norm_metric, r.end_date, r.period_type)
            seen[k] = seen.get(k, 0) + 1
            out.append((k + (seen[k],), r))
        return out

    cur = occurts(
        stacked_rows(current_pages, allowed) + block_rows(current_pages, allowed)
    )
    prior_index: dict[tuple, tuple[str, MatrixRow]] = {}
    for name, pages in prior_named_pages:
        rows = stacked_rows(pages, allowed) + block_rows(pages, allowed)
        for key, r in occurts(rows):
            prior_index.setdefault(key, (name, r))

    def nonzero(values):
        # Category matrices pad rows with a varying number of nil ("-") columns;
        # the meaningful content is the non-zero figures.
        return tuple(sorted(v for v in values if v != 0))

    ok: list[MatrixCheck] = []
    review: list[MatrixCheck] = []
    for key, c in cur:
        if key not in prior_index:
            continue
        name, p = prior_index[key]
        cv, pv = nonzero(c.values), nonzero(p.values)
        same = len(cv) == len(pv) and all(
            abs(a - b) <= tolerance for a, b in zip(cv, pv)
        )
        check = MatrixCheck(
            status="ok" if same else "review",
            section=c.section, metric=c.metric,
            period_desc=f"{c.period_type} ended {c.end_date:%d %b %Y}",
            current_values=c.values, prior_values=p.values,
            page_index=c.page_index, source=name, cell=c.cell,
        )
        (ok if same else review).append(check)
    return ok, review
