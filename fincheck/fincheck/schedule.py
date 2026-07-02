"""Casting of movement schedules — property, plant & equipment (note 2.2) and
right-of-use assets (note 2.19).

These are roll-forward schedules whose columns are *asset categories* (Land,
Buildings, …, Total), not periods. The current statement carries four of them
per note — three-month and six-month, each for the current and comparative year
— and the prior statement carries the earlier three-month versions. Each row is
cast according to what it represents:

* **flow lines** (additions, deletions, depreciation, translation, …) add up:
  ``six-month == three-month current + three-month prior``;
* a **closing balance** (period-end date) is a point in time, so the six-month
  and current-quarter schedules must show the *same* number;
* an **opening balance** equals the prior-quarter schedule's opening (both are
  the start-of-year balance).

A balance row carries its date inline ("… as at July 1, 2025 599 …"); the day
and year are parsed as numbers but sit far left of the value columns, so the
position-based column assignment drops them automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .casting import (
    Cell, _NOTE_RE, _NUMWORD, _looks_like_heading, _pdf_rows, _row_text)
from .numbers import parse_number

_SCHED_RE = re.compile(
    r"(?i)changes in the carrying value of (right-of-use|property)")
# "... for the three months ended ..." / "... nine months ended ..." /
# "... year ended ..." — the schedule's period, current quarter or year-to-date.
_PERIOD_RE = re.compile(
    r"(?i)(?:(three|six|nine|twelve)\s+months?|year)\s+ended\s+"
    r"([A-Za-z]+)\s+\d{1,2},?\s*(\d{4})")
_BALANCE_RE = re.compile(r"(?i)\bas\s+(?:at|of)\b")
_MONTH_RE = re.compile(
    r"(?i)\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b")
_FOOT_RE = re.compile(r"\((?:refer[^)]*|\d+)\)|\*+|#|\(refer.*", re.I)
_WS = re.compile(r"\s+")

_MIN_WIDE = 3            # a flow row has at least this many figures
_ASSIGN_TOL = 16.0

# Movement lines recognised in a roll-forward; rows that are neither one of these
# nor a dated balance row are not part of the schedule (narrative, a following
# lease-liability table, …) and are skipped.
_FLOW_VOCAB = (
    "addition", "deletion", "disposal", "depreciation", "amortis", "amortiz",
    "translation", "exchange", "impairment", "retire", "reclassif", "transfer",
    "business combination", "adjustment", "revaluation", "written off",
    "write-off", "borrowing cost", "interest capital",
)


def _centre(w) -> float:
    return (w["x0"] + w["x1"]) / 2


def _flowkey(label: str) -> str:
    """Canonical key for a movement line, robust to wording differences.

    The same line is often phrased differently across statement types or quarters
    ("Additions on Business Combinations" vs "Additions Business Combination
    (Refer ...)"), so movements are keyed by category, not by their exact text.
    Order matters: a business-combination addition contains "addition", and the
    accumulated-depreciation-on-deletions line contains "depreciation", so the
    more specific categories are tested first.
    """
    low = _FOOT_RE.sub(" ", label).lower()
    if "business combination" in low:
        return "business combination"
    if "deletion" in low or "disposal" in low or "retire" in low:
        return "deletions"
    if "depreciation" in low or "amortis" in low or "amortiz" in low:
        return "depreciation"
    if "translation" in low or "exchange" in low:
        return "translation"
    if "impairment" in low:
        return "impairment"
    if "addition" in low:
        return "additions"
    return _WS.sub(" ", low).strip(" :.-")


def _section_of(label: str) -> str | None:
    """The roll-forward block a balance row belongs to, or None if the row is not
    a recognisable schedule balance (so unrelated 'as at' lines are ignored)."""
    low = label.lower()
    if "gross carrying value" in low or "gross block" in low:
        return "gross"
    if "accumulated depreciation" in low or "accumulated amortis" in low:
        return "accdep"
    if "carrying value" in low or "net block" in low:
        return "net"
    if "balance" in low:
        return "balance"
    return None


def _is_flow(label: str) -> bool:
    low = label.lower()
    return any(v in low for v in _FLOW_VOCAB)


@dataclass
class Schedule:
    kind: str            # "PPE" or "ROU"
    note: str            # "2.2" or "2.19"
    months: int          # 3 or 6
    year: int
    end_month: str       # lower-case period-end month, e.g. "september"
    page_index: int
    centres: list[float]
    names: list[str]
    # (section, rowkey) -> {column index: Cell}
    cells: dict[tuple, dict[int, Cell]] = field(default_factory=dict)
    labels: dict[tuple, str] = field(default_factory=dict)


def _mk_cell(w, page_index=None) -> Cell:
    return Cell(value=parse_number(w["text"]), text=w["text"].strip(),
                x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
                page_index=page_index)


def _assign(row, centres, page_index=None) -> dict[int, Cell]:
    cells: dict[int, Cell] = {}
    for w in row:
        if parse_number(w["text"]) is None:
            continue
        c = _centre(w)
        i = min(range(len(centres)), key=lambda k: abs(centres[k] - c))
        if abs(centres[i] - c) <= _ASSIGN_TOL and i not in cells:
            cells[i] = _mk_cell(w, page_index)
    return cells


def _column_names(name_rows, centres) -> list[str]:
    buckets: list[list[tuple]] = [[] for _ in centres]
    for row in name_rows:
        for w in row:
            if parse_number(w["text"]) is not None:
                continue
            tok = _FOOT_RE.sub("", w["text"]).strip()
            if len(tok) < 2 or tok.lower() in {
                "particulars", "category", "of", "asset", "assets", "and",
                "in", "crore", "crore)", "(in", "(in₹", "₹", "rou",
            }:
                continue
            c = _centre(w)
            i = min(range(len(centres)), key=lambda k: abs(centres[k] - c))
            if abs(centres[i] - c) <= 24:
                buckets[i].append((w["top"], w["x0"], tok))
    out = []
    for i, items in enumerate(buckets):
        items.sort()
        text = " ".join(t for _, _, t in items).strip(" ,")
        out.append(text or ("Total" if i == len(centres) - 1 else f"Col {i + 1}"))
    return out


def _schedule_note(heading: str, kind: str) -> str | None:
    """The note number the schedule sits under, if its heading matches the kind."""
    m = _NOTE_RE.match(heading or "")
    if not m:
        return None
    title = m.group(2).lower()
    if kind == "PPE" and any(w in title for w in ("property", "plant", "equipment")):
        return m.group(1)
    if kind == "ROU" and any(w in title for w in ("right-of-use", "right of use", "lease")):
        return m.group(1)
    return None


def extract_schedules(pdf_path: str) -> list[Schedule]:
    schedules: list[Schedule] = []
    # Flatten every page into one row stream so a schedule that spills over a
    # page break — its closing balances continuing at the top of the next page —
    # is still read as a single table rather than being cut off at the page edge.
    rows: list[list[dict]] = []
    page_of: list[int] = []
    for page_index, page_rows in enumerate(_pdf_rows(pdf_path)):
        for row in page_rows:
            rows.append(list(row))
            page_of.append(page_index)
    note_at = [""] * len(rows)
    current_note = ""        # running note heading, carried across pages
    for idx, row in enumerate(rows):
        t = _row_text(row)
        if _looks_like_heading(t):
            current_note = t
        note_at[idx] = current_note
    if True:
        if True:
            i = 0
            while i < len(rows):
                m = _SCHED_RE.search(_row_text(rows[i]))
                if not m:
                    i += 1
                    continue
                page_index = page_of[i]
                kind = "ROU" if m.group(1).lower().startswith("right") else "PPE"
                # Prefer the actual note number from the heading (IFRS numbers
                # differ from Ind AS); fall back to the Ind AS default.
                note = _schedule_note(note_at[i], kind) or ("2.19" if kind == "ROU"
                                                            else "2.2")

                # The period descriptor may spill onto the next row or two.
                window = " ".join(_row_text(rows[j])
                                  for j in range(i, min(i + 3, len(rows))))
                pm = _PERIOD_RE.search(window)
                if not pm:
                    i += 1
                    continue
                months = _NUMWORD[pm.group(1).lower()] if pm.group(1) else 12
                end_month = pm.group(2).lower()
                year = int(pm.group(3))

                # Name band up to the first wide (flow/balance) data row.
                j = i + 1
                name_rows = []
                while j < len(rows):
                    figs = [w for w in rows[j]
                            if parse_number(w["text"]) is not None]
                    if len(figs) >= _MIN_WIDE:
                        break
                    name_rows.append(rows[j])
                    j += 1
                if j >= len(rows):
                    i = j
                    continue

                # Columns are taken from the first recognised *flow* row (those
                # have no date tokens, so the right edges are clean).
                flow_centres: list[float] = []
                k = j
                while k < len(rows):
                    t = _row_text(rows[k])
                    if _SCHED_RE.search(t) or _PERIOD_RE.search(t):
                        break
                    label = " ".join(w["text"] for w in rows[k]
                                     if parse_number(w["text"]) is None).strip()
                    figs = [w for w in rows[k]
                            if parse_number(w["text"]) is not None]
                    if len(figs) >= _MIN_WIDE and _is_flow(label):
                        flow_centres = sorted(_centre(w) for w in figs)
                        break
                    k += 1
                if not flow_centres:
                    i = j
                    continue
                centres = flow_centres
                names = _column_names(name_rows, centres)

                sched = Schedule(kind=kind, note=note, months=months, year=year,
                                 end_month=end_month, page_index=page_index,
                                 centres=centres, names=names)

                section = "balance"
                k = j
                while k < len(rows):
                    t = _row_text(rows[k])
                    # Stop at the next schedule, the next period block, or the
                    # next numbered-note heading — the roll-forward has ended.
                    # (The heading stop matters now that a table may run past a
                    # page break: without a page edge to halt it, it would
                    # otherwise wander into the following note's narrative.)
                    if (_SCHED_RE.search(t) or _PERIOD_RE.search(t)
                            or _looks_like_heading(t)):
                        break
                    label = " ".join(w["text"] for w in rows[k]
                                     if parse_number(w["text"]) is None).strip()
                    figs = [w for w in rows[k]
                            if parse_number(w["text"]) is not None]
                    if figs and label:
                        is_balance = bool(_BALANCE_RE.search(label)
                                          and _MONTH_RE.search(label))
                        balance_section = _section_of(label) if is_balance else None
                        # A movement line's label is terse ("Additions",
                        # "Translation difference"); a narrative sentence that
                        # merely contains a flow word ("… securities were
                        # transferred …") is not a schedule row, so cap the length.
                        clean = _FOOT_RE.sub("", label).strip()
                        if balance_section is not None:
                            section = balance_section
                            month = _MONTH_RE.search(label).group(1).lower()
                            kind_row = "close" if month == end_month else "open"
                            key = (section, kind_row)
                        elif not is_balance and _is_flow(label) and len(clean) <= 55:
                            key = (section, _flowkey(label))
                        else:
                            key = None          # narrative / unrelated table row
                        if key is not None:
                            cells = _assign(rows[k], centres, page_of[k])
                            if cells:
                                sched.cells[key] = cells
                                sched.labels.setdefault(key, label)
                    k += 1

                if sched.cells:
                    schedules.append(sched)
                i = k
    return schedules


def _pick(scheds, kind, months, year):
    for s in scheds:
        if s.kind == kind and s.months == months and s.year == year:
            return s
    return None


_PRETTY_SECTION = {"gross": "Gross block", "accdep": "Accumulated depreciation",
                   "net": "Net block", "balance": ""}


def _row_label(section, rowkey, raw):
    if rowkey == "open":
        text = "Opening balance"
    elif rowkey == "close":
        text = "Closing balance"
    else:
        text = raw
    sec = _PRETTY_SECTION.get(section, "")
    return f"{sec} · {text}" if sec else text


def schedule_checks(current_pdf: str, prior_pdf: str):
    """Casting checks for the PP&E and ROU movement schedules."""
    from .casting import CastCheck

    cur = extract_schedules(current_pdf)
    pri = extract_schedules(prior_pdf)
    checks: list[CastCheck] = []

    for kind in ("PPE", "ROU"):
        # The current statement's longest schedule is its year-to-date one
        # (6/9/12 months); the prior statement supplies the one three months
        # shorter. Current and prior period-end years can differ (a year ended
        # March vs nine months ended December), so they pair by recency rank.
        n = max((s.months for s in cur if s.kind == kind), default=0)
        if n <= 3:
            continue
        long_scheds = sorted([s for s in cur if s.kind == kind and s.months == n],
                             key=lambda s: s.year, reverse=True)
        prior_ytd = sorted([s for s in pri if s.kind == kind and s.months == n - 3],
                           key=lambda s: s.year, reverse=True)
        for rank, six in enumerate(long_scheds):
            year = six.year
            cur3 = _pick(cur, kind, 3, year)
            pri3 = prior_ytd[rank] if rank < len(prior_ytd) else None
            if not (six and cur3 and pri3):
                continue
            note = six.note          # the note number detected for this schedule
            title = ("Property, plant & equipment schedule" if kind == "PPE"
                     else "Right-of-use assets schedule")
            for key, cells6 in six.cells.items():
                section, rowkey = key
                raw = six.labels.get(key, rowkey)
                for col, cell6 in cells6.items():
                    name = six.names[col] if col < len(six.names) else f"col{col}"
                    label = f"{_row_label(section, rowkey, raw)} — {name}"
                    base = dict(note=note, title=title, label=label, year=year,
                                page_index=(cell6.page_index
                                            if cell6.page_index is not None
                                            else six.page_index),
                                six_month=cell6.value,
                                six_cell=cell6, additive=True)
                    if rowkey == "close":
                        c3 = cur3.cells.get(key, {}).get(col)
                        q_page = (c3.page_index if c3 and c3.page_index is not None
                                  else cur3.page_index)
                        checks.append(CastCheck(
                            mode="equal_current",
                            current_quarter=c3.value if c3 else None,
                            prior_quarter=None,
                            current_quarter_cell=c3,
                            quarter_page_index=q_page, **base))
                    elif rowkey == "open":
                        p3 = pri3.cells.get(key, {}).get(col)
                        checks.append(CastCheck(
                            mode="equal_prior",
                            current_quarter=None,
                            prior_quarter=p3.value if p3 else None, **base))
                    else:  # flow
                        c3 = cur3.cells.get(key, {}).get(col)
                        p3 = pri3.cells.get(key, {}).get(col)
                        # A movement line absent from a quarter altogether is nil
                        # for that quarter (0); but a line that IS present with a
                        # gap in this one column is an extraction gap, so leave it
                        # unverified rather than fabricate a zero and mis-flag it.
                        cur_val = (c3.value if c3 else
                                   (0.0 if key not in cur3.cells else None))
                        pri_val = (p3.value if p3 else
                                   (0.0 if key not in pri3.cells else None))
                        q_page = (c3.page_index if c3 and c3.page_index is not None
                                  else cur3.page_index)
                        checks.append(CastCheck(
                            mode="sum",
                            current_quarter=cur_val,
                            prior_quarter=pri_val,
                            current_quarter_cell=c3,
                            quarter_page_index=q_page, **base))
    return checks
