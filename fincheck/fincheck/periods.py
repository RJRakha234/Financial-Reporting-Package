"""Identify the reporting period each numeric column represents.

Rollforward checking compares the *same* period across two filings (e.g. the
comparative ``quarter ended 30 June 2025`` column in this quarter's results
against the figures originally published in last year's June quarter). To do
that we must label each column with the period it reports — its **end date** and
its **period type** (quarter / year / point-in-time, …).

Statements carry that information in stacked header rows::

                       Quarter ended                 Year ended
            30.06.2026   31.03.2026   30.06.2025      31.03.2026
            Unaudited    Audited      Unaudited       Audited

Dates are right-aligned under their figures, so a date's right edge lines up with
the column it heads. We therefore reconstruct every date in the header region
(``30.06.2026`` or ``June 30, 2025`` alike), map it to the nearest column by its
right edge, and attach the period-type phrase (``quarter ended`` …) by horizontal
proximity. Detection is best-effort: a column with no recognisable date is simply
left without a period and skipped by the comparison.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date

# Tokens that, preceding a month name, mark it as a period descriptor (so a
# day-less month such as "ended September" can be read as that month's end).
_DATE_CUES = {"ended", "ending", "at", "on", "of"}

from .extract import Page, _assign_column

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Period descriptors, longest token-sequence first so "three months ended" wins
# over a bare "ended". Each maps to a normalised period type.
_TYPE_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("three", "months", "ended"), "quarter"),
    (("3", "months", "ended"), "quarter"),
    (("twelve", "months", "ended"), "year"),
    (("12", "months", "ended"), "year"),
    (("six", "months", "ended"), "half-year"),
    (("nine", "months", "ended"), "nine-month"),
    (("half", "year", "ended"), "half-year"),
    (("quarter", "ended"), "quarter"),
    (("quarter", "ending"), "quarter"),
    (("year", "ended"), "year"),
    (("year", "ending"), "year"),
    (("period", "ended"), "period"),
    (("as", "at"), "point-in-time"),
    (("as", "on"), "point-in-time"),
    (("as", "of"), "point-in-time"),
]

_ORDINAL_RE = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)

# A date only counts as a column header when its right edge sits this close to a
# numeric column edge. Period dates are right-aligned under their figures, so a
# real header date aligns within a point or two; a date embedded in a title or
# narrative paragraph does not, and is ignored.
_ALIGN_TOL = 15.0


@dataclass(frozen=True)
class Period:
    """A reporting period attached to one numeric column."""

    column: int
    end_date: date | None
    period_type: str  # quarter | year | half-year | nine-month | period |
    #                   point-in-time | unknown
    raw: str = ""

    @property
    def key(self) -> tuple:
        """Identity used to match the same period across two filings.

        A figure for the *quarter ended* 31 Mar and the *year ended* 31 Mar are
        different numbers, so the type is part of the key — but only when known
        for both sides (handled in the comparison's fallback).
        """
        return (self.end_date, self.period_type)

    def describe(self) -> str:
        if self.end_date is None:
            return f"column {self.column} (period unknown)"
        label = {
            "quarter": "quarter ended",
            "year": "year ended",
            "half-year": "half-year ended",
            "nine-month": "nine months ended",
            "period": "period ended",
            "point-in-time": "as at",
        }.get(self.period_type, "")
        d = self.end_date.strftime("%d %b %Y")
        return f"{label} {d}".strip()


def parse_date(text: str) -> date | None:
    """Parse a financial-statement date, or ``None`` if the text is not one.

    Handles ``30 June 2025``, ``June 30, 2025``, ``30.06.2025`` / ``30-06-2025``
    / ``30/06/2025`` (day-first, as used in Indian/IFRS filings) and ISO
    ``2025-06-30``. Matches the whole string, so a run of two dates does not
    parse as one.
    """
    if not text:
        return None
    s = _ORDINAL_RE.sub("", text.strip().lower())
    s = s.replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None

    # "30 june 2025"
    m = re.fullmatch(r"(\d{1,2}) ([a-z]+) (\d{4})", s)
    if m and m.group(2) in _MONTHS:
        return _make(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))

    # "june 30 2025"
    m = re.fullmatch(r"([a-z]+) (\d{1,2}) (\d{4})", s)
    if m and m.group(1) in _MONTHS:
        return _make(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))

    # ISO "2025-06-30"
    m = re.fullmatch(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # "30.06.2025" day-first numeric
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        return _make(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    return None


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _clean(token: str) -> str:
    return token.strip().strip(".,;:").lower()


def _scan_dates(tokens: list[dict]) -> list[tuple[date, float]]:
    """Reconstruct dates from a row's tokens, returning (date, right_edge)."""
    out: list[tuple[date, float]] = []
    i, n = 0, len(tokens)
    while i < n:
        for span in (3, 2, 1):
            if i + span > n:
                continue
            chunk = tokens[i : i + span]
            d = parse_date(" ".join(t["text"] for t in chunk))
            if d is not None:
                out.append((d, chunk[-1]["x1"]))
                i += span
                break
        else:
            i += 1
    return out


def _is_year(text: str) -> int | None:
    t = _clean(text)
    if re.fullmatch(r"\d{4}", t) and 1900 <= int(t) <= 2099:
        return int(t)
    return None


def _scan_years(tokens: list[dict]) -> list[tuple[int, float]]:
    """Bare 4-digit year tokens, returning (year, right_edge)."""
    return [(_is_year(t["text"]), t["x1"]) for t in tokens if _is_year(t["text"])]


def _scan_partial_dates(tokens: list[dict]) -> list[tuple[int, int, float, float]]:
    """Month-and-day fragments lacking a year (e.g. ``September 30,`` or
    ``30 September``), returning (month, day, x0, x1).

    Statements often stack the period header: ``... ended September 30,`` on one
    row and the bare year (``2025``) right-aligned under each column on the next.
    These fragments supply the month/day to pair with those years.
    """
    out: list[tuple[int, int, float, float]] = []
    words = [_clean(t["text"]) for t in tokens]
    n = len(tokens)
    i = 0
    while i < n:
        if words[i] in _MONTHS and i + 1 < n and re.fullmatch(r"\d{1,2}", words[i + 1]):
            out.append((_MONTHS[words[i]], int(words[i + 1]),
                        tokens[i]["x0"], tokens[i + 1]["x1"]))
            i += 2
            continue
        if (
            re.fullmatch(r"\d{1,2}", words[i])
            and i + 1 < n
            and words[i + 1] in _MONTHS
        ):
            out.append((_MONTHS[words[i + 1]], int(words[i]),
                        tokens[i]["x0"], tokens[i + 1]["x1"]))
            i += 2
            continue
        # Day-less month inside a period descriptor ("ended September") -> use
        # the month's last day. Marked with day 0, resolved once the year known.
        if words[i] in _MONTHS and i > 0 and words[i - 1] in _DATE_CUES:
            out.append((_MONTHS[words[i]], 0, tokens[i]["x0"], tokens[i]["x1"]))
        i += 1
    return out


def _choose_month_day(
    partials: list[tuple[int, int, float, float]], x1: float
) -> tuple[int, int] | None:
    """Pick the month/day fragment that heads the column at right edge ``x1``."""
    if not partials:
        return None
    # Prefer fragments that carry an explicit day over day-less month-end ones.
    pool = [p for p in partials if p[1] != 0] or partials
    distinct = {(m, d) for m, d, _, _ in pool}
    if len(distinct) == 1:
        return (pool[0][0], pool[0][1])
    # Multiple fragments (e.g. two period groups): take the nearest by centre.
    m, d, _, _ = min(pool, key=lambda p: abs((p[2] + p[3]) / 2 - x1))
    return (m, d)


def _scan_types(tokens: list[dict]) -> list[tuple[str, float]]:
    """Find period-type phrases, returning (type, x_start of the phrase)."""
    words = [_clean(t["text"]) for t in tokens]
    hits: list[tuple[str, float]] = []
    i = 0
    while i < len(words):
        for phrase, ptype in _TYPE_PHRASES:
            k = len(phrase)
            if tuple(words[i : i + k]) == phrase:
                hits.append((ptype, tokens[i]["x0"]))
                i += k
                break
        else:
            i += 1
    return hits


def _type_for_column(edge: float, type_hits: list[tuple[str, float]]) -> str:
    """Type of the column at right edge ``edge``.

    Period descriptors are left-aligned group headers that govern the columns to
    their right, so a column takes the *last* descriptor that begins at or before
    it. (With a single descriptor this is just that type.)
    """
    if not type_hits:
        return "unknown"
    distinct = {t for t, _ in type_hits}
    if len(distinct) == 1:
        return next(iter(distinct))
    to_left = [th for th in type_hits if th[1] <= edge]
    chosen = max(to_left, key=lambda th: th[1]) if to_left else min(
        type_hits, key=lambda th: th[1]
    )
    return chosen[0]


def _is_header_candidate(row) -> bool:
    """Rows that may carry period information: explicit headers or any row with
    no numeric cells (a bare date/descriptor line is not parsed as data)."""
    return row.is_header or not row.cells


def detect_periods(page: Page) -> list[Period]:
    """Label each numeric column of ``page`` with its reporting period."""
    edges = page.column_edges
    if not edges:
        return []

    dates_by_col: dict[int, date] = {}
    type_hits: list[tuple[str, float]] = []
    aligned_rows: list[tuple[int, float, list[tuple[int, date]]]] = []
    for row in page.rows:
        if not _is_header_candidate(row):
            continue
        type_hits.extend(_scan_types(row.tokens))
        aligned: list[tuple[int, date]] = []
        for d, x1 in _scan_dates(row.tokens):
            col = _assign_column(x1, edges)
            if abs(edges[col] - x1) <= _ALIGN_TOL:
                aligned.append((col, d))
        if aligned:
            aligned_rows.append((len(aligned), row.top, aligned))

    # The genuine period-header row carries the most column-aligned dates; let it
    # populate first so a stray date elsewhere cannot override a real one.
    for _, _, aligned in sorted(aligned_rows, key=lambda r: (-r[0], r[1])):
        for col, d in aligned:
            dates_by_col.setdefault(col, d)

    # Fall back to stitching a bare-year row to a month/day fragment, for the
    # common "... ended September 30," / "2025  2024" stacked layout.
    if len(dates_by_col) < len(edges):
        partials: list[tuple[int, int, float, float]] = []
        years: list[tuple[int, float]] = []
        for row in page.rows:
            if not _is_header_candidate(row):
                continue
            partials.extend(_scan_partial_dates(row.tokens))
            years.extend(_scan_years(row.tokens))
        for year, x1 in years:
            col = _assign_column(x1, edges)
            if col in dates_by_col or abs(edges[col] - x1) > _ALIGN_TOL:
                continue
            md = _choose_month_day(partials, x1)
            if md is None:
                continue
            month, day = md
            if day == 0:  # day-less descriptor -> month end
                day = calendar.monthrange(year, month)[1]
            d = _make(year, month, day)
            if d is not None:
                dates_by_col[col] = d

    periods: list[Period] = []
    for col, edge in enumerate(edges):
        d = dates_by_col.get(col)
        if d is None:
            continue
        periods.append(
            Period(
                column=col,
                end_date=d,
                period_type=_type_for_column(edge, type_hits),
                raw=d.isoformat(),
            )
        )
    return periods
