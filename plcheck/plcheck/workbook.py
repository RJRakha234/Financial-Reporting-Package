"""Assemble the ``Check`` workbook with live reconciliation formulas.

The output is a genuine, self-contained Excel file: the three source reports
are embedded as sheets and every difference is a real formula referencing them,
so it recalculates in Excel exactly like a hand-built check file — but produced
in one step from the inputs.
"""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import column_index_from_string, get_column_letter

from . import config as C
from . import inputs
from .layout import (COL_ACCOUNT, COL_CATEGORY, COL_CLASS, COL_DESC,
                     CheckLayout, plan)
from .model import ReportTable, SheetData

# Row map of the Check sheet (matches the reference layout).
ROW_TB_LABEL = 2       # "Real Time TB" + per-entity MATCH into the TB
ROW_AGG_LABEL = 3      # "Aggregate Exp" + per-entity MATCH / FX-rate VLOOKUP
ROW_SUMDIFF = 4        # "Sum of Differences" + SUM of each difference column
ROW_BLOCKLABEL = 5
ROW_SUBHEADER = 6
ROW_ENTNAME = 7
ROW_CURR = 8
FIRST_DATA_ROW = 9

RED_FILL = PatternFill("solid", fgColor="FFC7CE")
RED_FONT = Font(color="9C0006")
HDR_FONT = Font(bold=True)
HELP_FONT = Font(italic=True, color="808080")


def _embed(wb: Workbook, sheet: SheetData) -> None:
    ws = wb.create_sheet(sheet.title)
    for coord, value in sheet.cells.items():
        ws[coord] = value


def build_check_workbook(report: ReportTable, tb_path: str, agg_path: str,
                         rates_path: str,
                         cfg: C.CheckConfig | None = None,
                         consol_path: str | None = None) -> Workbook:
    cfg = cfg or C.CheckConfig()
    # LC - Consol is reconciled to the entry tracker only when one is supplied.
    extra_checked = {C.CHECK_LC_CONSOL} if consol_path else set()
    layout = plan(report, extra_checked=extra_checked)
    codes = [e.code for e in report.entities]
    currency = {e.code: e.currency for e in report.entities}

    tb = inputs.parse_tb(tb_path, codes)
    agg = inputs.parse_agg(agg_path, codes) if agg_path else None
    rates = inputs.parse_rates(rates_path)
    consol = inputs.parse_consol(consol_path) if consol_path else None
    consol_ref = None
    if consol:
        cc = get_column_letter(consol.concat_col)
        concat_rng = (f"'{C.SHEET_CONSOL}'!${cc}${consol.first_row}"
                      f":${cc}${consol.last_row}")
        val_rngs = [f"'{C.SHEET_CONSOL}'!${get_column_letter(vc)}${consol.first_row}"
                    f":${get_column_letter(vc)}${consol.last_row}"
                    for vc in consol.val_cols]
        consol_ref = (concat_rng, val_rngs)
    gc_currency = cfg.gc_currency or inputs.detect_gc_currency(report, rates.rates)

    tb_lastcol = get_column_letter(tb.last_col)
    agg_mfirst = get_column_letter(agg.match_first_col) if agg else None
    agg_mlast = get_column_letter(agg.match_last_col) if agg else None
    rate_from = get_column_letter(rates.from_col)
    rate_to = get_column_letter(rates.rate_col)

    # Span the FX / consolidation source ranges over whichever of the expected
    # blocks are actually present, so a renamed or absent block can't crash the
    # build — the missing block simply contributes nothing to the SUMIF.
    def _first_present(blocks):
        cols = [layout.block_first_value[b] for b in blocks
                if b in layout.block_first_value]
        return min(cols, key=column_index_from_string) if cols else None

    def _last_present(blocks):
        cols = [layout.block_last_value[b] for b in blocks
                if b in layout.block_last_value]
        return max(cols, key=column_index_from_string) if cols else None

    fx_last = _last_present(C.FX_SOURCE_BLOCKS)
    consol_first = _first_present(C.CONSOL_SOURCE_BLOCKS)
    consol_last = _last_present(C.CONSOL_SOURCE_BLOCKS)

    # MA-rate lookup spans a safe fixed window so extra currencies are covered.
    rate_last_row = max(rates.last_row,
                        rates.first_row + cfg.rate_lookup_rows - 1)

    wb = Workbook()
    ws = wb.active
    ws.title = C.SHEET_CHECK

    # ---- header band ------------------------------------------------------
    ws[f"{COL_ACCOUNT}{ROW_CURR}"] = "GLACCOUNT"
    ws[f"{COL_DESC}{ROW_CURR}"] = "GL Description | Currency"
    ws[f"{COL_DESC}{ROW_TB_LABEL}"] = C.SHEET_TB
    ws[f"{COL_DESC}{ROW_AGG_LABEL}"] = C.SHEET_AGG
    ws[f"{COL_DESC}{ROW_SUMDIFF}"] = "Sum of Differences"
    for cell in (f"{COL_DESC}{ROW_TB_LABEL}", f"{COL_DESC}{ROW_AGG_LABEL}",
                 f"{COL_DESC}{ROW_SUMDIFF}"):
        ws[cell].font = HELP_FONT

    ent_by_valuecol: dict[str, str] = {}      # LC-Balance value col -> code
    for info in layout.cols:
        ws[f"{info.value_col}{ROW_BLOCKLABEL}"] = info.block
        ws[f"{info.value_col}{ROW_SUBHEADER}"] = info.sub
        ws[f"{info.value_col}{ROW_BLOCKLABEL}"].font = HDR_FONT
        if info.is_entity:
            e = next(en for en in report.entities if en.code == info.sub)
            ws[f"{info.value_col}{ROW_ENTNAME}"] = e.name
            ws[f"{info.value_col}{ROW_CURR}"] = e.currency
            if info.block == C.CHECK_LC_BALANCE:
                ent_by_valuecol[info.value_col] = e.code

    # ---- per-entity helper cells -----------------------------------------
    for info in layout.cols:
        if not (info.is_entity and info.diff_col):
            continue
        d, v = info.diff_col, info.value_col
        if info.block == C.CHECK_LC_BALANCE:
            ws[f"{d}{ROW_TB_LABEL}"] = (
                f"=MATCH({v}${ROW_SUBHEADER},'{C.SHEET_TB}'!"
                f"$A${tb.header_row}:${tb_lastcol}${tb.header_row},0)")
            if agg:
                ws[f"{d}{ROW_AGG_LABEL}"] = (
                    f"=MATCH({v}${ROW_SUBHEADER},'{C.SHEET_AGG}'!"
                    f"${agg_mfirst}${agg.subheader_row}:${agg_mlast}${agg.subheader_row},0)")
        elif info.block == C.CHECK_GC_BALANCE:
            rng = (f"${rate_from}${rates.first_row}:"
                   f"${rate_to}${rate_last_row}")
            local = (f"VLOOKUP({v}${ROW_CURR},'{C.SHEET_RATES}'!{rng},"
                     f"{rates.col_index},FALSE)")
            # The rates table quotes every currency TO the base (e.g. INR). If
            # the group currency is itself the base, it has no row of its own,
            # so the rate already converts to it (divisor = 1). Only when the
            # group currency is a quoted currency (e.g. USD) do we cross-divide.
            if gc_currency in rates.rates:
                ws[f"{d}{ROW_AGG_LABEL}"] = (
                    f"={local}/VLOOKUP(\"{gc_currency}\",'{C.SHEET_RATES}'!{rng},"
                    f"{rates.col_index},FALSE)")
            else:
                ws[f"{d}{ROW_AGG_LABEL}"] = f"={local}"
        for hr in (ROW_TB_LABEL, ROW_AGG_LABEL):
            if ws[f"{d}{hr}"].value is not None:
                ws[f"{d}{hr}"].font = HELP_FONT

    # ---- data rows --------------------------------------------------------
    diff_cols: set[str] = set()
    last_data_row = FIRST_DATA_ROW - 1
    for i, row in enumerate(report.rows):
        r = FIRST_DATA_ROW + i
        last_data_row = r
        rule = cfg.effective_rule(row.category)
        ws[f"{COL_CATEGORY}{r}"] = row.category or None
        if not row.is_subtotal:
            if rule and rule.classification:
                ws[f"{COL_CLASS}{r}"] = rule.classification
            ws[f"{COL_ACCOUNT}{r}"] = row.account
            ws[f"{COL_DESC}{r}"] = row.description

        # value cells (verbatim from the report)
        for info in layout.cols:
            val = row.values.get((info.block, info.sub))
            if val is not None:
                ws[f"{info.value_col}{r}"] = val

        # difference formulas
        for info in layout.cols:
            if not (info.is_entity and info.diff_col):
                continue
            d, v = info.diff_col, info.value_col
            diff_cols.add(d)
            f = _diff_formula(info, r, d, v, rule, agg, tb, layout,
                              fx_last, consol_first, consol_last,
                              row.is_subtotal, consol_ref,
                              cfg.is_pl_account(row.account))
            if f:
                ws[f"{d}{r}"] = f

    # ---- sum-of-differences row ------------------------------------------
    for d in sorted(diff_cols):
        ws[f"{d}{ROW_SUMDIFF}"] = f"=SUM({d}{FIRST_DATA_ROW}:{d}{last_data_row})"

    # ---- net-profit reconciliation block ---------------------------------
    _net_profit_block(ws, report, layout, tb, last_data_row, cfg)

    # ---- highlighting + cosmetics ----------------------------------------
    _highlight(ws, diff_cols, FIRST_DATA_ROW, last_data_row, cfg.tolerance)
    _cosmetics(ws, layout)

    # ---- derived Minority Interest sheet ---------------------------------
    _minority_sheet(wb, report, layout, cfg)

    # ---- embed the source sheets -----------------------------------------
    _embed(wb, inputs.read_sheet(tb_path, C.SHEET_TB))
    if agg_path:
        _embed(wb, inputs.read_sheet(agg_path, C.SHEET_AGG))
    _embed(wb, inputs.read_sheet(rates_path, C.SHEET_RATES))
    if consol_path:
        _embed(wb, inputs.read_sheet(consol_path, C.SHEET_CONSOL,
                                     sheet_name=consol.sheet_name))
    return wb


MINORITY_SHEET = "Minority Interest"


def _minority_sheet(ws_wb, report: ReportTable, layout: CheckLayout,
                    cfg: C.CheckConfig) -> None:
    """Add the Minority Interest sheet, derived from the Check sheet.

    Per company code (every entity in the LC section):
      Net Profit as per GC Bal  = GC-Balance of the Net Profit line
      Div received - <acct>     = GC-Balance of the dividend GL (0 if absent)
      Profit before Div         = the two added
      Minority - Total          = GC-Total of the Minority Interest line
      Current Period %          = Minority-Total / Profit-before-Div
    """
    # needs the GC-Balance and GC-Total blocks, plus Net Profit / Minority lines
    if (C.CHECK_GC_BALANCE not in layout.block_first_value
            or C.CHECK_GC_TOTAL not in layout.block_first_value):
        return

    def _norm(s):
        return str(s).strip().lower()

    def section_row(pred):
        idxs = [i for i, r in enumerate(report.rows) if pred(r.category)]
        if not idxs:
            return None
        subs = [i for i in idxs if report.rows[i].is_subtotal]
        return FIRST_DATA_ROW + (subs[0] if subs else idxs[0])

    np_row = section_row(lambda c: _norm(c) == "net profit")
    # fuzzy so a misspelled "Minority Interest" section still feeds the sheet
    mi_row = section_row(cfg.is_minority)
    if np_row is None or mi_row is None:
        return
    div_idx = next((i for i, r in enumerate(report.rows)
                    if inputs.account_key(r.account) == cfg.dividend_account),
                   None)
    div_row = FIRST_DATA_ROW + div_idx if div_idx is not None else None

    ws = ws_wb.create_sheet(MINORITY_SHEET)
    headers = ["Code", "Net Profit as per GC Bal",
               f"Div received - {cfg.dividend_account}", "Profit before Div",
               "Minority - Total", "Current Period %"]
    for j, h in enumerate(headers, start=1):
        ws.cell(1, j, h).font = HDR_FONT

    chk = C.SHEET_CHECK
    for k, e in enumerate(report.entities):
        row = 2 + k
        gcbal = layout.value_col(C.CHECK_GC_BALANCE, e.code)
        gctot = layout.value_col(C.CHECK_GC_TOTAL, e.code)
        ws.cell(row, 1, e.code)
        ws.cell(row, 2).value = f"='{chk}'!{gcbal}{np_row}"
        ws.cell(row, 3).value = (f"='{chk}'!{gcbal}{div_row}"
                                 if div_row else 0)
        ws.cell(row, 4).value = f"=B{row}+C{row}"
        ws.cell(row, 5).value = f"='{chk}'!{gctot}{mi_row}"
        ws.cell(row, 6).value = f"=IFERROR(E{row}/D{row},0)"
        ws.cell(row, 6).number_format = "0.00%"

    for col, w in {"A": 12, "B": 24, "C": 22, "D": 18, "E": 18,
                   "F": 16}.items():
        ws.column_dimensions[col].width = w


def _diff_formula(info, r, d, v, rule, agg, tb, layout: CheckLayout,
                  fx_last, consol_first, consol_last,
                  is_subtotal=False, consol_ref=None, is_pl=True) -> str | None:
    """The difference formula for one cell, by block type."""
    if info.block == C.CHECK_LC_CONSOL:
        # tie LC - Consol back to the manual entry tracker. For this
        # company+account (comp-code & account = the tracker's "Concatenate"
        # key) take the latest month's NET posting = Debit - Credit: the Dr
        # leg (charge) is in the left column, the "To ..." Cr leg in the right;
        # the report carries credit legs as negative, so the credit column is
        # subtracted. Only P&L-series accounts (leading digit 1/2/3) hit the
        # P&L; subtotal rows have no account to match.
        if is_subtotal or consol_ref is None or not is_pl:
            return None
        concat_rng, val_rngs = consol_ref
        key = f"{v}${ROW_SUBHEADER}&$C{r}"
        terms = f"SUMIF({concat_rng},{key},{val_rngs[0]})"
        if len(val_rngs) > 1:
            terms += f"-SUMIF({concat_rng},{key},{val_rngs[1]})"
        return f"={terms}-{v}{r}"

    if info.block == C.CHECK_LC_BALANCE:
        # subtotal rows have no GL account to look up — no LC tie-out
        if is_subtotal or rule is None or not rule.source:
            return None
        match = f"{C.SHEET_CHECK}!{d}${ROW_TB_LABEL}"
        if rule.source == "tb":
            table = (f"'{C.SHEET_TB}'!$A${tb.header_row}:"
                     f"${get_column_letter(tb.last_col)}${tb.last_data_row}")
            return f"=IFERROR(VLOOKUP($C{r},{table},{match},FALSE),0)-{v}{r}"
        blk = agg.blocks.get(rule.source) if agg else None
        if blk is None:
            return None
        match = f"{C.SHEET_CHECK}!{d}${ROW_AGG_LABEL}"
        table = (f"'{C.SHEET_AGG}'!${get_column_letter(blk.acct_col)}"
                 f"${blk.subheader_row}:${get_column_letter(blk.last_entity_col)}"
                 f"${blk.last_data_row}")
        return f"=IFERROR(VLOOKUP($C{r},{table},{match},FALSE),0)-{v}{r}"

    if info.block == C.CHECK_GC_BALANCE:
        if fx_last is None:
            return None
        rate = f"{C.SHEET_CHECK}!{d}${ROW_AGG_LABEL}"
        rng_hdr = f"${COL_CLASS}${ROW_SUBHEADER}:${fx_last}${ROW_SUBHEADER}"
        rng_row = f"${COL_CLASS}{r}:${fx_last}{r}"
        return (f"=SUMIF({rng_hdr},{v}${ROW_SUBHEADER},{rng_row})*{rate}-{v}{r}")

    if info.block == C.CHECK_GC_TOTAL:
        if consol_first is None or consol_last is None:
            return None
        rng_hdr = f"${consol_first}${ROW_SUBHEADER}:${consol_last}${ROW_SUBHEADER}"
        rng_row = f"${consol_first}{r}:${consol_last}{r}"
        return f"=SUMIF({rng_hdr},{v}${ROW_SUBHEADER},{rng_row})-{v}{r}"
    return None


def _net_profit_block(ws, report: ReportTable, layout: CheckLayout, tb,
                      last_data_row, cfg: C.CheckConfig) -> None:
    start = last_data_row + 2
    r_inc, r_exp, r_np = start, start + 1, start + 2
    r_calc = start + 4
    r_tbnp = start + 6
    r_chk = start + 7
    ws[f"{COL_DESC}{r_inc}"] = "Income"
    ws[f"{COL_DESC}{r_exp}"] = "Expense"
    ws[f"{COL_DESC}{r_np}"] = "Net Profit"
    ws[f"{COL_DESC}{r_calc}"] = "Calc Check"
    ws[f"{COL_DESC}{r_tbnp}"] = "Net Profit as per Real Time TB"
    ws[f"{COL_DESC}{r_chk}"] = "Check"
    # TB net profit is summed live from the P&L-series GLs (leading digit 1/2/3)
    # using a text-safe SUMPRODUCT, so it works whether the TB stores account
    # numbers as numbers or as text, and tracks GLs being added / removed.
    acct = get_column_letter(tb.acct_col)
    first, last = tb.first_data_row, tb.last_data_row
    acct_rng = f"'{C.SHEET_TB}'!${acct}${first}:${acct}${last}"
    series_test = "+".join(
        f'(LEFT(TRIM({acct_rng}),1)="{d}")' for d in cfg.pl_series)
    for e in report.entities:
        v = layout.value_col(C.CHECK_LC_BALANCE, e.code)   # value column (E/G/I)
        d = layout.diff_col(C.CHECK_LC_BALANCE, e.code)    # output column (F/H/J)
        ws[f"{d}{r_inc}"] = f"=SUMIF($A:$A,$D{r_inc},{v}:{v})"
        ws[f"{d}{r_exp}"] = f"=SUMIF($A:$A,$D{r_exp},{v}:{v})"
        ws[f"{d}{r_np}"] = f"=SUMIF($A:$A,$D{r_np},{v}:{v})"
        ws[f"{d}{r_calc}"] = f"={d}{r_inc}+{d}{r_exp}+{d}{r_np}"
        # Only sum the TB when this company actually exists there; otherwise the
        # net profit per TB is undefined (0), not the first company's figure.
        if e.code in tb.entity_cols:
            ent = get_column_letter(tb.entity_cols[e.code])
            ent_rng = f"'{C.SHEET_TB}'!${ent}${first}:${ent}${last}"
            ws[f"{d}{r_tbnp}"] = f"=SUMPRODUCT(({series_test})*({ent_rng}))"
        else:
            ws[f"{d}{r_tbnp}"] = 0
        ws[f"{d}{r_chk}"] = f"={d}{r_np}+{d}{r_tbnp}"
    for rr in (r_inc, r_exp, r_np, r_calc, r_tbnp, r_chk):
        ws[f"{COL_DESC}{rr}"].font = HDR_FONT


def _highlight(ws, diff_cols, first_row, last_row, tol) -> None:
    # data rows, the Sum-of-Differences row, and the bottom block share the
    # difference columns; flag any cell whose absolute value exceeds tolerance.
    for d in diff_cols:
        top = f"{d}{ROW_SUMDIFF}"
        rng = f"{d}{ROW_SUMDIFF}:{d}{last_row + 12}"
        # the formula is relative to the top-left of the range, so it adjusts
        # row-by-row down the column.
        rule = FormulaRule(formula=[f"AND({top}<>\"\",ABS({top})>{tol})"],
                           fill=RED_FILL, font=RED_FONT)
        ws.conditional_formatting.add(rng, rule)


def _cosmetics(ws, layout: CheckLayout) -> None:
    ws.freeze_panes = f"{get_column_letter(5)}{FIRST_DATA_ROW}"
    widths = {COL_CLASS: 16, COL_CATEGORY: 22, COL_ACCOUNT: 11, COL_DESC: 34}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for info in layout.cols:
        ws.column_dimensions[info.value_col].width = 15
        if info.diff_col:
            ws.column_dimensions[info.diff_col].width = 11
            ws[f"{info.diff_col}{ROW_BLOCKLABEL}"] = "Diff"
            ws[f"{info.diff_col}{ROW_BLOCKLABEL}"].font = HELP_FONT
