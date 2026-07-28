"""The rounding must not merely foot — it must be the least-bad one available."""

import itertools
import random
from fractions import Fraction

import pytest

from finround import Table, round_table
from finround.units import ceil_div, floor_div


def _table_with_margins(rng, n_rows, n_cols, denominator=10):
    body = [
        [Fraction(rng.randint(-200, 2000), denominator) for _ in range(n_cols)]
        for _ in range(n_rows)
    ]
    rows = [row + [sum(row)] for row in body]
    rows.append([sum(col) for col in zip(*rows)])
    return Table.from_rows(
        rows,
        row_labels=[f"r{i}" for i in range(n_rows)] + ["Total"],
        col_labels=[f"c{j}" for j in range(n_cols)] + ["Total"],
    )


@pytest.mark.parametrize("seed", range(25))
def test_interior_is_the_cheapest_fit_for_its_margins(seed):
    """Brute force every valid rounding of the body; ours must be as cheap."""
    rng = random.Random(7000 + seed)
    n_rows, n_cols = rng.randint(2, 3), rng.randint(2, 3)
    table = _table_with_margins(rng, n_rows, n_cols)
    result = round_table(table)
    assert result.violations() == []

    spec = result.spec
    true = [[spec.to_units(v) for v in row] for row in table.values]
    row_targets = [result.units[i][n_cols] for i in range(n_rows)]
    col_targets = [result.units[n_rows][j] for j in range(n_cols)]

    def cost(cells):
        return sum(
            abs(Fraction(cells[i][j]) - true[i][j])
            for i in range(n_rows)
            for j in range(n_cols)
        )

    choices = [
        [(floor_div(true[i][j]), ceil_div(true[i][j])) for j in range(n_cols)]
        for i in range(n_rows)
    ]
    best = None
    for combo in itertools.product(*[
        itertools.product(*choices[i]) for i in range(n_rows)
    ]):
        cells = [list(row) for row in combo]
        if any(sum(cells[i]) != row_targets[i] for i in range(n_rows)):
            continue
        if any(
            sum(cells[i][j] for i in range(n_rows)) != col_targets[j]
            for j in range(n_cols)
        ):
            continue
        here = cost(cells)
        best = here if best is None else min(best, here)

    ours = cost([row[:n_cols] for row in result.units[:n_rows]])
    assert best is not None, "brute force found no valid rounding at all"
    assert ours == best


@pytest.mark.parametrize("seed", range(15))
def test_aggregates_are_rounded_before_the_detail_gives_way(seed):
    """Grand total, then the margins: the visible figures keep their nearest value."""
    rng = random.Random(8000 + seed)
    table = _table_with_margins(rng, rng.randint(2, 5), rng.randint(2, 5), 100)
    result = round_table(table)
    grand = result.units[-1][-1]
    assert grand == result.naive_units[-1][-1]
    assert result.violations() == []


@pytest.mark.parametrize("size", [(12, 9), (25, 4), (3, 30)])
def test_larger_tables_stay_within_one_step(size):
    rng = random.Random(sum(size))
    table = _table_with_margins(rng, size[0], size[1], 1000)
    result = round_table(table)
    assert result.violations() == []
    assert result.slack_used == 0
    assert result.max_deviation() < 1
