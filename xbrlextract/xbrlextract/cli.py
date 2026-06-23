"""Command-line interface: ``python -m xbrlextract instance.xml -x out.xlsx``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import extract


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xbrlextract",
        description="Extract a filed XBRL instance (the EDGAR *_htm.xml) into a "
        "reviewable, multi-sheet Excel workbook.",
    )
    p.add_argument("instance", help="path to the XBRL instance (*_htm.xml)")
    p.add_argument(
        "-s",
        "--schema",
        help="path to the extension schema (*.xsd) for labels & structure "
        "(auto-detected next to the instance if not given)",
    )
    p.add_argument(
        "-x",
        "--xlsx",
        help="output Excel path (default: <instance>.facts.xlsx)",
    )
    return p


def _autodetect_schema(instance: Path) -> str | None:
    candidates = list(instance.parent.glob("*.xsd")) + list(
        instance.parent.glob("*.xsd.xml")
    )
    return str(candidates[0]) if candidates else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    inst = Path(args.instance)
    if not inst.is_file():
        print(f"error: file not found: {inst}", file=sys.stderr)
        return 2

    schema = args.schema or _autodetect_schema(inst)
    if args.schema and not Path(args.schema).is_file():
        print(f"error: schema not found: {args.schema}", file=sys.stderr)
        return 2

    out = args.xlsx or str(inst.with_suffix(".facts.xlsx"))

    result = extract(str(inst), schema=schema, output_xlsx=out)

    print(
        f"Extracted {result.fact_count} facts "
        f"({result.concept_count} concepts, {result.context_count} contexts)"
    )
    if schema:
        print(f"  labels & structure from: {schema}")
    else:
        print("  no schema found — concept names used instead of labels")
    print(f"Workbook written to: {result.output_xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
