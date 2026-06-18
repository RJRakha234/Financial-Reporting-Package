"""Stacked / block matrix-note reconciliation."""

from datetime import date

from fincheck.extract import Cell, Page, Row
from fincheck.matrix import parse_title_periods, reconcile_matrix


def test_parse_title_periods():
    assert parse_title_periods("Three months ended September 30, 2025 and "
                               "September 30, 2024") == (
        "quarter", [date(2025, 9, 30), date(2024, 9, 30)])
    assert parse_title_periods("Six months ended September 30, 2025 and "
                               "September 30, 2024")[0] == "half-year"
    pt = parse_title_periods("... by categories as at March 31, 2025 were as "
                             "follows:")
    assert pt == ("point-in-time", [date(2025, 3, 31)])
    assert parse_title_periods("Particulars and notes") is None


def _toks(text):
    return [{"text": w, "x0": i * 30, "x1": i * 30 + 20, "top": 0, "bottom": 8}
            for i, w in enumerate(text.split())]


def _title(text):
    return Row(0, 0, 8, text, 0.0, tokens=_toks(text))


def _datarow(label, values):
    cells = {i: Cell(i, v, str(v), 100 + i * 40, 130 + i * 40, 0, 8)
             for i, v in enumerate(values)}
    toks = _toks(label) if label else []
    return Row(0, 0, 8, label, 0.0, cells=cells, tokens=toks)


def _segment_page(d1, d2, rev_new, rev_old):
    # "2.15.1 Business segments" then a stacked Revenue metric.
    rows = [
        Row(0, 0, 8, "2.15.1 Business segments", 0.0,
            tokens=_toks("2.15.1 Business segments")),
        _title(f"Three months ended September 30, {d1} and September 30, {d2}"),
        _datarow("Revenue", rev_new),
        _datarow("", rev_old),  # unlabelled comparative row
    ]
    return Page(0, 600, 800, rows, 3)


def test_stacked_segment_reconciles_by_multiset():
    # Current filing: 2025 row + 2024 comparative; prior filing: 2024 + 2023.
    current = [_segment_page(2025, 2024, [10, 20, 30], [9, 18, 27])]
    prior = [_segment_page(2024, 2023, [9, 18, 27], [8, 16, 24])]
    ok, review = reconcile_matrix(current, [("prior", prior)], 1.0,
                                  allowed={"2.15.1"})
    assert len(ok) == 1 and not review
    assert ok[0].section == "2.15.1" and ok[0].status == "ok"
    assert "30 Sep 2024" in ok[0].period_desc


def test_stacked_segment_flags_changed_comparative():
    current = [_segment_page(2025, 2024, [10, 20, 30], [9, 18, 99])]  # 27 -> 99
    prior = [_segment_page(2024, 2023, [9, 18, 27], [8, 16, 24])]
    ok, review = reconcile_matrix(current, [("prior", prior)], 1.0,
                                  allowed={"2.15.1"})
    assert not ok and len(review) == 1 and review[0].status == "review"


def _fi_page(date_str, deriv_pad):
    # A "2.3.5" block with a duplicate "Total" label (assets then liabilities)
    # and a derivative row padded with a variable number of nil columns.
    rows = [
        Row(0, 0, 8, "2.3.5 Impairment", 0.0, tokens=_toks("2.3.5 Impairment")),
        _title(f"The carrying value ... as at {date_str} were as follows:"),
        _datarow("Cash and cash equivalents", [100, 0, 0, 100, 100]),
        _datarow("Total", [100, 0, 0, 100, 100]),          # assets total
        _datarow("Derivative financial instruments", [0] * deriv_pad + [50, 50]),
        _datarow("Total", [0, 0, 50, 50]),                 # liabilities total
    ]
    return Page(0, 600, 800, rows, 5)


def test_block_duplicate_labels_and_nil_padding_reconcile():
    # Same figures, but the prior pads the derivative row with an extra nil
    # column and repeats the "Total" label — must still reconcile.
    current = [_fi_page("March 31, 2025", deriv_pad=2)]
    prior = [_fi_page("March 31, 2025", deriv_pad=3)]
    ok, review = reconcile_matrix(current, [("prior", prior)], 1.0,
                                  allowed={"2.3.5"})
    assert not review
    assert {c.metric for c in ok} == {
        "Cash and cash equivalents", "Total", "Derivative financial instruments"}
    # both "Total" rows (assets and liabilities) reconciled by occurrence
    assert sum(c.metric == "Total" for c in ok) == 2
