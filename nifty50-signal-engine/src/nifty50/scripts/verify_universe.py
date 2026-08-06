"""Validate the point-in-time constituents file and report what it needs.

Run after every edit to ``nifty50_constituents.csv``, and after each semi-annual
NSE reconstitution.

    python -m nifty50.scripts.verify_universe --start 2019-01-01 --end 2026-12-31

Exit codes: 0 clean and backtest-ready, 1 problems found, 2 nothing loaded.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from nifty50.config import Config, load_config
from nifty50.data.store import BarStore, safe_symbol
from nifty50.domain import Exchange
from nifty50.trading_calendar import TradingCalendar
from nifty50.universe import PointInTimeUniverse

# How many short/over dates to print before truncating; the same reconstitution
# error repeats on every session until the next change, so a handful is enough.
_MAX_REPORTED_PROBLEMS = 15


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument(
        "--check-store",
        action="store_true",
        help="also report members with no price data in the bar store",
    )
    args = parser.parse_args(argv)

    config: Config = load_config()
    calendar = TradingCalendar.from_config(config)
    universe = PointInTimeUniverse.from_config(config)

    path = config.path(config.universe.constituents_file)
    print(f"{config.universe.index} membership from {path}")

    if universe.is_empty:
        print(
            "\nNo membership rows loaded — the file ships empty on purpose.\n"
            "Backtests are blocked until it is populated from NSE index "
            "reconstitution press releases (https://www.niftyindices.com).\n"
            "Shipping today's constituent list instead would be survivorship "
            "bias, which is why there is no placeholder data to delete.",
            file=sys.stderr,
        )
        return 2

    print(
        f"  {len(universe)} membership spell(s), "
        f"{len(universe.symbols_ever())} distinct symbol(s) ever"
    )
    coverage = universe.coverage
    if coverage is not None:
        earliest, latest = coverage
        print(f"  covers {earliest} .. {latest or 'present'}")

    changes = universe.changes_between(args.start, args.end)
    print(f"  {len(changes)} index change(s) in the requested window")
    for change in changes:
        print(f"    {change}")

    problems = universe.validate(calendar, args.start, args.end)
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):", file=sys.stderr)
        for problem in problems[:_MAX_REPORTED_PROBLEMS]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > _MAX_REPORTED_PROBLEMS:
            print(f"  ... and {len(problems) - _MAX_REPORTED_PROBLEMS} more", file=sys.stderr)

    missing_data = _members_without_bars(config, universe) if args.check_store else []
    if missing_data:
        print(
            f"\n{len(missing_data)} member(s) have no bars in the store. "
            "Point-in-time membership is useless without the price history of "
            "names that have since left the index:",
            file=sys.stderr,
        )
        for symbol in missing_data[:_MAX_REPORTED_PROBLEMS]:
            print(f"  {symbol}", file=sys.stderr)

    if problems or missing_data:
        return 1
    print("\nUniverse is complete, verified and backtest-ready over this window.")
    return 0


def _members_without_bars(config: Config, universe: PointInTimeUniverse) -> list[str]:
    store = BarStore.from_config(config)
    stored = set(store.symbols(Exchange.NSE))
    # The store sanitises symbols for path safety (M&M -> M_M), so compare on
    # the same form rather than on the raw trading symbol.
    return sorted(s for s in universe.symbols_ever() if safe_symbol(s) not in stored)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
