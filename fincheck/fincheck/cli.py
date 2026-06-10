"""Command-line interface: ``python -m fincheck statements.pdf``."""

import argparse
import sys
from pathlib import Path

from . import analyze
from .report import to_console, to_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck",
        description="Check totals and subtotals in a financial-statement PDF "
        "and produce a highlighted PDF of any inconsistencies.",
    )
    parser.add_argument("pdf", help="path to the financial statement PDF")
    parser.add_argument(
        "-o",
        "--output",
        help="path for the highlighted PDF "
        "(default: <input>.highlighted.pdf; use 'none' to skip)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="absolute rounding slack before a total is flagged (default: 1.0)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON"
    )
    parser.add_argument(
        "--no-components",
        action="store_true",
        help="highlight only the totals, not the figures summed into them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    src = Path(args.pdf)
    if not src.is_file():
        print(f"error: file not found: {src}", file=sys.stderr)
        return 2

    if args.output is None:
        output = str(src.with_suffix(".highlighted.pdf"))
    elif args.output.lower() == "none":
        output = None
    else:
        output = args.output

    result = analyze(
        str(src),
        output_pdf=output,
        tolerance=args.tolerance,
        show_components=not args.no_components,
    )

    if args.json:
        print(to_json(result.issues))
    else:
        print(to_console(result.issues))
        print(
            f"\nCoverage: checked {result.totals_checked} totals/subtotals "
            f"covering {result.figures_checked} figures"
            + (
                f"; {result.unverified} could not be auto-verified"
                if result.unverified
                else ""
            )
            + "."
        )
        if result.output_pdf:
            print(f"Highlighted PDF written to: {result.output_pdf}")
            if not args.no_components:
                print(
                    "  yellow = checked figure · green = total foots · "
                    "red = does not foot · orange = unverified"
                )

    # Exit non-zero when inconsistencies are found (handy for CI / pipelines).
    return 1 if result.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
