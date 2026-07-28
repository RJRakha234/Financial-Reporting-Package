"""Controlled rounding of a table whose totals must keep footing.

The problem
-----------
Round every figure in a table independently and the totals stop adding up:
three lines of 33.33 round to 33 each, but their total of 99.99 rounds to 100.
Forcing the total to 99 instead is worse — now it disagrees with the figure
readers expect. *Controlled rounding* resolves this by choosing, for each cell
at once, whether it goes down or up, so that

* every cell lands on one of the two multiples of the step either side of its
  true value (nothing moves by a whole step or more, nothing changes sign
  gratuitously), **and**
* every total, subtotal, row total, column total and grand total is exactly
  the sum of its rounded components, in both directions at once, **and**
* the total distance moved is the smallest possible.

How
---
For a block of rows against columns that is exactly a flow network
(:mod:`finround.flow`): a unit of flow out of row *i* and into column *j* is a
unit in cell *(i, j)*, conservation at the nodes is the row and column totals
footing, and arc bounds ``[floor, ceil]`` are the "stay adjacent" rule. Network
integrality guarantees a solution exists whenever the un-rounded table does,
and minimising cost picks the best one.

Nested subtotals turn that single block into a cascade. Blocks are solved
outermost-first, in order of depth; whatever an outer block decided for a cell
is *fixed* when the inner block that refines it is solved. Every cell of the
table — including subtotal-by-subtotal crossings — is the interior of exactly
one block, so each figure is decided once and every constraint is honoured by
construction rather than patched up afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import lcm

from .flow import Circulation
from .structure import Node, build_forest, detect_groups, walk
from .table import Table
from .units import RoundingSpec, ceil_div, floor_div, nearest

MAX_SLACK = 3


@dataclass(frozen=True)
class _Span:
    """Bounds for one quantity, plus the true value it is trying to stay near."""

    lo: int
    hi: int
    target: Fraction | None = None  # None: no preference, any value in range

    @classmethod
    def fixed(cls, value: int) -> "_Span":
        return cls(value, value)

    @classmethod
    def adjacent(cls, target: Fraction, slack: int = 0) -> "_Span":
        return cls(floor_div(target) - slack, ceil_div(target) + slack, target)

    @classmethod
    def free(cls, lo: int, hi: int) -> "_Span":
        return cls(lo, hi)


def _add_span(net: Circulation, u: int, v: int, span: _Span, epsilon: Fraction) -> list[int]:
    """Wire a quantity into the network as arcs; returns ids to read it back.

    A span with a target becomes one unit-capacity arc per step, each priced at
    the extra distance that step costs. The prices increase, so the solver
    always spends the cheap steps first and the piecewise cost stays convex —
    which is what lets a linear network solve an absolute-deviation objective.

    ``epsilon`` prices a second, far smaller preference for the plain nearest
    value. It settles ties — a figure ending in exactly half, or a choice
    between two equally good cells — the way an accountant would, without ever
    outweighing the real objective (see :func:`_epsilon`).
    """
    ids = [net.add_edge(u, v, span.lo, span.lo, 0)]
    if span.target is None:
        ids.append(net.add_edge(u, v, 0, span.hi - span.lo, 0))
        return ids
    plain = nearest(span.target)
    for step in range(span.lo, span.hi):
        cost = abs(Fraction(step + 1) - span.target) - abs(Fraction(step) - span.target)
        cost += epsilon * (abs(step + 1 - plain) - abs(step - plain))
        ids.append(net.add_edge(u, v, 0, 1, cost))
    return ids


def _epsilon(spans: list[_Span]) -> Fraction:
    """A tie-break weight too small to change any genuinely better rounding.

    Every primary cost is a multiple of ``1/d``, where ``d`` is the common
    denominator of the figures involved, so two roundings that differ at all
    differ by at least ``1/d``. Keeping the total tie-break weight strictly
    below ``1/d`` therefore leaves the ranking of distinct roundings untouched.
    """
    denominator = 1
    steps = 0
    for span in spans:
        if span.target is None:
            continue
        denominator = lcm(denominator, span.target.denominator)
        steps += span.hi - span.lo
    return Fraction(1, denominator * (2 * steps + 1))


def _read(net: Circulation, ids: list[int]) -> int:
    return sum(net.flow(i) for i in ids)


def _solve_block(
    cells: list[list[_Span]],
    rows: list[_Span],
    cols: list[_Span],
    corner: _Span,
) -> tuple[list[list[int]], list[int], list[int], int] | None:
    """Round one rectangular block; None when no rounding can satisfy it."""
    n_rows, n_cols = len(cells), len(cells[0])
    source, sink = n_rows + n_cols, n_rows + n_cols + 1
    net = Circulation(n_rows + n_cols + 2)
    eps = _epsilon([s for row in cells for s in row] + rows + cols + [corner])

    row_ids = [_add_span(net, source, i, rows[i], eps) for i in range(n_rows)]
    cell_ids = [
        [_add_span(net, i, n_rows + j, cells[i][j], eps) for j in range(n_cols)]
        for i in range(n_rows)
    ]
    col_ids = [_add_span(net, n_rows + j, sink, cols[j], eps) for j in range(n_cols)]
    corner_ids = _add_span(net, sink, source, corner, eps)

    if not net.solve():
        return None
    return (
        [[_read(net, cell_ids[i][j]) for j in range(n_cols)] for i in range(n_rows)],
        [_read(net, ids) for ids in row_ids],
        [_read(net, ids) for ids in col_ids],
        _read(net, corner_ids),
    )


@dataclass
class RoundedTable:
    """The result: rounded figures plus everything needed to justify them."""

    table: Table
    spec: RoundingSpec
    units: list[list[int]]
    naive_units: list[list[int]]
    row_groups: dict[int, list[int]]
    col_groups: dict[int, list[int]]
    warnings: list[str] = field(default_factory=list)
    slack_used: int = 0

    @property
    def values(self) -> list[list[Fraction]]:
        return [[self.spec.from_units(u) for u in row] for row in self.units]

    @property
    def naive_values(self) -> list[list[Fraction]]:
        return [[self.spec.from_units(u) for u in row] for row in self.naive_units]

    def formatted(self, thousands: bool = True) -> list[list[str]]:
        return self.table.formatted(self.values, self.spec, thousands=thousands)

    def adjusted_cells(self) -> list[tuple[int, int]]:
        """Cells that had to move away from their nearest multiple to make it foot."""
        return [
            (i, j)
            for i in range(self.table.n_rows)
            for j in range(self.table.n_cols)
            if self.units[i][j] != self.naive_units[i][j]
        ]

    def deviation(self, units: list[list[int]] | None = None) -> Fraction:
        """Total distance moved, counted in rounding steps."""
        units = self.units if units is None else units
        return sum(
            abs(Fraction(units[i][j]) - self.spec.to_units(self.table.values[i][j]))
            for i in range(self.table.n_rows)
            for j in range(self.table.n_cols)
        )

    def max_deviation(self) -> Fraction:
        return max(
            abs(Fraction(self.units[i][j]) - self.spec.to_units(self.table.values[i][j]))
            for i in range(self.table.n_rows)
            for j in range(self.table.n_cols)
        )

    def violations(self, units: list[list[int]] | None = None) -> list[str]:
        """Constraints the rounded table fails — empty is the whole point."""
        units = self.units if units is None else units
        out: list[str] = []
        for total, members in sorted(self.row_groups.items()):
            for j in range(self.table.n_cols):
                got = sum(units[m][j] for m in members)
                if got != units[total][j]:
                    out.append(
                        f"row total {self.table.row_labels[total]!r} under "
                        f"{self.table.col_labels[j]!r}: "
                        f"{self.spec.from_units(units[total][j])} "
                        f"vs components {self.spec.from_units(got)}"
                    )
        for total, members in sorted(self.col_groups.items()):
            for i in range(self.table.n_rows):
                got = sum(units[i][m] for m in members)
                if got != units[i][total]:
                    out.append(
                        f"column total {self.table.col_labels[total]!r} in row "
                        f"{self.table.row_labels[i]!r}: "
                        f"{self.spec.from_units(units[i][total])} "
                        f"vs components {self.spec.from_units(got)}"
                    )
        return out

    @property
    def consistent(self) -> bool:
        return not self.violations()

    def report(self, show_table: bool = True) -> str:
        from .report import render_report

        return render_report(self, show_table=show_table)


def round_table(
    table: Table | list,
    spec: RoundingSpec | None = None,
    *,
    scale=1,
    decimals: int | None = None,
    step=None,
    row_groups: dict[int, list[int]] | None = None,
    col_groups: dict[int, list[int]] | None = None,
    row_totals=None,
    col_totals=None,
    detect: bool = True,
    tolerance=0,
) -> RoundedTable:
    """Round ``table`` so that every total still equals the sum of its parts.

    Args:
        table: a :class:`~finround.table.Table`, or nested rows of figures.
        spec: how to present figures; or pass ``scale``/``decimals``/``step``.
        row_groups: explicit structure, ``{total row: [component rows]}``.
            Given, it is used as-is and nothing is inferred for that axis.
        row_totals: rows to *treat* as totals; their components are inferred.
        detect: when neither is given, infer totals from the labels.
        tolerance: slack allowed when deciding whether a stated total foots.

    ``col_groups`` / ``col_totals`` do the same for columns.
    """
    if not isinstance(table, Table):
        table = Table.from_rows(table)
    if spec is None:
        spec = RoundingSpec.make(scale=scale, decimals=decimals, step=step)
    tolerance = Fraction(tolerance)

    warnings: list[str] = []
    row_groups = _structure(
        table.row_labels, table.values, row_groups, row_totals, detect, tolerance,
        "row", warnings,
    )
    col_groups = _structure(
        table.col_labels,
        [table.column(j) for j in range(table.n_cols)],
        col_groups, col_totals, detect, tolerance, "column", warnings,
    )
    _warn_unbalanced(table, row_groups, col_groups, tolerance, warnings)

    row_root = build_forest(table.n_rows, row_groups, table.row_labels)
    col_root = build_forest(table.n_cols, col_groups, table.col_labels)

    for slack in range(MAX_SLACK + 1):
        units = _round_forests(table, spec, row_root, col_root, slack)
        if units is not None:
            if slack:
                warnings.append(
                    f"no rounding within one step of every figure could satisfy "
                    f"the structure; up to {slack} extra step(s) of leeway were used"
                )
            break
    else:  # pragma: no cover - a network this constrained has no rounding at all
        raise RuntimeError("no consistent rounding of this table exists")

    naive = [
        [nearest(spec.to_units(value)) for value in row] for row in table.values
    ]
    return RoundedTable(
        table=table,
        spec=spec,
        units=units,
        naive_units=naive,
        row_groups=row_groups,
        col_groups=col_groups,
        warnings=warnings,
        slack_used=slack,
    )


def _structure(labels, vectors, groups, totals, detect, tolerance, axis, warnings):
    if groups is not None:
        return {int(k): [int(m) for m in v] for k, v in groups.items()}
    if totals is None and not detect:
        return {}
    candidates = None if totals is None else {int(t) for t in totals}
    found, unreconciled = detect_groups(labels, vectors, candidates, tolerance)
    for index in unreconciled:
        warnings.append(
            f"{axis} {index + 1} ({labels[index]!r}) looks like a total but no "
            f"block of {axis}s above it adds up to it — left out of the structure"
        )
    return found


def _warn_unbalanced(table, row_groups, col_groups, tolerance, warnings):
    """Note stated totals that only foot approximately, before rounding."""
    for total, members in sorted(row_groups.items()):
        for j in range(table.n_cols):
            gap = table.values[total][j] - sum(table.values[m][j] for m in members)
            if gap != 0 and abs(gap) <= tolerance:
                warnings.append(
                    f"row {total + 1} ({table.row_labels[total]!r}) is out by {float(gap):g} "
                    f"under {table.col_labels[j]!r} before rounding; the rounded "
                    f"table is made to foot exactly, so this total may shift"
                )
    for total, members in sorted(col_groups.items()):
        for i in range(table.n_rows):
            gap = table.values[i][total] - sum(table.values[i][m] for m in members)
            if gap != 0 and abs(gap) <= tolerance:
                warnings.append(
                    f"column {total + 1} ({table.col_labels[total]!r}) is out by "
                    f"{float(gap):g} in row {table.row_labels[i]!r} before rounding"
                )


def _round_forests(
    table: Table, spec: RoundingSpec, row_root: Node, col_root: Node, slack: int
) -> list[list[int]] | None:
    """Solve every block outermost-first; None if some block cannot be satisfied."""
    memo: dict[tuple[int, int], Fraction] = {}

    def true_units(rn: Node, cn: Node) -> Fraction:
        key = (id(rn), id(cn))
        if key not in memo:
            if rn.index is not None and cn.index is not None:
                value = spec.to_units(table.values[rn.index][cn.index])
            elif rn.index is None:
                value = sum(
                    (true_units(child, cn) for child in rn.children), Fraction(0)
                )
            else:
                value = sum(
                    (true_units(rn, child) for child in cn.children), Fraction(0)
                )
            memo[key] = value
        return memo[key]

    def span(rn: Node, cn: Node, lo: int, hi: int) -> _Span:
        """Adjacency when the pair is a real cell of the table; free otherwise."""
        if rn.index is None or cn.index is None:
            return _Span.free(lo, hi)
        return _Span.adjacent(true_units(rn, cn), slack)

    order = {id(node): i for i, node in enumerate(walk(row_root))}
    order.update({id(node): i for i, node in enumerate(walk(col_root))})
    row_nodes = [n for n in walk(row_root) if n.children]
    col_nodes = [n for n in walk(col_root) if n.children]
    blocks = sorted(
        ((rn, cn) for rn in row_nodes for cn in col_nodes),
        key=lambda pair: (
            pair[0].depth + pair[1].depth,
            order[id(pair[0])],
            order[id(pair[1])],
        ),
    )

    decided: dict[tuple[int, int], int] = {}
    for rn, cn in blocks:
        rows, cols = rn.children, cn.children
        cells = [
            [_Span.adjacent(true_units(r, c), slack) for c in cols] for r in rows
        ]
        row_spans = [
            _Span.fixed(decided[(id(r), id(cn))])
            if (id(r), id(cn)) in decided
            else span(
                r, cn,
                sum(cells[i][j].lo for j in range(len(cols))),
                sum(cells[i][j].hi for j in range(len(cols))),
            )
            for i, r in enumerate(rows)
        ]
        col_spans = [
            _Span.fixed(decided[(id(rn), id(c))])
            if (id(rn), id(c)) in decided
            else span(
                rn, c,
                sum(cells[i][j].lo for i in range(len(rows))),
                sum(cells[i][j].hi for i in range(len(rows))),
            )
            for j, c in enumerate(cols)
        ]
        total_lo = sum(s.lo for row in cells for s in row)
        total_hi = sum(s.hi for row in cells for s in row)
        corner = (
            _Span.fixed(decided[(id(rn), id(cn))])
            if (id(rn), id(cn)) in decided
            else span(rn, cn, total_lo, total_hi)
        )

        solved = _solve_block(cells, row_spans, col_spans, corner)
        if solved is None:
            return None
        cell_units, row_units, col_units, corner_units = solved
        for i, r in enumerate(rows):
            for j, c in enumerate(cols):
                decided[(id(r), id(c))] = cell_units[i][j]
        for i, r in enumerate(rows):
            decided[(id(r), id(cn))] = row_units[i]
        for j, c in enumerate(cols):
            decided[(id(rn), id(c))] = col_units[j]
        decided[(id(rn), id(cn))] = corner_units

    by_index = {node.index: node for node in walk(row_root) if node.index is not None}
    by_col = {node.index: node for node in walk(col_root) if node.index is not None}
    return [
        [decided[(id(by_index[i]), id(by_col[j]))] for j in range(table.n_cols)]
        for i in range(table.n_rows)
    ]
