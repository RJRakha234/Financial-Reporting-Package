"""Assess whether a folder of historical files can support a backtest.

    python -m nifty50.scripts.analyse_historical --path data/historical
    python -m nifty50.scripts.analyse_historical --path data/historical --symbol RELIANCE

Answers one question per file: *can this be backtested on, and if not, what
exactly is wrong with it.* It reads only — nothing is written to the bar store,
because ingesting a file whose adjustment status is unknown is how a store ends
up with a mix of adjusted and unadjusted history that nothing downstream can
detect.

The verdict is deliberately harsh. A file is USABLE only if it parses cleanly,
aligns to the NSE trading calendar, has no OHLC violations, and shows no sign
of unadjusted corporate actions. Anything else is NEEDS WORK or UNUSABLE, with
the specific reason. A backtest run on a NEEDS WORK file will produce a number;
that number will just be wrong.

Exit codes: 0 at least one file is usable, 1 files were found but none are
usable as-is, 2 nothing readable was found.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from nifty50.config import Config, load_config
from nifty50.data.integrity import check_bars
from nifty50.data.vendor_csv import (
    DirectoryScan,
    LoadResult,
    scan_directory,
    suspected_split_dates,
)
from nifty50.domain import Timeframe
from nifty50.trading_calendar import TradingCalendar

_RULE = "=" * 78
_THIN = "-" * 78

# Below this many bars a backtest is a description of a handful of trades.
_MIN_BARS_FOR_A_BACKTEST = 2000


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether one file can be backtested on, and what stands in the way."""

    result: LoadResult
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def label(self) -> str:
        if self.blockers:
            return "UNUSABLE" if len(self.blockers) > 1 else "NEEDS WORK"
        return "NEEDS WORK" if self.warnings else "USABLE"

    @property
    def is_usable(self) -> bool:
        return not self.blockers and not self.warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True, help="directory of historical files")
    parser.add_argument("--symbol", default=None, help="focus the summary on one instrument")
    parser.add_argument(
        "--max-files", type=int, default=40, help="cap per-file detail in the output"
    )
    args = parser.parse_args(argv)

    config = load_config()
    calendar = TradingCalendar.from_config(config)

    root = args.path if args.path.is_absolute() else Path.cwd() / args.path
    if not root.exists():
        print(f"{root} does not exist", file=sys.stderr)
        return 2

    scan = scan_directory(root)
    print(_RULE)
    print(f"HISTORICAL DATA ANALYSIS  —  {root}")
    print(_RULE)
    print(
        f"{len(scan.loaded)} readable, {len(scan.failed)} unreadable, "
        f"{len(scan.skipped)} non-tabular files skipped"
    )
    if not scan.loaded and not scan.failed:
        print("\nNothing to analyse. No .csv or .txt files found under that path.")
        return 2

    _print_failures(scan)

    verdicts = [_assess(result, calendar, config) for result in scan.loaded]
    _print_inventory(verdicts, limit=args.max_files)
    _print_details(verdicts, limit=args.max_files)

    if args.symbol:
        _print_symbol_summary(args.symbol, verdicts, calendar)

    usable = [verdict for verdict in verdicts if verdict.is_usable]
    print()
    print(_RULE)
    print(
        f"SUMMARY: {len(usable)} usable, "
        f"{sum(1 for v in verdicts if v.label == 'NEEDS WORK')} need work, "
        f"{sum(1 for v in verdicts if v.label == 'UNUSABLE')} unusable"
    )
    print(_RULE)
    print()
    print("Nothing was written to the bar store. Ingest deliberately, per file,")
    print("once its adjustment status is known — a store holding a mix of adjusted")
    print("and unadjusted history cannot be told apart from a clean one afterwards.")
    print()
    print("Not investment advice.")
    return 0 if usable else 1


def _assess(result: LoadResult, calendar: TradingCalendar, config: Config) -> Verdict:
    blockers: list[str] = []
    warnings: list[str] = []

    # Concerns the loader raised are graded here, where the severity is known.
    for concern in result.concerns:
        if any(
            marker in concern
            for marker in ("outside their own high-low", "UNADJUSTED", "negative price")
        ):
            blockers.append(concern)
        else:
            warnings.append(concern)
    for inference in result.inferences:
        if "AMBIGUOUS DATES" in inference:
            blockers.append(inference)

    if result.timeframe is None:
        blockers.append("timeframe could not be inferred; bars may mix resolutions")
        return Verdict(result, tuple(blockers), tuple(warnings))

    if result.rows < _MIN_BARS_FOR_A_BACKTEST:
        warnings.append(
            f"only {result.rows} bars — too few for a result that means anything. "
            "Metrics will be dominated by a handful of trades."
        )

    span = result.span
    if span is None:
        blockers.append("no dated rows")
        return Verdict(result, tuple(blockers), tuple(warnings))

    report = check_bars(
        result.frame,
        symbol=result.symbol_hint or result.path.stem,
        timeframe=result.timeframe,
        calendar=calendar,
        start=span[0],
        end=span[1],
        suspect_abs_log_return=config.data.integrity.suspect_unadjusted_abs_log_return,
        max_missing_pct=config.data.integrity.max_missing_bars_pct_before_quarantine,
    )
    for finding in report.errors:
        blockers.append(f"{finding.check}: {finding.message}")
    for finding in report.warnings:
        warnings.append(f"{finding.check}: {finding.message}")

    return Verdict(result, tuple(blockers), tuple(warnings))


def _print_failures(scan: DirectoryScan) -> None:
    if not scan.failed:
        return
    print()
    print("COULD NOT READ")
    print(_THIN)
    for path, reason in scan.failed[:20]:
        print(f"  {path.name}")
        print(f"      {reason}")
    if len(scan.failed) > 20:
        print(f"  ... and {len(scan.failed) - 20} more")


def _print_inventory(verdicts: list[Verdict], *, limit: int) -> None:
    print()
    print("INVENTORY")
    print(_THIN)
    print(f"{'file':<34}{'symbol':<12}{'tf':>5}{'rows':>9}  {'span':<25}{'verdict'}")
    for verdict in verdicts[:limit]:
        result = verdict.result
        span = result.span
        span_text = f"{span[0]} .. {span[1]}" if span else "-"
        timeframe = result.timeframe.value if result.timeframe else "?"
        print(
            f"{result.path.name[:33]:<34}"
            f"{(result.symbol_hint or '?')[:11]:<12}"
            f"{timeframe:>5}{result.rows:>9}  {span_text:<25}{verdict.label}"
        )
    if len(verdicts) > limit:
        print(f"  ... and {len(verdicts) - limit} more")


def _print_details(verdicts: list[Verdict], *, limit: int) -> None:
    interesting = [verdict for verdict in verdicts if verdict.blockers or verdict.warnings]
    if not interesting:
        print()
        print("No blockers or warnings on any file.")
        return

    print()
    print("PER-FILE FINDINGS")
    print(_THIN)
    for verdict in interesting[:limit]:
        print()
        print(f"{verdict.result.path.name}  [{verdict.label}]")
        for inference in verdict.result.inferences:
            print(f"    inferred : {inference}")
        for blocker in verdict.blockers:
            print(f"    BLOCKER  : {blocker}")
        for warning in verdict.warnings:
            print(f"    warning  : {warning}")


def _print_symbol_summary(
    symbol: str, verdicts: list[Verdict], calendar: TradingCalendar
) -> None:
    wanted = symbol.upper()
    matching = [v for v in verdicts if (v.result.symbol_hint or "") == wanted]
    print()
    print(_RULE)
    print(f"{symbol.upper()} — BACKTEST READINESS")
    print(_RULE)
    if not matching:
        available = sorted({v.result.symbol_hint or "?" for v in verdicts})
        print(f"No file resolved to {symbol.upper()}.")
        print(f"Symbols found: {', '.join(available[:30])}")
        return

    by_timeframe: dict[Timeframe, list[Verdict]] = {}
    for verdict in matching:
        if verdict.result.timeframe:
            by_timeframe.setdefault(verdict.result.timeframe, []).append(verdict)

    for timeframe, group in sorted(by_timeframe.items(), key=lambda item: item[0].value):
        stacked = pd.concat([verdict.result.frame for verdict in group]).sort_index()
        duplicated = int(stacked.index.duplicated().sum())
        spans = [verdict.result.span for verdict in group]
        span = (
            min(item[0] for item in spans if item),
            max(item[1] for item in spans if item),
        )
        sessions = calendar.sessions_between(span[0], span[1])
        years = (span[1] - span[0]).days / 365.25
        unique_bars = int(stacked.index.nunique())

        print()
        print(f"  {timeframe.value}: {unique_bars} distinct bars across {len(group)} file(s)")
        print(f"    span      : {span[0]} .. {span[1]}  ({years:.1f} years, ~{sessions} sessions)")
        if duplicated:
            print(
                f"    OVERLAP   : {duplicated} duplicated timestamps across files. Decide "
                "which file\n                wins before ingesting — if two files disagree "
                "on price for the same\n                bar, one is adjusted and the other "
                "is not."
            )
        usable = sum(1 for verdict in group if verdict.is_usable)
        print(f"    clean     : {usable} of {len(group)} file(s)")

        # Corporate actions are detected per file, never on the merge. Files at
        # different price levels — one adjusted, one not — interleave into a
        # sawtooth where every tooth reads as a split. Concatenating first and
        # asking questions afterwards is precisely the error this tool exists
        # to catch, so it must not commit it.
        for verdict in group:
            splits = suspected_split_dates(verdict.result.frame)
            if not splits:
                continue
            print(
                f"    SUSPECTED CORPORATE ACTIONS in {verdict.result.path.name} "
                f"({len(splits)} session(s)):"
            )
            for date in splits[:8]:
                print(f"        {date}")
            if len(splits) > 8:
                print(f"        ... and {len(splits) - 8} more")
            print(
                "      Each needs a row in data/reference/corporate_actions.csv, or the "
                "backtest\n      reads the gap as a real return."
            )

    print()
    _print_symbol_next_steps(matching)


def _print_symbol_next_steps(matching: list[Verdict]) -> None:
    blocked = [verdict for verdict in matching if verdict.blockers]
    print("  NEXT STEPS")
    if blocked:
        print(f"    1. Resolve blockers on {len(blocked)} file(s) — listed above.")
    print(
        "    2. Confirm the adjustment status with the vendor. This cannot be "
        "determined\n       from the file alone unless it carries both close and "
        "adjusted close."
    )
    print(
        "    3. Populate data/reference/nifty50_constituents.csv. Single-symbol "
        "backtests\n       do not need it; anything index-wide will fail closed "
        "without it."
    )
    print(
        "    4. Ingest via BarStore.write, then run the engine's own integrity "
        "check\n       before backtesting."
    )


if __name__ == "__main__":
    raise SystemExit(main())
