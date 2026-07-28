"""Command line entry point: ``python -m finround table.csv --scale 1000``."""

from __future__ import annotations

import argparse
import csv
import sys

from .report import to_json
from .solver import round_table
from .table import Table
from .units import RoundingSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finround",
        description=(
            "Round a table of figures so that every total, subtotal, row total "
            "and column total still equals the sum of its rounded components."
        ),
    )
    parser.add_argument("csv_path", help="input CSV (first row headings, first column labels)")
    parser.add_argument("-o", "--output", help="write the rounded table here as CSV")
    parser.add_argument(
        "--scale", default="1", help="divide every figure by this first (e.g. 1000)"
    )
    parser.add_argument(
        "-d", "--decimals", type=int, help="decimal places to present (default 0)"
    )
    parser.add_argument(
        "--step", help="round to a multiple of this instead (e.g. 0.5, 25)"
    )
    parser.add_argument(
        "--total-rows",
        help="1-based rows that are totals, comma separated; overrides label detection",
    )
    parser.add_argument("--total-cols", help="1-based columns that are totals")
    parser.add_argument(
        "--no-detect",
        action="store_true",
        help="do not infer totals from labels (round each figure independently)",
    )
    parser.add_argument("--no-header", action="store_true", help="the CSV has no heading row")
    parser.add_argument("--no-index", action="store_true", help="the CSV has no label column")
    parser.add_argument(
        "--tolerance",
        default="0",
        help="slack allowed when deciding whether a stated total foots (default 0)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--quiet", action="store_true", help="print the table only")
    return parser


def _indices(text: str | None) -> list[int] | None:
    if text is None:
        return None
    return [int(part) - 1 for part in text.replace(" ", "").split(",") if part]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        table = Table.from_csv(
            args.csv_path, header=not args.no_header, index=not args.no_index
        )
        spec = RoundingSpec.make(
            scale=args.scale, decimals=args.decimals, step=args.step
        )
        result = round_table(
            table,
            spec,
            row_totals=_indices(args.total_rows),
            col_totals=_indices(args.total_cols),
            detect=not args.no_detect,
            tolerance=args.tolerance,
        )
    except (OSError, ValueError) as exc:
        print(f"finround: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(to_json(result))
    elif args.quiet:
        print(table.render(result.formatted()))
    else:
        print(result.report())

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(
                table.csv_rows(result.formatted(thousands=False))
            )
        if not args.json and not args.quiet:
            print(f"\nRounded table written to: {args.output}")

    return 0 if result.consistent else 1
