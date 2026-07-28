from fractions import Fraction

import pytest

from finround.structure import build_forest, detect_groups, is_total_label, walk


def frac(rows):
    return [[Fraction(v) for v in row] for row in rows]


def test_labels_that_read_as_totals():
    assert is_total_label("Total current assets")
    assert is_total_label("Sub-total")
    assert is_total_label("GRAND TOTAL")
    assert not is_total_label("Cash and cash equivalents")


def test_detects_a_simple_total():
    labels = ["A", "B", "Total"]
    groups, unreconciled = detect_groups(labels, frac([[1, 2], [3, 4], [4, 6]]))
    assert groups == {2: [0, 1]}
    assert unreconciled == []


def test_nested_totals_roll_up():
    labels = ["A", "B", "Subtotal 1", "C", "D", "Subtotal 2", "Total"]
    vectors = frac([[1], [2], [3], [4], [5], [9], [12]])
    groups, _ = detect_groups(labels, vectors)
    assert groups == {2: [0, 1], 5: [3, 4], 6: [2, 5]}


def test_a_total_must_reconcile_in_every_column():
    labels = ["A", "B", "Total"]
    # foots in the first column only, so it is not accepted as a total
    groups, unreconciled = detect_groups(labels, frac([[1, 2], [3, 4], [4, 99]]))
    assert groups == {}
    assert unreconciled == [2]


def test_explicit_candidates_ignore_labels():
    labels = ["A", "B", "Closing balance"]
    groups, _ = detect_groups(labels, frac([[1], [2], [3]]), candidates={2})
    assert groups == {2: [0, 1]}


def test_forest_covers_every_row_and_records_depth():
    root = build_forest(3, {2: [0, 1]}, ["A", "B", "Total"])
    assert root.index is None
    assert [n.index for n in root.children] == [2]
    assert {n.index: n.depth for n in walk(root) if n.index is not None} == {
        2: 1,
        0: 2,
        1: 2,
    }


def test_forest_rejects_a_row_claimed_twice():
    with pytest.raises(ValueError):
        build_forest(3, {1: [0], 2: [0]}, ["A", "T1", "T2"])


def test_forest_rejects_self_reference():
    with pytest.raises(ValueError):
        build_forest(2, {1: [1]}, ["A", "T"])
