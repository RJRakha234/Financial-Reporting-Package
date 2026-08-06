"""Backfill historical bars into the store, then run the integrity suite.

    python -m nifty50.scripts.backfill --symbols RELIANCE,INFY --timeframes 15m,1d

Read-only: this pulls market data and writes local parquet. It cannot place,
modify or cancel anything — see PART 8 and ``nifty50.data.brokers.base``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from nifty50.config import Config, load_config
from nifty50.corporate_actions import CorporateActionSet
from nifty50.data.backfill import Backfiller
from nifty50.data.brokers import build_adapter
from nifty50.data.integrity import check_bars
from nifty50.data.store import BarStore
from nifty50.domain import Exchange, Timeframe
from nifty50.logging_setup import configure_logging, get_logger
from nifty50.trading_calendar import TradingCalendar

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated NSE trading symbols")
    parser.add_argument("--timeframes", default="", help="comma-separated; default: all configured")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--no-resume", action="store_true", help="re-fetch the whole window")
    args = parser.parse_args(argv)

    config: Config = load_config()
    configure_logging(config)
    calendar = TradingCalendar.from_config(config)
    store = BarStore.from_config(config)
    adapter = build_adapter(config)
    actions = CorporateActionSet.from_csv(config.path(config.corporate_actions.actions_file))

    status = adapter.authenticate()
    if not status.valid:
        print(f"authentication failed: {status.message}", file=sys.stderr)
        return 2

    timeframes = [Timeframe(value) for value in args.timeframes.split(",") if value] or list(
        config.data.timeframes
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    backfiller = Backfiller(adapter, store, calendar, config)
    exit_code = 0

    for symbol in symbols:
        instrument = adapter.resolve(symbol, Exchange.NSE)
        for timeframe in timeframes:
            result = backfiller.backfill(
                instrument,
                timeframe,
                start=args.start,
                end=args.end,
                resume=not args.no_resume,
            )
            if result.skipped:
                print(f"{symbol} {timeframe.value}: skipped ({result.skip_reason})")
                continue
            print(
                f"{symbol} {timeframe.value}: {result.bars_written} bars "
                f"in {result.requests_made} request(s)"
            )
            for error in result.errors:
                print(f"  ERROR {error}", file=sys.stderr)
                exit_code = 1

            # Integrity is not optional and not deferred: a bad ingest that is
            # not reported now becomes a "surprisingly good" backtest later.
            stored = store.read(Exchange.NSE, symbol, timeframe)
            if stored.empty:
                continue
            report = check_bars(
                stored,
                symbol=symbol,
                timeframe=timeframe,
                calendar=calendar,
                start=stored.index.min().date(),
                end=stored.index.max().date(),
                suspect_abs_log_return=config.data.integrity.suspect_unadjusted_abs_log_return,
                known_action_dates=[a.ex_date for a in actions.for_symbol(symbol)],
                max_missing_pct=config.data.integrity.max_missing_bars_pct_before_quarantine,
            )
            if not report.is_clean:
                print(report.describe())
                if report.errors:
                    exit_code = 1

    return exit_code


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
