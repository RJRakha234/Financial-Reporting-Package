"""Command line: ``python -m fincheck statements.pdf`` (check totals) and
``python -m fincheck compare old.pdf new.pdf`` (exact two-PDF comparison)."""

import argparse
import sys
from pathlib import Path

from . import DEFAULT_DPI, analyze, compare
from .compare_report import comparison_to_console, comparison_to_json
from .diffmark import write_diff_pdf
from .report import to_console, to_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck",
        description="Check totals and subtotals in a financial-statement PDF "
        "and produce a highlighted PDF of any inconsistencies.",
        epilog="To compare two PDFs exactly instead: "
        "fincheck compare OLD.pdf NEW.pdf  (see 'fincheck compare -h')",
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


def build_compare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck compare",
        description="Compare two PDFs exactly. Tests equality of the bytes, the "
        "page geometry, every text span the file draws (string, font, size, "
        "colour and position), every vector path and image, and the rendered "
        "pixels. No layout, row or column is inferred.",
    )
    parser.add_argument("pdf_a", help="reference PDF")
    parser.add_argument("pdf_b", help="PDF to compare against it")
    parser.add_argument(
        "-o",
        "--output",
        help="path for a marked-up copy of the second PDF "
        "(default: <pdf_b>.diff.pdf; use 'none' to skip)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"resolution for the rendered-pixel check (default: {DEFAULT_DPI})",
    )
    parser.add_argument(
        "--no-render",
        action="store_true",
        help="skip the pixel check; leaves visual equality undetermined",
    )
    parser.add_argument(
        "--position-tolerance",
        type=float,
        default=0.0,
        help="coordinate slack in points; 0 (default) compares positions exactly",
    )
    parser.add_argument(
        "--align-pages",
        action="store_true",
        help="match pages by content instead of by position, for inserted or "
        "removed pages (positional matching assumes nothing, so it is default)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the comparison as JSON"
    )
    return parser


def compare_main(argv: list[str]) -> int:
    args = build_compare_parser().parse_args(argv)

    paths = [Path(args.pdf_a), Path(args.pdf_b)]
    for path in paths:
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    if args.output is None:
        output = str(paths[1].with_suffix(".diff.pdf"))
    elif args.output.lower() == "none":
        output = None
    else:
        output = args.output

    result = compare(
        str(paths[0]),
        str(paths[1]),
        output_pdf=None,  # only write once we know there is something to show
        dpi=None if args.no_render else args.dpi,
        position_tolerance=args.position_tolerance,
        align_pages=args.align_pages,
    )

    written, omitted = None, 0
    if output is not None and not result.identical:
        written, omitted = write_diff_pdf(result, output)

    if args.json:
        print(comparison_to_json(result))
    else:
        print(comparison_to_console(result))
        if written:
            print(f"\nMarked-up PDF written to: {written}")
            if omitted:
                print(
                    f"  {omitted:,} text changes are listed above but not drawn "
                    "in it — too many to mark legibly."
                )
        elif output is not None:
            print("\nNo differences to mark up, so no diff PDF was written.")

    # Non-zero when the documents differ, for use in CI or a release gate.
    return 0 if result.identical else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "compare":
        return compare_main(argv[1:])

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
