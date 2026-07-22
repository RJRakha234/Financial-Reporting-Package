"""Command-line interface.

Two commands:

* ``python -m fincheck check statements.pdf`` — verify totals/subtotals foot and
  write a highlighted PDF (the original behaviour; ``check`` may be omitted, so
  ``python -m fincheck statements.pdf`` still works).
* ``python -m fincheck compare original.pdf scanned.pdf`` — compare the text of a
  reference PDF against a scanned copy and report every textual and numeric
  difference.
"""

import argparse
import sys
from pathlib import Path

from . import analyze, compare_documents
from .compare import OcrUnavailable
from .report import (
    comparison_to_console,
    comparison_to_json,
    to_console,
    to_json,
)

_COMMANDS = {"check", "compare"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck",
        description="Check totals in a financial-statement PDF, or compare a "
        "reference PDF against a scanned copy.",
    )
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser(
        "check",
        help="check totals/subtotals foot and highlight inconsistencies",
        description="Check totals and subtotals in a financial-statement PDF "
        "and produce a highlighted PDF of any inconsistencies.",
    )
    check.add_argument("pdf", help="path to the financial statement PDF")
    check.add_argument(
        "-o",
        "--output",
        help="path for the highlighted PDF "
        "(default: <input>.highlighted.pdf; use 'none' to skip)",
    )
    check.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="absolute rounding slack before a total is flagged (default: 1.0)",
    )
    check.add_argument(
        "--json", action="store_true", help="print the report as JSON"
    )
    check.add_argument(
        "--no-components",
        action="store_true",
        help="highlight only the totals, not the figures summed into them",
    )

    compare = sub.add_parser(
        "compare",
        help="compare a reference PDF against a scanned copy",
        description="Compare the text of a reference PDF against a scanned copy "
        "(image PDF/image, OCR'd as needed) and report differing text and "
        "figures.",
    )
    compare.add_argument("reference", help="text-based reference PDF")
    compare.add_argument(
        "scanned", help="scanned document to compare (image PDF, image, or PDF)"
    )
    compare.add_argument(
        "--ocr",
        choices=["auto", "always", "never"],
        default="auto",
        help="OCR pages with no text layer: auto (default), always, or never",
    )
    compare.add_argument(
        "--lang",
        default="eng",
        help="tesseract language code(s) for OCR (default: eng)",
    )
    compare.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="render resolution when a page must be OCR'd (default: 300)",
    )
    compare.add_argument(
        "--case-sensitive",
        action="store_true",
        help="do not fold case when diffing the text",
    )
    compare.add_argument(
        "--json", action="store_true", help="print the report as JSON"
    )
    return parser


def _run_check(args) -> int:
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


def _run_compare(args) -> int:
    reference = Path(args.reference)
    scanned = Path(args.scanned)
    for path in (reference, scanned):
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    try:
        result = compare_documents(
            str(reference),
            str(scanned),
            ocr=args.ocr,
            lang=args.lang,
            dpi=args.dpi,
            ignore_case=not args.case_sensitive,
        )
    except OcrUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(comparison_to_json(result))
    else:
        print(comparison_to_console(result))

    # Exit non-zero when the documents differ (handy for CI / pipelines).
    return 0 if result.identical else 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Backward compatibility: `fincheck statements.pdf` (no subcommand) still runs
    # the footing check. Only rewrite when the first token is not a flag.
    if argv and argv[0] not in _COMMANDS and not argv[0].startswith("-"):
        argv = ["check", *argv]

    args = build_parser().parse_args(argv)
    if args.command == "compare":
        return _run_compare(args)
    if args.command == "check":
        return _run_check(args)

    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
