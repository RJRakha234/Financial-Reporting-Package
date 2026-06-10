"""End-to-end checks against the generated sample, plus unit-level footing."""

import subprocess
import sys
from pathlib import Path

import pytest

from fincheck import analyze
from fincheck.checks import Page, _check_column, check_pages, is_total_row
from fincheck.extract import Cell, Row

SAMPLE = Path(__file__).resolve().parent.parent / "sample_financials.pdf"


@pytest.fixture(scope="session", autouse=True)
def ensure_sample():
    if not SAMPLE.exists():
        subprocess.run(
            [sys.executable, str(SAMPLE.parent / "make_sample.py")], check=True
        )


def test_is_total_row():
    assert is_total_row("Total current assets")
    assert is_total_row("Sub-total")
    assert is_total_row("Grand Total")
    assert not is_total_row("Revenue from operations")
    assert not is_total_row("Profit for the year")


_TOP = [0]


def _row(label, indent, value=None):
    return _mrow(label, {0: value} if value is not None else {}, indent)


def _mrow(label, cols=None, indent=0):
    """Build a Row with cells in the given columns (keys are column indices)."""
    cols = cols or {}
    _TOP[0] += 12
    top = _TOP[0]
    cells = {
        c: Cell(c, v, str(v), 100 + c * 40, 130 + c * 40, top, top + 10)
        for c, v in cols.items()
    }
    return Row(0, top, top + 10, label, label_x0=indent * 10.0, indent=indent, cells=cells)


def _page(rows, n_columns=1):
    return Page(0, 600, 800, rows, n_columns)


def _errors(checks):
    return [c for c in checks if c.status == "error"]


def test_flat_footing_detects_error():
    page = Page(0, 600, 800, [], 1)
    page.rows = [
        _row("Sales", 0, 100),
        _row("Services", 0, 50),
        _row("Total revenue", 0, 160),  # should be 150
    ]
    errors = _errors(_check_column(page, 0, base_tolerance=1.0))
    assert len(errors) == 1
    assert errors[0].stated == 160
    assert errors[0].expected == 150


def test_nested_grand_total_does_not_double_count():
    page = Page(0, 600, 800, [], 1)
    page.rows = [
        _row("Inventories", 2, 100),
        _row("Cash", 2, 50),
        _row("Total current assets", 1, 150),
        _row("PPE", 2, 200),
        _row("Total non-current assets", 1, 200),
        _row("Total assets", 0, 350),
    ]
    assert _errors(_check_column(page, 0, base_tolerance=1.0)) == []


def test_correct_total_within_tolerance():
    page = Page(0, 600, 800, [], 1)
    page.rows = [
        _row("A", 0, 33.3),
        _row("B", 0, 33.3),
        _row("C", 0, 33.4),
        _row("Total", 0, 100.0),  # rounded components
    ]
    assert _errors(_check_column(page, 0, base_tolerance=1.0)) == []


def test_derived_subtotal_is_a_boundary_but_can_be_a_component():
    # Total comprehensive income = Net profit (derived) + Total OCI.
    rows = [
        _row("Net profit", 0, 8509),  # derived: a boundary
        _row("Remeasurement", 0, 100),
        _row("Exchange differences", 0, 200),
        _row("Total other comprehensive income", 0, 300),
        _row("Total comprehensive income", 0, 8809),
    ]
    assert _errors(_check_column(_page(rows), 0, 1.0)) == []


def test_implicit_unlabelled_subtotals_reconcile():
    # Gross/tax/net style: 138 = -236+374, 917 = -11+1021-93, total = 138+917.
    rows = [
        _row("Remeasurement", 0, -236),
        _row("Equity instruments", 0, 374),
        _row("", 0, 138),  # implicit subtotal
        _row("Derivatives", 0, -11),
        _row("Exchange", 0, 1021),
        _row("Investments", 0, -93),
        _row("", 0, 917),  # implicit subtotal
        _row("Total other comprehensive income", 0, 1055),
    ]
    assert _errors(_check_column(_page(rows), 0, 1.0)) == []


def test_duplicate_split_column_is_suppressed():
    # Same total value reconciles in column 0; the split column 1 (missing a
    # line) must not be reported.
    rows = [
        _mrow("Cash", {0: 10, 1: 10}),
        _mrow("Receivables", {0: 20}),  # spilled out of column 1
        _mrow("Total", {0: 30, 1: 30}),
    ]
    issues = check_pages([_page(rows, n_columns=2)], base_tolerance=1.0)
    assert issues == []


def test_real_world_genuine_error_still_caught():
    # A genuinely misstated subtotal in a single clean column is still flagged.
    rows = [
        _row("Inventories", 0, 100),
        _row("Cash", 0, 50),
        _row("Total current assets", 0, 999),
    ]
    errors = _errors(_check_column(_page(rows), 0, 1.0))
    assert len(errors) == 1 and errors[0].expected == 150


def test_components_are_tracked_for_coverage():
    rows = [
        _row("Inventories", 0, 100),
        _row("Cash", 0, 50),
        _row("Total current assets", 0, 150),
    ]
    checks = _check_column(_page(rows), 0, 1.0)
    ok = [c for c in checks if c.status == "ok"]
    assert len(ok) == 1
    # both line items were recorded as the figures summed into the total
    assert {round(v) for _, v in ok[0].component_detail} == {100, 50}
    assert len(ok[0].component_cells) == 2


def test_sample_pdf_flags_exactly_two():
    result = analyze(str(SAMPLE))
    labels = sorted(i.label for i in result.issues)
    assert len(result.issues) == 2
    assert any("current assets" in lbl.lower() for lbl in labels)
    assert any("expenses" in lbl.lower() for lbl in labels)


def test_highlighted_pdf_written(tmp_path):
    out = tmp_path / "out.pdf"
    result = analyze(str(SAMPLE), output_pdf=str(out))
    assert out.exists()
    import fitz

    doc = fitz.open(str(out))
    # original 1 page + 1 summary page prepended
    assert doc.page_count == 2
    annot_types = [a.type[1] for a in doc[1].annots()]
    # every figure summed into a total is highlighted (coverage), plus the
    # two failing totals are outlined.
    assert annot_types.count("Highlight") > 2
    assert annot_types.count("Square") == 2
    assert result.figures_checked > 0
