"""Tests for the cross-period casting checker."""

import subprocess
import sys
from pathlib import Path

import pytest

from fincheck.casting import (
    cast,
    extract_period_tables,
    is_additive_label,
    normalize_label,
)
from fincheck.casting_report import to_dict, to_json, write_excel

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "cast_current.pdf"
PRIOR = ROOT / "cast_prior.pdf"


@pytest.fixture(scope="session", autouse=True)
def ensure_samples():
    if not (CURRENT.exists() and PRIOR.exists()):
        subprocess.run(
            [sys.executable, str(ROOT / "make_cast_sample.py")],
            check=True, cwd=ROOT,
        )


@pytest.fixture(scope="session")
def result():
    return cast(str(CURRENT), str(PRIOR), tolerance=1.0)


# --- label helpers ---------------------------------------------------------

def test_normalize_label_strips_footnotes_and_case():
    assert normalize_label("Total expenses (1)") == "total expenses"
    assert normalize_label("Other expenses 2.18 *") == "other expenses 2.18"


def test_per_share_rows_are_not_additive():
    assert not is_additive_label("Basic (₹)")
    assert not is_additive_label("Weighted average equity shares (Basic)")
    assert not is_additive_label("Diluted (in shares)")
    assert is_additive_label("Revenue from operations")


# --- extraction ------------------------------------------------------------

def test_extracts_period_columns():
    tables = extract_period_tables(str(CURRENT))
    assert tables, "no period tables found"
    cols = tables[0].columns
    months_years = sorted((c.months, c.year) for c in cols)
    assert months_years == [(3, 2024), (3, 2025), (6, 2024), (6, 2025)]


def test_prior_has_only_three_month_columns():
    tables = extract_period_tables(str(PRIOR))
    assert tables
    assert all(c.months == 3 for c in tables[0].columns)


# --- the casting check ------------------------------------------------------

def test_casts_both_years(result):
    rev = [c for c in result.checks if c.label.startswith("Revenue")]
    assert {c.year for c in rev} == {2025, 2024}
    for c in rev:
        assert c.status(1.0) == "ok"
        assert c.expected == c.current_quarter + c.prior_quarter


def test_single_known_mismatch(result):
    assert len(result.mismatches) == 1
    bad = result.mismatches[0]
    assert bad.label.startswith("Cost of sales")
    assert bad.year == 2025
    assert bad.six_month == 125
    assert bad.expected == 122          # 60 + 62
    assert bad.difference == 3
    assert not result.consistent


def test_non_additive_rows_excluded_from_pass_fail(result):
    na = result.by_status("not_additive")
    assert na, "expected the per-share row to be flagged non-additive"
    assert all("basic" in c.label.lower() for c in na)
    # never counted as a mismatch even though it is present
    assert all(c.status(1.0) != "mismatch" for c in na)


def test_tolerance_can_absorb_the_difference():
    loose = cast(str(CURRENT), str(PRIOR), tolerance=5.0)
    assert loose.consistent
    strict = cast(str(CURRENT), str(PRIOR), tolerance=0.0)
    # with no slack the off-by-3 (and nothing else) is the only failure
    assert len(strict.mismatches) == 1


# --- reporting -------------------------------------------------------------

def test_report_dict_and_json(result):
    d = to_dict(result)
    assert d["counts"]["mismatch"] == 1
    assert d["consistent"] is False
    assert "Cost of sales" in to_json(result)


def test_excel_written(tmp_path, result):
    out = tmp_path / "casting.xlsx"
    write_excel(result, str(out))
    assert out.exists() and out.stat().st_size > 0
    import openpyxl
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Summary", "Casting"]


def test_highlighted_pdf_written(tmp_path, result):
    from fincheck.casting_highlight import write_highlighted_pdf
    out = tmp_path / "casting.pdf"
    write_highlighted_pdf(result, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_html_report_written(tmp_path, result):
    from fincheck.casting_html import write_html
    out = tmp_path / "casting.html"
    write_html(result, str(out))
    text = out.read_text(encoding="utf-8")
    assert "Casting report" in text
    assert "Segment reporting" in text       # segment section is rendered
    assert "Year-to-date" in text


# --- segment reporting (note 2.23) -----------------------------------------

def test_segment_tables_extracted():
    from fincheck.segment import extract_segment_tables
    tables = extract_segment_tables(str(CURRENT))
    months = sorted(t.months for t in tables)
    assert months == [3, 6]                   # both the 3M and 6M matrices
    assert all(t.names[-1] == "Total" for t in tables)


def test_segment_casts_every_cell(result):
    seg = [c for c in result.checks if c.note == "2.23"]
    assert len(seg) == 12                      # 2 metrics x 3 cols x 2 years
    assert all(c.status(1.0) == "ok" for c in seg)
    total = next(c for c in seg if "Total" in c.label and c.year == 2025
                 and c.label.startswith("Revenue"))
    assert total.six_month == 110              # 50 (3M cur) + 60 (3M prior)
    assert total.expected == 110


def test_segment_quarter_cell_on_its_own_page(result):
    # the six-month and current-quarter figures live on different pages, so the
    # check must remember each cell's page for highlighting
    seg = [c for c in result.checks if c.note == "2.23"]
    assert any(c.quarter_page_index is not None for c in seg)


# --- movement schedules (PP&E note 2.2 / ROU note 2.19) --------------------

def test_schedule_tables_extracted():
    from fincheck.schedule import extract_schedules
    scheds = extract_schedules(str(CURRENT))
    assert sorted(s.months for s in scheds) == [3, 6]
    assert all(s.kind == "ROU" and s.names[-1] == "Total" for s in scheds)


def test_schedule_flow_lines_cast_additively(result):
    flows = [c for c in result.checks if c.note == "2.19"
             and c.mode == "sum" and c.label.startswith("Additions")]
    assert flows
    for c in flows:
        assert c.status(1.0) == "ok"
        assert c.expected == c.current_quarter + c.prior_quarter


def test_schedule_closing_balance_equals_current(result):
    close = next(c for c in result.checks if c.note == "2.19"
                 and c.mode == "equal_current" and "Total" in c.label)
    assert close.six_month == 315 and close.expected == 315   # same date as 3M
    assert close.prior_quarter is None
    assert close.status(1.0) == "ok"


def test_schedule_opening_balance_equals_prior(result):
    opening = next(c for c in result.checks if c.note == "2.19"
                   and c.mode == "equal_prior" and "Total" in c.label)
    assert opening.six_month == 270 and opening.expected == 270
    assert opening.current_quarter is None
    assert opening.status(1.0) == "ok"


def test_basis_appears_in_reports(result):
    from fincheck.casting_report import to_dict
    from fincheck.casting_html import to_html
    bases = {r["basis"] for r in to_dict(result)["checks"]}
    assert "= 3M current (same date)" in bases     # closing balance
    assert "= 3M prior (same date)" in bases        # opening balance
    assert "Basis" in to_html(result)


def test_cli_exits_nonzero_on_mismatch(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "fincheck.cast", str(CURRENT), str(PRIOR),
         "-o", str(tmp_path / "r.xlsx"), "--pdf", "none"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "do not cast" in proc.stdout
