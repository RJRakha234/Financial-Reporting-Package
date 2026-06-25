"""Command-line interface.

Two subcommands:

* ``fincheck check  statements.pdf``           — foot every total/subtotal.
* ``fincheck compare published.pdf filed.html`` — compare the published PDF
  against the HTML filed with the SEC and comment every difference, page by
  page, on a copy of the PDF.

For backwards compatibility ``fincheck statements.pdf`` (no subcommand) still
runs the footing check.
"""

import argparse
import sys
from pathlib import Path

from . import analyze, compare
from .comparereport import to_console as compare_console
from .comparereport import to_html as compare_html
from .comparereport import to_json as compare_json
from .report import to_console, to_json

_SUBCOMMANDS = {"check", "compare"}


def _add_check_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("pdf", help="path to the financial statement PDF")
    p.add_argument(
        "-o",
        "--output",
        help="path for the highlighted PDF "
        "(default: <input>.highlighted.pdf; use 'none' to skip)",
    )
    p.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="absolute rounding slack before a total is flagged (default: 1.0)",
    )
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument(
        "--no-components",
        action="store_true",
        help="highlight only the totals, not the figures summed into them",
    )


def _add_compare_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("pdf", help="the published financial-statement PDF")
    p.add_argument("html", help="the HTML filed with the SEC (same document)")
    p.add_argument(
        "-o",
        "--output",
        help="path for the annotated PDF "
        "(default: <pdf>.compared.pdf; use 'none' to skip)",
    )
    p.add_argument(
        "--html-report",
        help="also write a standalone HTML report of the differences here",
    )
    p.add_argument(
        "--no-text",
        action="store_true",
        help="compare figures only, skip the wording comparison",
    )
    p.add_argument("--json", action="store_true", help="print the report as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck",
        description="Check totals in a financial PDF, or compare a published "
        "PDF against the HTML filed with the SEC.",
    )
    sub = parser.add_subparsers(dest="command")
    _add_check_args(sub.add_parser("check", help="foot totals/subtotals in a PDF"))
    _add_compare_args(
        sub.add_parser("compare", help="compare a published PDF against filed HTML")
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

    return 1 if result.issues else 0


def _run_compare(args) -> int:
    pdf = Path(args.pdf)
    html = Path(args.html)
    if not pdf.is_file():
        print(f"error: file not found: {pdf}", file=sys.stderr)
        return 2
    if not html.is_file():
        print(f"error: file not found: {html}", file=sys.stderr)
        return 2

    if args.output is None:
        output = str(pdf.with_suffix(".compared.pdf"))
    elif args.output.lower() == "none":
        output = None
    else:
        output = args.output

    result = compare(
        str(pdf),
        str(html),
        output_pdf=output,
        compare_text=not args.no_text,
    )

    if args.json:
        print(compare_json(result))
    else:
        print(compare_console(result))
        if result.output_pdf:
            print(f"\nAnnotated PDF written to: {result.output_pdf}")
            print(
                "  red = figure changed · orange = figure missing from HTML · "
                "blue = figure only in HTML · amber = wording differs"
            )

    if args.html_report:
        Path(args.html_report).write_text(compare_html(result), encoding="utf-8")
        if not args.json:
            print(f"HTML report written to: {args.html_report}")

    return 1 if result.differences else 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatibility: a bare path (no subcommand) means `check`.
    if raw and raw[0] not in _SUBCOMMANDS and not raw[0].startswith("-"):
        raw = ["check", *raw]

    args = build_parser().parse_args(raw)
    if args.command == "compare":
        return _run_compare(args)
    if args.command == "check":
        return _run_check(args)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
