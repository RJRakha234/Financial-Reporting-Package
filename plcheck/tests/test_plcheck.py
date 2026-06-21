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


# --------------------------------------------------------------------------
# Requirement #1: a different number of GL accounts must never be missed.
# --------------------------------------------------------------------------

def test_added_gl_account_gets_all_checks(tmp_path):
    """A GL added to the report must receive LC / FX / consolidation checks."""
    from plcheck.model import ReportRow
    from plcheck.workbook import build_check_workbook, FIRST_DATA_ROW

    report = inputs.read_report(REPORT)
    # insert a brand-new Cost of Production GL right after the existing ones
    new_row = ReportRow(
        category="Cost of Production", account=110999,
        description="Newly added salary GL",
        values={(b.label, sub): 123.0
                for b in report.blocks for sub in b.columns},
    )
    pos = max(i for i, r in enumerate(report.rows)
              if r.category == "Cost of Production") + 1
    report.rows.insert(pos, new_row)

    out = tmp_path / "Check.xlsx"
    build_check_workbook(report, TB, AGG, RATES).save(out)
    ws = openpyxl.load_workbook(out)["Check"]
    r = FIRST_DATA_ROW + pos
    assert ws[f"C{r}"].value == 110999                         # GL carried over
    assert str(ws[f"F{r}"].value).startswith("=IFERROR(VLOOKUP")  # LC tie-out
    assert str(ws[f"Q{r}"].value).startswith("=SUMIF")          # FX
    assert str(ws[f"AJ{r}"].value).startswith("=SUMIF")         # consolidation


def test_lc_checks_cover_every_sourced_detail_gl():
    """Number of LC tie-out checks == sourced detail GLs x entities."""
    from plcheck.config import CheckConfig
    report = inputs.read_report(REPORT)
    cfg = CheckConfig()
    sourced = [r for r in report.rows
               if not r.is_subtotal and r.category
               and (cfg.rule_for(r.category) and cfg.rule_for(r.category).source)]
    ev = evaluate(report, TB, AGG, RATES)
    lc = [d for d in ev.diffs if d.kind == "lc"]
    assert len(lc) == len(sourced) * len(report.entities)


# --------------------------------------------------------------------------
# Requirement #2: TB net profit is the column-wise sum of the P&L-series GLs,
# computed from the accounts (not a fixed SUM row), excluding balance sheet.
# --------------------------------------------------------------------------

def _make_tb(path, extra_rows):
    """Clone the sample TB and append extra (account, chf, eur, dkk) rows."""
    wb = openpyxl.load_workbook(TB)
    ws = wb.active
    r = ws.max_row + 1
    for acct, chf, eur, dkk in extra_rows:
        ws.cell(r, 1, acct)            # Group Account Number (col A)
        ws.cell(r, 4, chf); ws.cell(r, 5, eur); ws.cell(r, 6, dkk)
        r += 1
    wb.save(path)


def test_tb_net_profit_excludes_balance_sheet(tmp_path):
    from plcheck.config import CheckConfig
    cfg = CheckConfig()
    accounts, _ = inputs.tb_values(TB, ["BALSCH", "BALSDE", "BALSDK"])
    expected = sum(v["BALSCH"] for a, v in accounts.items() if cfg.is_pl_account(a))

    # adding a balance-sheet account (>=400000) must NOT change the net profit
    tb2 = tmp_path / "TB_bs.xlsx"
    _make_tb(tb2, [(456000, 9_999_999, 0, 0)])
    acc2, _ = inputs.tb_values(str(tb2), ["BALSCH", "BALSDE", "BALSDK"])
    got = sum(v["BALSCH"] for a, v in acc2.items() if cfg.is_pl_account(a))
    assert round(got, 2) == round(expected, 2)


def test_tb_net_profit_includes_added_pl_gl(tmp_path):
    from plcheck.config import CheckConfig
    cfg = CheckConfig()
    accounts, _ = inputs.tb_values(TB, ["BALSCH", "BALSDE", "BALSDK"])
    base = sum(v["BALSCH"] for a, v in accounts.items() if cfg.is_pl_account(a))

    # a new P&L-series account (3-series) must flow into the net-profit sum
    tb2 = tmp_path / "TB_pl.xlsx"
    _make_tb(tb2, [(312000, 1000.0, 0, 0)])
    acc2, _ = inputs.tb_values(str(tb2), ["BALSCH", "BALSDE", "BALSDK"])
    got = sum(v["BALSCH"] for a, v in acc2.items() if cfg.is_pl_account(a))
    assert round(got - base, 2) == 1000.0


def test_is_pl_account_boundaries():
    from plcheck.config import CheckConfig
    cfg = CheckConfig()
    assert cfg.is_pl_account(110200) and cfg.is_pl_account(311025)
    assert not cfg.is_pl_account(445600)   # balance sheet
    assert not cfg.is_pl_account(885900)
    assert not cfg.is_pl_account(400000)   # boundary is exclusive


# --------------------------------------------------------------------------
# Block-label robustness: GC - Total (and others) must still be checked when
# the export's spelling/spacing varies; if a block is truly absent, warn.
# --------------------------------------------------------------------------

def _clone_report_relabel(path, old_norm, new_text):
    """Clone the sample report, replacing a block label (row 2) by its
    normalized match with new_text."""
    from plcheck.config import normalize_label
    wb = openpyxl.load_workbook(REPORT)
    ws = wb.active
    for c in ws[2]:
        if c.value is not None and normalize_label(c.value) == old_norm:
            c.value = new_text
    wb.save(path)


def test_canonical_block_label_variants():
    from plcheck.config import canonical_block_label
    assert canonical_block_label("GC-Total") == "GC - Total"
    assert canonical_block_label("gc  -  total") == "GC - Total"
    assert canonical_block_label("LC - Balance ") == "LC - Balance"
    assert canonical_block_label("Mystery Block") == "Mystery Block"


def test_gc_total_check_runs_despite_spacing(tmp_path):
    from plcheck.workbook import build_check_workbook
    rep = tmp_path / "report_variant.xlsx"
    _clone_report_relabel(rep, "gc-total", "GC-Total")     # drop the spaces
    report = inputs.read_report(str(rep))
    assert report.block("GC - Total") is not None          # canonicalized
    out = tmp_path / "Check.xlsx"
    build_check_workbook(report, TB, AGG, RATES).save(out)
    ws = openpyxl.load_workbook(out)["Check"]
    assert str(ws["AJ9"].value).startswith("=SUMIF")       # GC-Total check built
    ev = evaluate(report, TB, AGG, RATES)
    assert "GC - Total" not in ev.missing_blocks


def test_missing_block_is_flagged(tmp_path):
    rep = tmp_path / "report_missing.xlsx"
    _clone_report_relabel(rep, "gc-total", "Grand Total XYZ")   # unrecognised
    report = inputs.read_report(str(rep))
    ev = evaluate(report, TB, AGG, RATES)                       # must not crash
    assert "GC - Total" in ev.missing_blocks


def test_rate_lookup_window_is_configurable():
    from plcheck.config import CheckConfig
    assert CheckConfig().rate_lookup_rows == 150
