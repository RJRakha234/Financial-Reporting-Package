"""Command-line interface: ``python -m secverify statement.pdf filing.html``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import verify
from .report import to_console, to_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secverify",
        description="Validate an SEC-filing HTML against the published PDF "
        "of the same document and write a green/red highlighted HTML review "
        "copy with remarks on every inconsistency. Runs fully offline.",
    )
    parser.add_argument(
        "pdf",
        nargs="+",
        help="reference PDF(s) — the published statement, and optionally "
        "further sources such as the signed auditor's report; the HTML is "
        "validated against their combined content",
    )
    parser.add_argument("html", help="HTML rendering to validate (SEC exhibit)")
    parser.add_argument(
        "-o",
        "--output",
        help="path for the highlighted HTML "
        "(default: <html>.checked.html; use 'none' to skip)",
    )
    parser.add_argument("--json", metavar="PATH", help="also write a JSON report")
    return parser


def main(argv: list[str] | None = None, level: str = "base") -> int:
    parser = build_parser()
    parser.add_argument(
        "--level",
        choices=["base", "alpha", "beta", "sigma"],
        default=level,
        help="capability tier (base < alpha < beta < sigma)",
    )
    args = parser.parse_args(argv)
    for path, kind in [(p, "PDF") for p in args.pdf] + [(args.html, "HTML")]:
        if not Path(path).is_file():
            print(f"error: {kind} file not found: {path}", file=sys.stderr)
            return 2

    result = verify(args.pdf, args.html, output_html=args.output, level=args.level)

    print(to_console(result))
    if getattr(result, "output_html", None):
        print(f"\nHighlighted HTML written to: {result.output_html}")
        print(
            "  green = validated against PDF · amber = review · "
            "red = inconsistent (hover for remark, [n] links to summary panel)"
        )
    if args.json:
        Path(args.json).write_text(to_json(result), encoding="utf-8")
        print(f"JSON report written to: {args.json}")

    return 1 if result.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
