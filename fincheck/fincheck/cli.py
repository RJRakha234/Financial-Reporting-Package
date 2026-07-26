"""Command line: ``python -m fincheck statements.pdf`` (check totals) and
``python -m fincheck compare old.pdf new.pdf`` (exact two-PDF comparison)."""

import argparse
import sys
from pathlib import Path

from . import DEFAULT_DPI, analyze, compare, side_by_side
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
        "--side-by-side",
        metavar="HTML",
        help="also write an HTML page placing every paragraph and table beside "
        "its counterpart, matched by content. This reconstructs paragraphs and "
        "tables from the page geometry, so unlike the checks above it infers "
        "structure: read it as a worksheet, not as proof",
    )
    parser.add_argument(
        "--label-a",
        metavar="NAME",
        help="column heading for the first PDF in the side-by-side page "
        "(default: how it was produced, e.g. 'Excel export' or 'HTML print')",
    )
    parser.add_argument(
        "--label-b", metavar="NAME", help="column heading for the second PDF"
    )
    parser.add_argument(
        "--marked-pdfs",
        action="store_true",
        help="also write a numbered copy of each input PDF beside the "
        "side-by-side page, with every compared section outlined and stamped "
        "with its number from the report; page references in the report then "
        "link into them",
    )
    parser.add_argument(
        "--ignore-marks",
        action="store_true",
        help="ignore section numbers written into the PDFs' highlight comments; "
        "by default content marked 'n' is compared only against content marked "
        "'n', which stops a passage being shown against a blank when a "
        "counterpart exists",
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

    markup = None
    if output is not None and not result.identical:
        markup = write_diff_pdf(result, output)

    aligned = None
    if args.side_by_side:
        copies = (None, None)
        if args.marked_pdfs:
            beside = Path(args.side_by_side).parent
            copies = tuple(
                str(beside / f"{p.stem}.marked.pdf") for p in paths
            )
        aligned = side_by_side(
            str(paths[0]),
            str(paths[1]),
            output_html=args.side_by_side,
            marked_pdf_a=copies[0],
            marked_pdf_b=copies[1],
            label_a=args.label_a,
            label_b=args.label_b,
            use_marks=not args.ignore_marks,
        )

    if args.json:
        print(comparison_to_json(result))
    else:
        print(comparison_to_console(result))
        if markup is not None:
            print(f"\nMarked-up PDF written to: {markup.path}")
            if markup.unmarked_pages:
                print(
                    f"  {markup.unmarked_pages} page(s) left unmarked: their text "
                    f"differs wholesale ({markup.omitted_changes:,} changes), so "
                    "marking every line would obscure rather than show."
                )
        elif output is not None:
            print("\nNo differences to mark up, so no diff PDF was written.")

        if aligned is not None:
            s = aligned.summary
            print(f"\nSide-by-side written to: {aligned.output_html}")
            print(
                f"  {s.matched:,} passages matched by content "
                f"({s.paragraphs_matched:,} paragraphs, {s.rows_matched:,} table rows); "
                f"{s.changed:,} differ, {s.changed_figures:,} figure cells differ."
            )
            print(
                f"  {s.only_in_a:,} only in A, {s.only_in_b:,} only in B. "
                "This view infers paragraphs and tables — treat it as a worksheet."
            )
            for path in aligned.marked_pdfs:
                print(f"  Numbered copy written to: {path}")
            if aligned.marked_sections:
                blanks = sum(
                    1
                    for sec in aligned.sections
                    if sec.marked is not None
                    for pair in sec.pairs
                    if pair.a is None or pair.b is None
                )
                print(
                    f"  Honoured {len(aligned.marked_sections)} section number(s) "
                    "marked in both files; "
                    + (
                        "none of them came out against a blank."
                        if not blanks
                        else f"{blanks} pair(s) in them still have no counterpart."
                    )
                )

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
