"""plcheck — automate the IFRS INR P&L reconciliation ("check") file.

Given the P&L report and the three sources it was built from (Real Time TB,
Aggregate Expenses, MA Rates), :func:`analyze` reconciles them and writes a
self-contained, formula-driven check workbook with any differences highlighted.

    from plcheck import analyze
    result = analyze("PL_Report.xlsx", "TB.xlsx", "AggExp.xlsx", "Rates.xlsx",
                     output_path="PL_Report_Check.xlsx")
    print(result.ok)             # True if everything reconciles
    for d in result.evaluation.flagged():
        print(d.account, d.entity, d.delta)
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as C
from . import inputs
from .config import CategoryRule, CheckConfig
from .evaluate import Evaluation, evaluate
from .model import ReportTable
from .workbook import build_check_workbook

__all__ = ["analyze", "AnalysisResult", "CheckConfig", "CategoryRule",
           "Evaluation"]


@dataclass
class AnalysisResult:
    report: ReportTable
    evaluation: Evaluation
    output_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.evaluation.ok


def analyze(report_path: str, tb_path: str, agg_path: str, rates_path: str,
            output_path: str | None = None,
            cfg: CheckConfig | None = None) -> AnalysisResult:
    """Reconcile the report against its sources and optionally write the check.

    Args:
        report_path: the IFRS INR P&L report to check.
        tb_path: Real Time Trial Balance.
        agg_path: Aggregate Expenses report.
        rates_path: MA exchange-rate table.
        output_path: where to write the generated check workbook (optional).
        cfg: overrides for the category mapping / tolerance.
    """
    cfg = cfg or CheckConfig()
    report = inputs.read_report(report_path)
    ev = evaluate(report, tb_path, agg_path, rates_path, cfg)
    written = None
    if output_path is not None:
        wb = build_check_workbook(report, tb_path, agg_path, rates_path, cfg)
        wb.save(output_path)
        written = output_path
    return AnalysisResult(report=report, evaluation=ev, output_path=written)
