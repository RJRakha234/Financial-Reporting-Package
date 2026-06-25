"""Command line: ``pdfhtmlcompare published.pdf filed.html``."""

import argparse
import sys
from pathlib import Path

from .compare import compare
from .report import to_console, to_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdfhtmlcompare",
        description="Compare the financial tables of a published PDF against the "
        "HTML filed with the SEC: check numbers, wordings, and that no table line "
        "is missing. Writes a green-validated PDF and a ✓/✗ commented HTML.",
    )
    p.add_argument(
        "pdf", nargs="+",
        help="the published PDF(s), in order — several (e.g. auditor's report "
        "then statements) are concatenated to line up with one filed HTML",
    )
    p.add_argument("html", help="the HTML filed with the SEC (same document)")
    p.add_argument(
        "-o", "--out-pdf", dest="out_pdf",
        help="validated PDF, green where checked (default: <pdf>.validated.pdf; "
        "'none' to skip)",
    )
    p.add_argument(
        "--out-html",
        help="commented HTML, ✓/✗ on each financial row "
        "(default: <html>.commented.html; 'none' to skip)",
    )
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    return p


def _resolve(value, default):
    if value is None:
        return default
    return None if value.lower() == "none" else value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdfs = [Path(p) for p in args.pdf]
    html = Path(args.html)
    for p in pdfs + [html]:
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 2

    out_pdf = _resolve(args.out_pdf, str(pdfs[0].with_suffix(".validated.pdf")))
    out_html = _resolve(args.out_html, str(html.with_suffix(".commented.html")))

    result = compare(
        [str(p) for p in pdfs], str(html), output_pdf=out_pdf, output_html=out_html
    )

    if args.json:
        print(to_json(result))
    else:
        print(to_console(result))
        if result.output_pdf:
            print(f"\nValidated PDF written to: {result.output_pdf}")
            print("  green = validated · red = number changed · orange = number/line "
                  "missing from HTML · amber = wording differs")
        if result.output_html:
            print(f"Commented HTML written to: {result.output_html}")
            print("  ✓ on each matching financial row, ✗/⚠ where it differs from the PDF")

    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
