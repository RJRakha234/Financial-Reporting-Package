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


def main(argv: list[str] | None = None, level: str = "base",
         strict_default: bool = False) -> int:
    parser = build_parser()
    parser.add_argument(
        "--level",
        choices=["base", "alpha", "beta", "sigma"],
        default=level,
        help="capability tier (base < alpha < beta < sigma)",
    )
    parser.add_argument(
        "--review-zones",
        action="store_true",
        help="paint a blue manual-review overlay over prose figures and "
        "cross-references — the Class 2/3 spots the engine cannot verdict — so "
        "they can be checked by eye (leaves no chance on token-preserving errors)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=strict_default,
        help="leave no number un-reviewed: blue-mark EVERY prose number and "
        "every unconfirmed in-table figure (for a reviewer who cannot risk "
        "missing any figure, however small). Implies --review-zones.",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="turn strict review off (toolsigma runs strict by default)",
    )
    parser.add_argument(
        "--footed",
        action="store_true",
        help="the HTML's tables have already been footed and tie: a single "
        "unconfirmed row in a total-bearing table is covered by the footing "
        "and is not blue-marked (2+ unconfirmed rows in one table stay "
        "marked — a sum-preserving permutation survives footing)",
    )
    args = parser.parse_args(argv)
    for path, kind in [(p, "PDF") for p in args.pdf] + [(args.html, "HTML")]:
        if not Path(path).is_file():
            print(f"error: {kind} file not found: {path}", file=sys.stderr)
            return 2

    result = verify(
        args.pdf, args.html, output_html=args.output, level=args.level,
        review_zones=args.review_zones, strict=args.strict, footed=args.footed,
    )

    if getattr(result, "pdf_reordered", False):
        print(
            "note: reference PDFs reordered to match the HTML's document "
            "order: " + " → ".join(result.pdf_order)
        )
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
