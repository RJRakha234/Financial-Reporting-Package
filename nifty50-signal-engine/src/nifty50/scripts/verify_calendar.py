"""Cross-check the seeded trading calendar against actually-ingested bars.

``nse_holidays.csv`` ships as recalled seed data, not as a scrape of an NSE
circular, and a wrong holiday row is silently destructive in both directions:

* a **phantom holiday** deletes a real session from every backtest and from
  every indicator window that spans it;
* a **missing holiday** manufactures a day of expected bars that never existed,
  which the integrity checker then reports as a data gap forever.

Neither shows up as an error anywhere else, so this script exists to correct the
file from evidence. Run it after the first full backfill and after every
calendar-year rollover.

    python -m nifty50.scripts.verify_calendar --start 2019-01-01 --end 2026-12-31
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Mapping

import pandas as pd

from nifty50.config import Config, load_config
from nifty50.data.integrity import suggest_missing_holidays
from nifty50.data.store import BarStore
from nifty50.domain import Exchange, Timeframe
from nifty50.frames import ist_index
from nifty50.trading_calendar import TradingCalendar

# Below this many symbols, "no bars that day" is not evidence of a holiday.
_MIN_SYMBOLS_FOR_INFERENCE = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--timeframe", default=Timeframe.D1.value)
    args = parser.parse_args(argv)

    config: Config = load_config()
    calendar = TradingCalendar.from_config(config)
    store = BarStore.from_config(config)
    timeframe = Timeframe(args.timeframe)

    symbols = store.symbols(Exchange.NSE)
    if len(symbols) < _MIN_SYMBOLS_FOR_INFERENCE:
        print(
            f"Only {len(symbols)} symbol(s) in the store. At least "
            f"{_MIN_SYMBOLS_FOR_INFERENCE} are needed before an absence of bars "
            "is evidence of anything. Run the backfill first.",
            file=sys.stderr,
        )
        return 2

    frames = {
        symbol: store.read(Exchange.NSE, symbol, timeframe, start=_at(calendar, args.start))
        for symbol in symbols
    }
    frames = {symbol: frame for symbol, frame in frames.items() if not frame.empty}

    missing = suggest_missing_holidays(frames, calendar, args.start, args.end)
    phantom = _phantom_holidays(frames, calendar, args.start, args.end)

    print(f"Checked {len(frames)} symbol(s) of {timeframe.value} bars, {args.start} .. {args.end}")

    if missing:
        print(f"\nMISSING HOLIDAYS — {len(missing)} day(s) with no bars from any symbol.")
        print("Add these to data/reference/nse_holidays.csv after confirming the reason:")
        for day in missing:
            print(f"  {day.isoformat()},<name>,medium,verified_from_data  # {day:%A}")

    if phantom:
        print(f"\nPHANTOM HOLIDAYS — {len(phantom)} day(s) marked as holidays that have bars.")
        print("Remove these rows; each one is silently deleting a real session:")
        for day, count in phantom:
            record = calendar.holiday(day)
            name = record.name if record else "?"
            print(f"  {day.isoformat()} ({name}) — {count} symbol(s) traded")

    if not missing and not phantom:
        print("\nCalendar agrees with the data over this window.")
        return 0
    return 1


def _at(calendar: TradingCalendar, day: dt.date) -> dt.datetime:
    """Session open on ``day``, or on the next trading day if it is closed."""
    target = day if calendar.is_trading_day(day) else calendar.next_trading_day(day)
    return calendar.schedule(target).continuous.start


def _phantom_holidays(
    frames: Mapping[str, pd.DataFrame],
    calendar: TradingCalendar,
    start: dt.date,
    end: dt.date,
) -> list[tuple[dt.date, int]]:
    traded: dict[dt.date, int] = {}
    for frame in frames.values():
        for ts in ist_index(frame):
            traded[ts.date()] = traded.get(ts.date(), 0) + 1
    hits: list[tuple[dt.date, int]] = []
    for record in calendar.holidays_in(start, end):
        count = traded.get(record.date, 0)
        if count:
            hits.append((record.date, count))
    return hits


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
