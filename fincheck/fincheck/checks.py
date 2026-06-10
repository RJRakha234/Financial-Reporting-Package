"""Verify that totals and subtotals actually foot.

Real statements are flat (little reliable indentation), multi-period, and full of
*derived* subtotals (gross profit, operating profit, net profit) that are not
labelled "total". A naive "sum everything since the last total" model runs away
on them. Instead, for every total row we reconcile **backwards**: walk the
pending amounts most-recent-first and stop at the first contiguous group that
sums to the stated total (within a rounding tolerance). The matched group is then
rolled up into a single subtotal so a higher-level total sums subtotals rather
than the raw line items, and a subtotal mixed with a plain line (e.g.
``Total equity = Equity attributable + NCI``) still reconciles.

A total is flagged only when *no* trailing group reconciles to it — which is
exactly what a genuine footing error looks like.
"""

import re
from dataclasses import dataclass

from .extract import Cell, Page
from .numbers import format_number

# Words that mark a row as an explicit, additive total/subtotal.
_TOTAL_RE = re.compile(
    r"(?i)\b(?:sub-?\s*total|totals?|aggregate|sum\s+of|grand\s+total)\b"
)

# Derived subtotals are computed by subtraction (e.g. revenue - cost), not by
# footing a block of line items. They act as hard boundaries: a total's
# components must not be drawn from across one of these, because doing so means
# we have mis-identified the structure.
_DERIVED_RE = re.compile(
    r"(?i)\b(?:gross profit|gross margin|operating profit|operating margin|"
    r"profit before (?:tax|income tax)|profit after tax|"
    r"profit for the (?:year|period|quarter)|net profit|net income|ebitda)\b"
)

# How far back the reconciliation will look for components.
_MAX_LOOKBACK = 80


def is_total_row(label: str) -> bool:
    return bool(_TOTAL_RE.search(label or ""))


def is_derived_row(label: str) -> bool:
    return bool(_DERIVED_RE.search(label or ""))


@dataclass
class Inconsistency:
    page_index: int
    column: int
    label: str
    stated: float
    expected: float
    components: list[tuple[str, float]]
    cell: Cell

    @property
    def difference(self) -> float:
        return self.stated - self.expected

    def explanation(self) -> str:
        parts = " + ".join(
            f"{name.strip()[:24] or '?'} ({format_number(v)})"
            for name, v in self.components
        )
        return (
            f"Total does not foot. Stated {format_number(self.stated)}, "
            f"nearest sum of line items {format_number(self.expected)} "
            f"(off by {format_number(self.difference)}). "
            f"Components: {parts or 'no line items detected above this total'}."
        )


@dataclass
class TotalCheck:
    """A single total/subtotal the tool evaluated, with the figures it summed.

    ``status`` is ``"ok"`` (foots), ``"error"`` (does not foot), or
    ``"unverified"`` (a total whose components could not be identified — e.g. a
    cross-tabulated note). ``component_cells`` are the exact figures that were
    added together, so every checked number can be highlighted.
    """

    page_index: int
    column: int
    label: str
    status: str
    stated: float
    expected: float
    total_cell: Cell
    component_cells: list[Cell]
    component_detail: list[tuple[str, float]]

    @property
    def difference(self) -> float:
        return self.stated - self.expected

    def message(self) -> str:
        parts = " + ".join(
            f"{name.strip()[:24] or '?'} ({format_number(v)})"
            for name, v in self.component_detail
        )
        if self.status == "ok":
            return (
                f"Foots: {format_number(self.stated)} = {parts}."
                if parts
                else f"Foots: {format_number(self.stated)}."
            )
        if self.status == "unverified":
            return (
                f"Total {format_number(self.stated)}: components could not be "
                "isolated automatically (cross-tabulated table) — review manually."
            )
        return (
            f"Does NOT foot. Stated {format_number(self.stated)}, "
            f"sum of highlighted figures {format_number(self.expected)} "
            f"(off by {format_number(self.difference)}). Components: {parts}."
        )

    def as_inconsistency(self) -> Inconsistency:
        return Inconsistency(
            page_index=self.page_index,
            column=self.column,
            label=self.label,
            stated=self.stated,
            expected=self.expected,
            components=self.component_detail,
            cell=self.total_cell,
        )


@dataclass
class _Pending:
    value: float
    label: str
    is_subtotal: bool
    cell: Cell
    is_boundary: bool = False


def _tolerance(value: float, n_components: int, base: float) -> float:
    """Flat base plus rounding drift from summing already-rounded figures."""
    return base + n_components * 0.5 + abs(value) * 1e-9


def _reconcile(
    pending: list[_Pending], target: float, base: float
) -> list[_Pending] | None:
    """Return the trailing group of pending entries summing to ``target``.

    Walks most-recent-first and stops at the first prefix within tolerance, so a
    subtotal that mixes earlier subtotals with a couple of plain lines still
    reconciles. Returns ``None`` when nothing in the look-back window matches.
    """
    running = 0.0
    for depth in range(1, min(len(pending), _MAX_LOOKBACK) + 1):
        entry = pending[-depth]
        running += entry.value
        if abs(running - target) <= _tolerance(target, depth, base):
            return pending[-depth:]
        # A derived subtotal may itself be the deepest component (e.g.
        # Total comprehensive income = Net profit + Total OCI), but nothing
        # beyond it can be: include it in the test above, then stop.
        if entry.is_boundary:
            break
    return None


def _trailing_plain_run(pending: list[_Pending]) -> list[_Pending]:
    """Contiguous trailing plain items, back to the last subtotal/boundary."""
    run: list[_Pending] = []
    for entry in reversed(pending):
        if entry.is_subtotal or entry.is_boundary:
            break
        run.append(entry)
    run.reverse()
    return run


def _best_effort(pending: list[_Pending]) -> list[_Pending]:
    """Most plausible components for a total that did not reconcile.

    The contiguous run of plain line items just above the total, stopping at the
    last subtotal or derived-subtotal boundary; failing that, the trailing
    subtotals.
    """
    run: list[_Pending] = []
    for entry in reversed(pending):
        if entry.is_subtotal or entry.is_boundary:
            break
        run.append(entry)
    if run:
        run.reverse()
        return run
    subs = [e for e in pending if e.is_subtotal and not e.is_boundary]
    return subs[-2:] if subs else []


def _check_column(
    page: Page,
    column: int,
    base_tolerance: float,
    reconciled: set[tuple[float, float]] | None = None,
) -> list[TotalCheck]:
    """Evaluate every total in one column, returning a TotalCheck for each."""
    if reconciled is None:
        reconciled = set()
    checks: list[TotalCheck] = []
    pending: list[_Pending] = []

    def record(label, status, stated, expected, total_cell, comps):
        checks.append(
            TotalCheck(
                page_index=page.index,
                column=column,
                label=label,
                status=status,
                stated=stated,
                expected=expected,
                total_cell=total_cell,
                component_cells=[e.cell for e in comps],
                component_detail=[(e.label, e.value) for e in comps],
            )
        )

    for row in page.rows:
        if row.is_header:
            continue
        cell = row.cells.get(column)
        if cell is None:
            continue

        # Derived subtotals (operating profit, net profit, ...) are boundaries,
        # not footing checks: record them so nothing reconciles across them.
        if is_derived_row(row.label) and not is_total_row(row.label):
            pending.append(
                _Pending(cell.value, row.label, True, cell, is_boundary=True)
            )
            continue

        if not is_total_row(row.label):
            # Implicit (often unlabelled) subtotal: a line equal to the sum of
            # the contiguous items just above it — common in gross/tax/net
            # blocks and "items that will/won't be reclassified" groupings.
            # These are real checks too, so record them (their figures count as
            # "checked" and get highlighted), then roll them up.
            run = _trailing_plain_run(pending)
            if len(run) >= 2 and abs(sum(e.value for e in run) - cell.value) <= (
                _tolerance(cell.value, len(run), base_tolerance)
            ):
                label = row.label or "(implicit subtotal)"
                record(label, "ok", cell.value, sum(e.value for e in run), cell, run)
                run_ids = {id(e) for e in run}
                pending = [e for e in pending if id(e) not in run_ids]
                pending.append(_Pending(cell.value, label, True, cell))
            else:
                pending.append(_Pending(cell.value, row.label, False, cell))
            continue

        matched = _reconcile(pending, cell.value, base_tolerance)
        if matched is not None:
            record(
                row.label, "ok", cell.value,
                sum(e.value for e in matched), cell, matched,
            )
            consume = matched
            rolled = cell.value
            reconciled.add((round(cell.top), cell.value))
        else:
            components = _best_effort(pending)
            if len(components) < 2:
                # Components could not be isolated (e.g. a cross-tabulated note).
                record(row.label, "unverified", cell.value, cell.value, cell, [])
                pending.append(_Pending(cell.value, row.label, True, cell))
                continue
            expected = sum(e.value for e in components)
            record(row.label, "error", cell.value, expected, cell, components)
            consume = components
            # Roll up the reconciled (expected) figure so one misstatement does
            # not cascade into every higher-level total.
            rolled = expected

        consume_ids = {id(e) for e in consume}
        pending = [e for e in pending if id(e) not in consume_ids]
        pending.append(_Pending(rolled, row.label, True, cell))

    return checks


def run_checks(
    pages: list[Page], base_tolerance: float = 1.0
) -> tuple[list[Inconsistency], list[TotalCheck]]:
    """Return (inconsistencies, all total-checks) across every page."""
    all_checks: list[TotalCheck] = []
    for page in pages:
        page_checks: list[TotalCheck] = []
        reconciled: set[tuple[float, float]] = set()
        for column in range(page.n_columns):
            page_checks.extend(
                _check_column(page, column, base_tolerance, reconciled)
            )
        for check in page_checks:
            # Drop duplicate-column artefacts: the same total value on the same
            # row reconciled cleanly in another (split) column.
            if check.status == "error" and (
                (round(check.total_cell.top), check.stated) in reconciled
            ):
                continue
            all_checks.append(check)

    issues = [c.as_inconsistency() for c in all_checks if c.status == "error"]
    return issues, all_checks


def check_pages(pages: list[Page], base_tolerance: float = 1.0) -> list[Inconsistency]:
    return run_checks(pages, base_tolerance)[0]
