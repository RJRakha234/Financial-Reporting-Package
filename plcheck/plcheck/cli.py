"""Command-line interface: ``python -m plcheck`` ."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze
from .config import CategoryRule, CheckConfig
from .report import to_console, to_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plcheck",
        description="Reconcile a function-wise consolidation P&L report "
        "(e.g. IFRS INR or Ind-AS Function-wise) against its sources and "
        "generate a highlighted, formula-driven check workbook.",
    )
    p.add_argument("report",
                   help="the P&L report to check, IFRS INR or Ind-AS "
                        "Function-wise (.xlsx)")
    p.add_argument("--tb", required=True, help="Real Time Trial Balance (.xlsx)")
    p.add_argument("--agg", help="Aggregate Expenses (.xlsx). Omit for a "
                   "Nature-wise report, where every line ties to the TB.")
    p.add_argument("--rates", required=True, help="MA exchange rates (.xlsx)")
    p.add_argument("--consol", help="consolidation-entry tracker (.xlsx). When "
                   "given, LC - Consol is tied out to it (comp-code + account).")
    p.add_argument("-o", "--output",
                   help="path for the generated check workbook "
                        "(default: <report>_Check.xlsx; 'none' to skip)")
    p.add_argument("--gc-currency", default="",
                   help="group/consolidation currency, e.g. USD "
                        "(auto-detected from the report if omitted)")
    p.add_argument("--tolerance", type=float, default=0.5,
                   help="absolute slack before a difference is flagged "
                        "(default: 0.5)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--html", nargs="?", const="auto", default=None,
                   help="also write a bonus HTML error summary (A PL check, "
                        "B Minority, C LC-Consol, D Entity reconciler). Give a "
                        "path, or just --html to write <report>_Check.html.")
    p.add_argument("--html-min", type=float, default=1.0,
                   help="ignore differences smaller than this in the HTML "
                        "summary (default: 1)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths = {"report": args.report, "TB": args.tb, "MA rates": args.rates}
    if args.agg:
        paths["Aggregate Exp"] = args.agg
    if args.consol:
        paths["Consol Entries"] = args.consol
    for label, path in paths.items():
        if not Path(path).is_file():
            print(f"error: {label} file not found: {path}", file=sys.stderr)
            return 2

    src = Path(args.report)
    if args.output is None:
        output = str(src.with_name(src.stem + "_Check.xlsx"))
    elif args.output.lower() == "none":
        output = None
    else:
        output = args.output

    cfg = CheckConfig(tolerance=args.tolerance, gc_currency=args.gc_currency)
    if not args.agg:
        # Nature-wise report: no Aggregate Exp, every expense line ties to the TB
        cfg.default_rule = CategoryRule("Expense", "tb")

    try:
        result = analyze(args.report, args.tb, args.agg, args.rates,
                         output_path=output, cfg=cfg, consol_path=args.consol)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(to_json(result.evaluation))
    else:
        print(to_console(result.evaluation))
        if result.output_path:
            print(f"\nCheck workbook written to: {result.output_path}")
            print("  Open in Excel to recalculate; non-zero differences are "
                  "highlighted red.")

    # --- bonus HTML error summary (optional, isolated) --------------------
    if args.html is not None:
        from . import inputs
        from .htmlreport import build_html
        if args.html == "auto":
            html_path = str(src.with_name(src.stem + "_Check.html"))
        else:
            html_path = args.html
        doc = build_html(
            result.report, result.evaluation, title=src.stem,
            tb_codes=inputs.company_codes(args.tb),
            agg_codes=inputs.company_codes(args.agg) if args.agg else [],
            is_minority=cfg.is_minority, min_amount=args.html_min)
        Path(html_path).write_text(doc, encoding="utf-8")
        if not args.json:
            print(f"HTML error summary written to: {html_path}")

    return 1 if not result.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
