import random
from fractions import Fraction

import pytest

from finround import RoundingSpec, Table, round_table


def check(result):
    """Every promise the tool makes, asserted at once."""
    assert result.violations() == [], result.violations()
    if result.slack_used == 0:
        assert result.max_deviation() < 1, "a figure moved a whole step or more"
    return result


def test_thirds_table_foots_in_both_directions():
    table = Table.from_rows(
        [
            ["33.3", "33.3", "33.4", "100.0"],
            ["33.3", "33.3", "33.4", "100.0"],
            ["33.4", "33.4", "33.2", "100.0"],
            ["100.0", "100.0", "100.0", "300.0"],
        ],
        row_labels=["A", "B", "C", "Total"],
        col_labels=["Q1", "Q2", "Q3", "Total"],
    )
    result = check(round_table(table))
    assert result.units[3] == [100, 100, 100, 300]
    assert sum(result.units[0][:3]) == 100
    assert len(result.adjusted_cells()) == 3


def test_percentages_still_add_to_a_hundred():
    """Every share is a half, so two of them must give way — but only two."""
    table = Table.from_rows(
        [["24.5"], ["24.5"], ["24.5"], ["26.5"], ["100.0"]],
        row_labels=["A", "B", "C", "D", "Share"],
        col_labels=["Share"],
    )
    result = check(round_table(table, row_totals=[4]))
    shares = [row[0] for row in result.units]
    assert shares[4] == 100 == sum(shares[:4])
    assert sorted(shares[:4]) == [24, 25, 25, 26]


def test_no_totals_means_plain_nearest_rounding():
    table = Table.from_rows(
        [["1.5", "2.4"], ["3.6", "-2.5"]], row_labels=["A", "B"], col_labels=["X", "Y"]
    )
    result = check(round_table(table))
    assert result.units == [[2, 2], [4, -3]]
    assert result.adjusted_cells() == []


def test_negative_figures_round_towards_a_footing_total():
    table = Table.from_rows(
        [["-10.5"], ["-10.5"], ["-10.5"], ["-31.5"]],
        row_labels=["A", "B", "C", "Total"],
        col_labels=["Value"],
    )
    result = check(round_table(table))
    column = [row[0] for row in result.units]
    assert column[3] == sum(column[:3]) == -32


def test_grand_total_keeps_its_nearest_value():
    table = Table.from_rows(
        [
            ["10.4", "10.4", "20.8"],
            ["10.4", "10.4", "20.8"],
            ["20.8", "20.8", "41.6"],
        ],
        row_labels=["A", "B", "Total"],
        col_labels=["X", "Y", "Total"],
    )
    result = check(round_table(table))
    assert result.units[2][2] == 42  # 41.6 to nearest, not dragged by the corners


def test_nested_subtotals_in_both_dimensions():
    table = Table.from_rows(
        [
            ["1.5", "1.5", "3.0", "1.5", "4.5"],
            ["1.5", "1.5", "3.0", "1.5", "4.5"],
            ["3.0", "3.0", "6.0", "3.0", "9.0"],
            ["2.5", "2.5", "5.0", "2.5", "7.5"],
            ["5.5", "5.5", "11.0", "5.5", "16.5"],
        ],
        row_labels=["A", "B", "Subtotal", "C", "Total"],
        col_labels=["X", "Y", "Subtotal", "Z", "Total"],
    )
    result = check(round_table(table))
    assert result.row_groups == {2: [0, 1], 4: [2, 3]}
    assert result.col_groups == {2: [0, 1], 4: [2, 3]}


def test_explicit_structure_overrides_detection():
    table = Table.from_rows(
        [["1.4"], ["1.4"], ["2.8"]],
        row_labels=["A", "B", "Closing"],
        col_labels=["Value"],
    )
    result = check(round_table(table, row_groups={2: [0, 1]}))
    column = [row[0] for row in result.units]
    assert column == [1, 2, 3] or column == [2, 1, 3]


def test_total_rows_can_be_named_by_position():
    table = Table.from_rows(
        [["1.4"], ["1.4"], ["2.8"]], row_labels=["A", "B", "Closing"], col_labels=["V"]
    )
    result = check(round_table(table, row_totals=[2]))
    assert result.row_groups == {2: [0, 1]}


def test_detection_can_be_switched_off():
    table = Table.from_rows(
        [["1.4"], ["1.4"], ["2.8"]], row_labels=["A", "B", "Total"], col_labels=["V"]
    )
    result = round_table(table, detect=False)
    assert result.row_groups == {}
    assert [row[0] for row in result.units] == [1, 1, 3]  # each at its nearest


def test_unreconciled_total_is_warned_about_not_invented():
    table = Table.from_rows(
        [["1.0"], ["2.0"], ["99.0"]], row_labels=["A", "B", "Total"], col_labels=["V"]
    )
    result = round_table(table)
    assert result.row_groups == {}
    assert any("looks like a total" in w for w in result.warnings)


def test_scale_and_step_are_honoured():
    table = Table.from_rows(
        [["1234567"], ["2345678"], ["3580245"]],
        row_labels=["A", "B", "Total"],
        col_labels=["FY25"],
    )
    result = check(round_table(table, scale=1000, decimals=1))
    # 1,234.567 + 2,345.678 = 3,580.245; the total keeps its nearest value and
    # the cheaper of the two lines gives way (0.067 of a step, against 0.222).
    assert result.formatted() == [["1,234.5"], ["2,345.7"], ["3,580.2"]]


def test_rounding_to_the_nearest_five():
    table = Table.from_rows(
        [["11"], ["11"], ["11"], ["33"]],
        row_labels=["A", "B", "C", "Total"],
        col_labels=["V"],
    )
    result = check(round_table(table, spec=RoundingSpec.make(step=5)))
    column = [float(row[0]) for row in result.values]
    assert column[3] == sum(column[:3]) == 35.0


def test_blank_cells_stay_blank():
    table = Table.from_rows(
        [["1.4", ""], ["1.4", "2.0"], ["2.8", "2.0"]],
        row_labels=["A", "B", "Total"],
        col_labels=["X", "Y"],
    )
    result = check(round_table(table))
    assert result.formatted()[0][1] == ""


def test_report_states_the_outcome():
    table = Table.from_rows(
        [["33.3"], ["33.3"], ["33.4"], ["100.0"]],
        row_labels=["A", "B", "C", "Total"],
        col_labels=["V"],
    )
    text = round_table(table).report()
    assert "foot exactly after rounding" in text
    assert "nearest 1" in text


def test_single_row_and_single_column_tables():
    check(round_table(Table.from_rows([["1.5"]])))
    check(round_table(Table.from_rows([["1.5", "1.5", "3.0"]], col_labels=["A", "B", "Total"])))


@pytest.mark.parametrize("seed", range(40))
def test_random_tables_always_foot_and_stay_adjacent(seed):
    """The guarantee, exercised: any table with margins can be rounded."""
    rng = random.Random(seed)
    n_rows, n_cols = rng.randint(1, 6), rng.randint(1, 6)
    body = [
        [Fraction(rng.randint(-40000, 200000), 100) for _ in range(n_cols)]
        for _ in range(n_rows)
    ]
    rows = [row + [sum(row)] for row in body]
    rows.append([sum(col) for col in zip(*rows)])
    table = Table.from_rows(
        rows,
        row_labels=[f"r{i}" for i in range(n_rows)] + ["Total"],
        col_labels=[f"c{j}" for j in range(n_cols)] + ["Total"],
    )
    result = check(round_table(table))
    assert result.slack_used == 0


@pytest.mark.parametrize("seed", range(20))
def test_random_hierarchical_tables(seed):
    """Two levels of subtotals down the rows, a total column across."""
    rng = random.Random(1000 + seed)
    groups = [rng.randint(1, 3) for _ in range(rng.randint(1, 3))]
    n_cols = rng.randint(1, 4)
    rows, labels, index = [], [], 0
    subtotal_rows = []
    for size in groups:
        block = [
            [Fraction(rng.randint(-5000, 90000), 100) for _ in range(n_cols)]
            for _ in range(size)
        ]
        for k, line in enumerate(block):
            rows.append(line)
            labels.append(f"item {index}.{k}")
        rows.append([sum(col) for col in zip(*block)])
        labels.append(f"Total group {index}")
        subtotal_rows.append(len(rows) - 1)
        index += 1
    rows.append([sum(rows[s][j] for s in subtotal_rows) for j in range(n_cols)])
    labels.append("Grand total")
    rows = [row + [sum(row)] for row in rows]

    table = Table.from_rows(
        rows, row_labels=labels, col_labels=[f"c{j}" for j in range(n_cols)] + ["Total"]
    )
    result = check(round_table(table, scale=1, decimals=0))
    assert result.slack_used == 0
