"""Command-line interface: ``python -m plcheck`` ."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze
from .config import CheckConfig
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
    p.add_argument("--agg", required=True, help="Aggregate Expenses (.xlsx)")
    p.add_argument("--rates", required=True, help="MA exchange rates (.xlsx)")
    p.add_argument("-o", "--output",
                   help="path for the generated check workbook "
                        "(default: <report>_Check.xlsx; 'none' to skip)")
    p.add_argument("--tolerance", type=float, default=0.5,
                   help="absolute slack before a difference is flagged "
                        "(default: 0.5)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths = {"report": args.report, "TB": args.tb, "Aggregate Exp": args.agg,
             "MA rates": args.rates}
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

    try:
        result = analyze(args.report, args.tb, args.agg, args.rates,
                         output_path=output,
                         cfg=CheckConfig(tolerance=args.tolerance))
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

    return 1 if not result.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
