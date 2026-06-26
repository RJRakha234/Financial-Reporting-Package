"""Evaluate the three reconciliations in Python.

This is independent of Excel: it recomputes the same differences the generated
formulas would, so the CLI can print a pass/fail summary and drive the
red-highlighting without needing an Excel engine to open the workbook.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config as C
from . import inputs
from .model import ReportTable


@dataclass
class Diff:
    kind: str            # "lc" | "fx" | "consol"
    row_index: int       # index into report.rows
    category: str
    account: object
    description: str
    entity: str          # entity code
    expected: float
    stated: float

    @property
    def delta(self) -> float:
        return self.expected - self.stated


@dataclass
class NetProfitCheck:
    entity: str
    income: float
    expense: float
    net_profit: float
    tb_net_profit: float

    @property
    def calc_check(self) -> float:        # internal P&L integrity (~0)
        return self.income + self.expense + self.net_profit

    @property
    def tie_check(self) -> float:         # report vs TB net profit (~0)
        return self.net_profit + self.tb_net_profit


@dataclass
class Evaluation:
    diffs: list[Diff] = field(default_factory=list)
    net_profit: list[NetProfitCheck] = field(default_factory=list)
    unmapped_categories: list[str] = field(default_factory=list)
    missing_blocks: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    report_errors: list[str] = field(default_factory=list)
    tolerance: float = 0.5

    def flagged(self) -> list[Diff]:
        return [d for d in self.diffs if abs(d.delta) > self.tolerance]

    def net_profit_flagged(self) -> list[NetProfitCheck]:
        return [n for n in self.net_profit
                if abs(n.calc_check) > self.tolerance
                or abs(n.tie_check) > self.tolerance]

    @property
    def ok(self) -> bool:
        return not self.flagged() and not self.net_profit_flagged()


def evaluate(report: ReportTable, tb_path: str, agg_path: str, rates_path: str,
             cfg: C.CheckConfig | None = None) -> Evaluation:
    cfg = cfg or C.CheckConfig()
    codes = [e.code for e in report.entities]
    currency = {e.code: e.currency for e in report.entities}

    tb_accounts, _ = inputs.tb_values(tb_path, codes)
    agg = inputs.agg_values(agg_path, codes)
    rates = inputs.parse_rates(rates_path).rates
    tb_entity_cols = inputs.parse_tb(tb_path, codes).entity_cols

    # Net profit per TB = column-wise sum of the P&L-series GLs (computed from
    # the accounts themselves, so it adapts to GLs being added or removed and
    # excludes balance-sheet accounts).
    tb_sums = {e.code: 0.0 for e in report.entities}
    for acct, by_entity in tb_accounts.items():
        if cfg.is_pl_account(acct):
            for code, val in by_entity.items():
                tb_sums[code] = tb_sums.get(code, 0.0) + val

    ev = Evaluation(tolerance=cfg.tolerance)
    unmapped: set[str] = set()

    for i, row in enumerate(report.rows):
        rule = cfg.rule_for(row.category)
        # --- 1. LC tie-out (detail rows with a known source) ---------------
        if not row.is_subtotal and row.category:
            if rule is None:
                unmapped.add(row.category)
            elif rule.source:
                acct = inputs.account_key(row.account)
                for e in report.entities:
                    stated = row.values.get((C.CHECK_LC_BALANCE, e.code), 0.0)
                    if rule.source == "tb":
                        expected = tb_accounts.get(acct, {}).get(e.code, 0.0)
                    else:
                        expected = agg.get(rule.source, {}).get(
                            acct, {}).get(e.code, 0.0)
                    ev.diffs.append(Diff("lc", i, row.category, row.account,
                                         row.description, e.code, expected, stated))

        # --- 2. FX conversion (every row) ----------------------------------
        for e in report.entities:
            lc = (row.values.get((C.CHECK_LC_BALANCE, e.code), 0.0)
                  + row.values.get(("LC - Consol", e.code), 0.0))
            rate = rates.get(e.currency, 0.0)
            expected = lc * rate
            stated = row.values.get((C.CHECK_GC_BALANCE, e.code), 0.0)
            ev.diffs.append(Diff("fx", i, row.category, row.account,
                                 row.description, e.code, expected, stated))

        # --- 3. GC consolidation (every row) -------------------------------
        for e in report.entities:
            expected = sum(row.values.get((b, e.code), 0.0)
                           for b in C.CONSOL_SOURCE_BLOCKS)
            stated = row.values.get((C.CHECK_GC_TOTAL, e.code), 0.0)
            ev.diffs.append(Diff("consol", i, row.category, row.account,
                                 row.description, e.code, expected, stated))

    # --- 4. Net-profit reconciliation, per entity --------------------------
    for e in report.entities:
        totals = {cls: 0.0 for cls in C.RECON_CLASSES}
        for row in report.rows:
            if row.is_subtotal:
                continue
            rule = cfg.rule_for(row.category)
            if rule and rule.classification in totals:
                totals[rule.classification] += row.values.get(
                    (C.CHECK_LC_BALANCE, e.code), 0.0)
        ev.net_profit.append(NetProfitCheck(
            entity=e.code,
            income=totals["Income"], expense=totals["Expense"],
            net_profit=totals["Net Profit"],
            tb_net_profit=tb_sums.get(e.code, 0.0),
        ))

    ev.unmapped_categories = sorted(unmapped)

    # Flag any expected reconciliation block that the report did not contain, so
    # a skipped check (e.g. GC - Total under a different heading) is visible.
    present = {b.label for b in report.blocks}
    expected = (C.CHECK_LC_BALANCE, C.CHECK_GC_BALANCE, C.CHECK_GC_TOTAL,
                *C.FX_SOURCE_BLOCKS, *C.CONSOL_SOURCE_BLOCKS)
    ev.missing_blocks = [b for b in dict.fromkeys(expected) if b not in present]

    # report companies with no matching column in the Real Time TB (their
    # "Net Profit as per Real Time TB" can't be computed and shows 0).
    ev.missing_entities = [e.code for e in report.entities
                           if e.code not in tb_entity_cols]
    ev.report_errors = list(report.errors)
    return ev
