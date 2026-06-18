"""Command-line interface: ``python -m finmatch a.pdf b.pdf``."""

import argparse
import sys
from pathlib import Path

from . import analyze
from .report import to_console, to_html, to_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="finmatch",
        description="Compare two financial-statement PDFs by language (ignoring "
        "numbers) and highlight matched vs unmatched lines. Built for checking "
        "that two currency versions (e.g. IFRS INR vs USD) carry identical "
        "wording.",
    )
    p.add_argument("pdf_a", help="first PDF (e.g. the INR statement)")
    p.add_argument("pdf_b", help="second PDF (e.g. the USD statement)")
    p.add_argument(
        "-o", "--output-a",
        help="highlighted output for A (default <input>.compared.pdf; "
        "'none' to skip)",
    )
    p.add_argument(
        "-O", "--output-b",
        help="highlighted output for B (default <input>.compared.pdf; "
        "'none' to skip)",
    )
    p.add_argument(
        "--keep-currency", action="store_true",
        help="do NOT mask currency symbols/unit words, so currency labels "
        "(INR/USD, crores/millions) are reported as differences",
    )
    p.add_argument(
        "--case-sensitive", action="store_true",
        help="treat differences in letter case as mismatches",
    )
    p.add_argument("--json", action="store_true", help="print a JSON report")
    p.add_argument(
        "--html", metavar="PATH",
        help="write a side-by-side HTML report to PATH",
    )
    p.add_argument(
        "--no-color", action="store_true", help="disable coloured console output"
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    for path in (args.pdf_a, args.pdf_b):
        if not Path(path).is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    result = analyze(
        args.pdf_a,
        args.pdf_b,
        output_a=args.output_a,
        output_b=args.output_b,
        mask_currency=not args.keep_currency,
        ignore_case=not args.case_sensitive,
    )

    if args.html:
        Path(args.html).write_text(
            to_html(result, args.pdf_a, args.pdf_b), encoding="utf-8"
        )

    if args.json:
        print(to_json(result, args.pdf_a, args.pdf_b))
    else:
        color = sys.stdout.isatty() and not args.no_color
        print(to_console(result, args.pdf_a, args.pdf_b, color=color))
        out_a = getattr(result, "output_a", None)
        out_b = getattr(result, "output_b", None)
        if out_a and out_b:
            print(f"\nHighlighted PDFs written to:\n  {out_a}\n  {out_b}")
        if args.html:
            print(f"HTML report written to: {args.html}")

    return 0 if result.consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
