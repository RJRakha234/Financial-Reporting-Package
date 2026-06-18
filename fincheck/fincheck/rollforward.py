"""Check that prior-period (comparative) figures were rolled forward correctly.

Every set of financial statements restates earlier periods as *comparatives*:
the results for the quarter ended 30 June 2026 also show the figures for the
quarter ended 30 June 2025 and the year ended 31 March 2026. Those comparatives
must equal the numbers **as originally published** in the earlier filings. When
they don't, something has been mis-keyed, mis-mapped, or silently restated — a
classic financial-reporting control point.

This module reconciles a *current* filing against one or more *previously
published* filings:

1. each numeric column is labelled with its reporting period (``periods.py``);
2. for every period that appears in both the current filing and a prior one,
   the current comparative figure for each line item is matched — by period and
   by (normalised) line-item label — against the published figure;
3. any difference beyond a rounding tolerance is flagged as a rollforward error.

Like the footing checker it runs **entirely offline** and can write a
colour-highlighted copy of the current PDF (green = comparative agrees with what
was published, red = it does not).
"""

import re
from dataclasses import dataclass, field
from datetime import date

from .extract import Cell, extract_pages
from .numbers import format_number
from .periods import Period, detect_periods


def _normalize(label: str) -> str:
    s = (label or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Figure:
    """One reported figure: a line item, in a period, in a filing."""

    source: str
    end_date: date
    period_type: str
    period: Period
    label: str
    norm_label: str
    occurrence: int
    value: float
    page_index: int
    cell: Cell


def _type_compatible(a: str, b: str) -> bool:
    """Same period type, treating an undetected type as a wildcard."""
    return a == b or a == "unknown" or b == "unknown"


@dataclass
class RollforwardCheck:
    """A current comparative figure matched against a published one."""

    status: str  # "ok" | "mismatch"
    current: Figure
    prior: Figure

    @property
    def difference(self) -> float:
        return self.current.value - self.prior.value


@dataclass
class RollforwardResult:
    current_pdf: str
    prior_pdfs: list[str]
    checks: list[RollforwardCheck] = field(default_factory=list)
    current_periods: list[Period] = field(default_factory=list)
    covered_period_keys: set = field(default_factory=set)
    output_pdf: str | None = None

    @property
    def mismatches(self) -> list[RollforwardCheck]:
        return [c for c in self.checks if c.status == "mismatch"]

    @property
    def consistent(self) -> bool:
        return not self.mismatches

    @property
    def figures_checked(self) -> int:
        return len(self.checks)

    @property
    def uncovered_periods(self) -> list[Period]:
        """Comparative periods with no matching prior filing supplied.

        The most recent period (the one the filing actually reports) is excluded:
        there is never a prior publication of it to roll forward from.
        """
        dated = [p for p in self.current_periods if p.end_date is not None]
        if not dated:
            return []
        latest = max(p.end_date for p in dated)
        seen: dict[tuple, Period] = {}
        for p in dated:
            if p.end_date == latest:
                continue
            if p.key not in self.covered_period_keys:
                seen.setdefault(p.key, p)
        return list(seen.values())

    def as_dict(self) -> dict:
        return {
            "current": self.current_pdf,
            "prior": self.prior_pdfs,
            "figures_checked": self.figures_checked,
            "mismatch_count": len(self.mismatches),
            "consistent": self.consistent,
            "uncovered_periods": [p.describe() for p in self.uncovered_periods],
            "mismatches": [
                {
                    "period": c.current.period.describe(),
                    "line_item": c.current.label.strip(),
                    "current": c.current.value,
                    "published": c.prior.value,
                    "difference": c.difference,
                    "published_in": c.prior.source,
                }
                for c in self.mismatches
            ],
        }

    def as_json(self) -> str:
        import json

        return json.dumps(self.as_dict(), indent=2)


def _figures_from_pages(source: str, pages) -> list[Figure]:
    figures: list[Figure] = []
    for page in pages:
        periods = {p.column: p for p in detect_periods(page)}
        if not periods:
            continue
        occ: dict[tuple, int] = {}
        for row in page.rows:
            if row.is_header or not row.cells:
                continue
            norm = _normalize(row.label)
            if not norm:
                continue
            for col, period in periods.items():
                cell = row.cells.get(col)
                if cell is None or period.end_date is None:
                    continue
                key = (period.end_date, norm)
                occ[key] = occ.get(key, 0) + 1
                figures.append(
                    Figure(
                        source=source,
                        end_date=period.end_date,
                        period_type=period.period_type,
                        period=period,
                        label=row.label,
                        norm_label=norm,
                        occurrence=occ[key],
                        value=cell.value,
                        page_index=page.index,
                        cell=cell,
                    )
                )
    return figures


def _tolerance(value: float, base: float) -> float:
    return base + abs(value) * 1e-9


def compare(
    current: list[Figure],
    priors: list[list[Figure]],
    base_tolerance: float,
) -> tuple[list[RollforwardCheck], set]:
    """Match current comparatives to published figures by period and line item."""
    index: dict[tuple, list[Figure]] = {}
    for figs in priors:
        for f in figs:
            index.setdefault((f.end_date, f.norm_label, f.occurrence), []).append(f)

    checks: list[RollforwardCheck] = []
    covered: set = set()
    for c in current:
        candidates = index.get((c.end_date, c.norm_label, c.occurrence), [])
        matches = [p for p in candidates if _type_compatible(c.period_type, p.period_type)]
        if not matches:
            continue
        covered.add(c.period.key)
        for p in matches:
            status = (
                "ok"
                if abs(c.value - p.value) <= _tolerance(c.value, base_tolerance)
                else "mismatch"
            )
            checks.append(RollforwardCheck(status=status, current=c, prior=p))
    return checks, covered


def check_rollforward(
    current_pdf: str,
    prior_pdfs: list[str],
    tolerance: float = 1.0,
    output_pdf: str | None = None,
) -> RollforwardResult:
    """Reconcile a current filing's comparatives against prior published filings.

    Args:
        current_pdf: the filing under review (contains the comparative columns).
        prior_pdfs: one or more previously published filings to roll forward
            from. Their reporting periods are matched to the current filing's
            comparative periods automatically.
        tolerance: absolute rounding slack before a figure is flagged.
        output_pdf: if given, write a highlighted copy of the current filing.
    """
    current_pages = extract_pages(current_pdf)
    current_figs = _figures_from_pages(current_pdf, current_pages)
    prior_figs = [
        _figures_from_pages(p, extract_pages(p)) for p in prior_pdfs
    ]

    checks, covered = compare(current_figs, prior_figs, tolerance)
    current_periods: list[Period] = []
    for page in current_pages:
        current_periods.extend(detect_periods(page))

    written = None
    if output_pdf is not None:
        from .rollforward_highlight import write_rollforward_pdf

        written = write_rollforward_pdf(current_pdf, output_pdf, checks)

    return RollforwardResult(
        current_pdf=current_pdf,
        prior_pdfs=list(prior_pdfs),
        checks=checks,
        current_periods=current_periods,
        covered_period_keys=covered,
        output_pdf=written,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def to_console(result: RollforwardResult) -> str:
    lines: list[str] = []
    mism = result.mismatches
    if not mism:
        lines.append(
            f"✓ All {result.figures_checked} comparative figures match the "
            "previously published numbers."
        )
    else:
        lines.append(
            f"✗ Found {len(mism)} rollforward mismatch"
            f"{'' if len(mism) == 1 else 'es'} "
            f"(of {result.figures_checked} comparative figures checked):"
        )
        lines.append("")
        for n, c in enumerate(mism, 1):
            lines.append(
                f"  {n}. {c.current.period.describe()}  ·  "
                f"{c.current.label.strip()}"
            )
            lines.append(
                f"     in current filing   {format_number(c.current.value):>16}"
            )
            lines.append(
                f"     as published        {format_number(c.prior.value):>16}"
                f"   (off by {format_number(c.difference)})"
            )
            lines.append(f"     published in: {c.prior.source}")
            lines.append("")

    if result.uncovered_periods:
        lines.append(
            "Note: no prior filing supplied covered these comparative periods, "
            "so they could not be checked:"
        )
        for p in result.uncovered_periods:
            lines.append(f"  · {p.describe()}")
    return "\n".join(lines)


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="fincheck-rollforward",
        description="Check that the comparative (prior-period) figures in a "
        "current financial-statement PDF match the numbers as originally "
        "published in earlier filings.",
    )
    parser.add_argument(
        "current", help="the current filing under review (has the comparatives)"
    )
    parser.add_argument(
        "prior",
        nargs="+",
        help="one or more previously published filings to roll forward from",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="path for a highlighted copy of the current PDF "
        "(green = matches published, red = mismatch; 'none' to skip). "
        "Default: <current>.rollforward.pdf",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="absolute rounding slack before a figure is flagged (default: 1.0)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    import sys
    from pathlib import Path

    args = _build_parser().parse_args(argv)

    paths = [Path(args.current), *(Path(p) for p in args.prior)]
    for p in paths:
        if not p.is_file():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 2

    if args.output is None:
        output = str(Path(args.current).with_suffix(".rollforward.pdf"))
    elif args.output.lower() == "none":
        output = None
    else:
        output = args.output

    result = check_rollforward(
        args.current,
        args.prior,
        tolerance=args.tolerance,
        output_pdf=output,
    )

    if args.json:
        print(result.as_json())
    else:
        print(to_console(result))
        if result.output_pdf:
            print(f"\nHighlighted PDF written to: {result.output_pdf}")
            print("  green = comparative matches published · red = mismatch")

    return 1 if result.mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
