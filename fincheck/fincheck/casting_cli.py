"""Command-line interface for casting two interim statements.

    python -m fincheck.cast CURRENT.pdf PRIOR.pdf
    python -m fincheck.cast --current q2.pdf --prior q1.pdf -o casting.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .casting import cast
from .casting_report import to_console, to_json, write_excel


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fincheck.cast",
        description="Cast a current-period interim statement against the prior "
        "period: check that every year-to-date (six-month) figure equals the "
        "current quarter plus the prior quarter, for both years shown.",
    )
    p.add_argument("current", nargs="?", help="current-period statement PDF "
                   "(with three- and six-month columns)")
    p.add_argument("prior", nargs="?", help="prior-period statement PDF "
                   "(with the earlier three-month column)")
    p.add_argument("--current", dest="current_opt", help=argparse.SUPPRESS)
    p.add_argument("--prior", dest="prior_opt", help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", help="Excel report path "
                   "(default: <current>.casting.xlsx; 'none' to skip)")
    p.add_argument("--pdf", help="highlighted PDF path "
                   "(default: <current>.casting.pdf; 'none' to skip)")
    p.add_argument("--html", help="HTML report path "
                   "(default: <current>.casting.html; 'none' to skip)")
    p.add_argument("--tolerance", type=float, default=1.0,
                   help="absolute rounding slack before a difference is a "
                   "mismatch (default: 1.0; use 0 for an exact cast)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = args.current_opt or args.current
    prior = args.prior_opt or args.prior
    if not current or not prior:
        print("error: provide both the current and prior statement PDFs.",
              file=sys.stderr)
        return 2
    for label, path in (("current", current), ("prior", prior)):
        if not Path(path).is_file():
            print(f"error: {label} file not found: {path}", file=sys.stderr)
            return 2

    result = cast(current, prior, tolerance=args.tolerance)

    src = Path(current)
    excel = args.output if args.output is not None else str(
        src.with_suffix(".casting.xlsx"))
    pdf = args.pdf if args.pdf is not None else str(src.with_suffix(".casting.pdf"))
    html_path = args.html if args.html is not None else str(
        src.with_suffix(".casting.html"))

    if args.json:
        print(to_json(result))
    else:
        print(to_console(result))

    if excel and excel.lower() != "none":
        write_excel(result, excel)
        if not args.json:
            print(f"\nExcel report written to:    {excel}")

    if html_path and html_path.lower() != "none":
        from .casting_html import write_html
        write_html(result, html_path)
        if not args.json:
            print(f"HTML report written to:     {html_path}")

    if pdf and pdf.lower() != "none":
        # Imported lazily so the report still works without PyMuPDF installed.
        from .casting_highlight import write_highlighted_pdf
        write_highlighted_pdf(result, pdf)
        if not args.json:
            print(f"Highlighted PDF written to: {pdf}")

    return 1 if result.mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
