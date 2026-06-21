"""Tests for plcheck, driven by the bundled sample inputs.

The sample report contains two genuine discrepancies (the General
Administration / Depreciation line was entered as rounded integers, 458 and
157384, instead of 458.51 and 157384.65), which the check must surface — and
which the reference check file also flags.
"""

import os

import openpyxl
import pytest

from plcheck import analyze
from plcheck import inputs
from plcheck.evaluate import evaluate
from plcheck.report import to_dict

HERE = os.path.dirname(__file__)
SAMPLE = os.path.join(HERE, os.pardir, "sample")
REPORT = os.path.join(SAMPLE, "IFRS_INR_PL_Report.xlsx")
TB = os.path.join(SAMPLE, "Real_Time_TB.xlsx")
AGG = os.path.join(SAMPLE, "Aggregate_Expenses.xlsx")
RATES = os.path.join(SAMPLE, "MA_Rates.xlsx")


@pytest.fixture(scope="module")
def report():
    return inputs.read_report(REPORT)


def test_report_parsed(report):
    assert [e.code for e in report.entities] == ["BALSCH", "BALSDE", "BALSDK"]
    assert [e.currency for e in report.entities] == ["CHF", "EUR", "DKK"]
    labels = [b.label for b in report.blocks]
    assert labels[0] == "LC - Balance"
    assert "GC - Total" in labels


def test_evaluation_flags_depreciation_rounding(report):
    ev = evaluate(report, TB, AGG, RATES)
    assert not ev.ok
    # the only LC tie-out failures are the depreciation roundings
    lc = {(d.entity, round(d.delta, 2)) for d in ev.flagged() if d.kind == "lc"}
    assert ("BALSDE", 0.51) in lc
    assert ("BALSDK", 0.65) in lc
    # no LC tie-out failure for any other account
    bad_accounts = {d.account for d in ev.flagged()
                    if d.kind == "lc" and abs(d.delta) > 0.5}
    assert bad_accounts == {290100}


def test_fx_and_consolidation_otherwise_tie(report):
    ev = evaluate(report, TB, AGG, RATES)
    # every consolidation (GC-Total) difference reconciles
    consol = [d for d in ev.flagged() if d.kind == "consol"]
    assert consol == []
    # the only FX failures are the ones propagated from the rounding
    fx = {round(d.delta, 2) for d in ev.flagged() if d.kind == "fx"}
    assert fx <= {-54.99, 54.99, -9.24, 9.23, 9.24}


def test_net_profit_reconciliation(report):
    ev = evaluate(report, TB, AGG, RATES)
    for n in ev.net_profit:
        # internal P&L integrity always holds
        assert abs(n.calc_check) < 0.01
    tie = {n.entity: round(n.tie_check, 2) for n in ev.net_profit}
    assert tie["BALSCH"] == 0.0
    assert tie["BALSDE"] == 0.51
    assert tie["BALSDK"] == 0.65


def test_all_categories_mapped(report):
    ev = evaluate(report, TB, AGG, RATES)
    assert ev.unmapped_categories == []


def test_build_writes_valid_workbook(tmp_path):
    out = tmp_path / "Check.xlsx"
    result = analyze(REPORT, TB, AGG, RATES, output_path=str(out))
    assert result.output_path == str(out)
    assert out.is_file()

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Check", "Real Time TB", "Aggregate Exp", "MA rates"]
    ws = wb["Check"]
    # difference formulas are present on detail rows, absent on subtotal rows
    assert str(ws["F9"].value).startswith("=IFERROR(VLOOKUP")
    assert ws["F11"].value is None            # Revenue subtotal: no LC tie-out
    assert str(ws["Q9"].value).startswith("=SUMIF")   # FX conversion
    assert str(ws["AJ9"].value).startswith("=SUMIF")  # consolidation


def test_json_summary_shape(report):
    ev = evaluate(report, TB, AGG, RATES)
    d = to_dict(ev)
    assert set(d) >= {"ok", "tolerance", "differences", "net_profit"}
    assert d["ok"] is False
    assert all("delta" in row for row in d["differences"])
