"""Command-line interface: ``python -m fincheck.notes DOC [DOC ...]``.

Each ``DOC`` is a PDF path, optionally suffixed with ``:label`` and/or ``@kind``
to set a display name and override the inferred document kind::

    python -m fincheck.notes \\
        standalone.pdf:SA@indas-standalone-inr \\
        consol.pdf:Consol@indas-consol-inr \\
        ifrs_inr.pdf@ifrs-consol-inr \\
        ifrs_usd.pdf@ifrs-consol-usd \\
        -o notes_diff.html --ledger decisions.json
"""

from __future__ import annotations

import argparse
import os
import sys

from .compare import compare_documents, load_document
from .ledger import load_ledger, summarize
from .normalize import parse_kind
from .report import write_html_report


def _parse_doc_arg(spec: str):
    """Split ``path[:label][@kind]`` into ``(path, label, kind)``."""
    rest, kind = (spec.rsplit("@", 1) + [None])[:2] if "@" in spec else (spec, None)
    if ":" in rest and not os.path.exists(rest):
        path, label = rest.rsplit(":", 1)
    else:
        path, label = rest, None
    parsed_kind = parse_kind(kind) if kind else None
    return path, label, parsed_kind


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fincheck.notes",
        description="Compare the common notes across financial-statement PDFs "
        "(Consol/Standalone Ind AS, IFRS INR/USD) and produce an interactive "
        "HTML report of the differences to accept or ignore.",
    )
    parser.add_argument(
        "docs",
        nargs="+",
        metavar="DOC",
        help="PDF path, optionally 'path:label@framework-entity-currency'",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="notes_diff.html",
        help="path for the HTML report (default: notes_diff.html)",
    )
    parser.add_argument(
        "--ledger",
        help="JSON decisions file to seed accept/ignore state and suppress "
        "previously-resolved differences",
    )
    return parser


def _print_matrix(result) -> None:
    docs = result.documents
    width = max((len(t) for t in (next(iter(p.values())) for p in result.matrix.values())), default=10)
    width = min(max(width, 10), 44)
    header = "  " + "Note".ljust(width) + "  " + " ".join(d.name[:10].center(10) for d in docs)
    print(header)
    for present in (p for p in result.matrix.values()):
        title = next(iter(present.values()))
        cells = " ".join(("✓" if d.name in present else "·").center(10) for d in docs)
        print("  " + title[:width].ljust(width) + "  " + cells)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if len(args.docs) < 2:
        print("error: provide at least two documents to compare", file=sys.stderr)
        return 2

    documents = []
    for spec in args.docs:
        path, label, kind = _parse_doc_arg(spec)
        if not os.path.isfile(path):
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
        documents.append(load_document(path, name=label, kind=kind))

    result = compare_documents(documents)
    ledger = load_ledger(args.ledger)

    print(f"Compared {len(documents)} documents:")
    for d in documents:
        print(f"  · {d.name} — {d.kind.label} ({len(d.notes)} notes)")
    print(f"\n{len(result.common_topics)} of {len(result.matrix)} notes common to all.\n")
    _print_matrix(result)

    counts = summarize(result.all_differences, ledger)
    print(
        f"\nDifferences: {len(result.all_differences)} total "
        f"({counts['open']} open, {counts['accepted']} accepted, "
        f"{counts['ignored']} ignored)."
    )
    for p in result.pairs:
        print(
            f"  {p.left_doc} vs {p.right_doc}: {len(p.differences)} "
            f"(avg similarity {round(p.similarity * 100)}%)"
        )

    out = write_html_report(result, args.output, ledger)
    print(f"\nReport written to: {out}")
    print("Open it in a browser to accept/ignore each difference, then "
          "'Export decisions' to save the ledger for next quarter.")

    # Exit non-zero when unresolved differences remain (handy for a checklist/CI).
    return 1 if counts["open"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
