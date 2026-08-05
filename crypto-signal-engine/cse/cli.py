"""Command-line entry points for the data layer.

python -m cse.cli backfill      # populate the store for every symbol/timeframe
python -m cse.cli verify        # integrity report over everything stored
python -m cse.cli stats         # row counts and coverage per series
python -m cse.cli check-connectivity   # can we actually reach Binance?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from cse.config import Config, load_config
from cse.data.backfill import last_closed_open_time, summarize_coverage
from cse.data.rest import BinanceRestClient, EgressBlockedError
from cse.data.schema import floor_to_interval, interval_to_ms
from cse.data.source import build_source
from cse.data.store import CandleStore
from cse.logging import configure_logging, get_logger

_log = get_logger(__name__)


def _store(config: Config) -> CandleStore:
    return CandleStore(config.data.storage, ohlc_tolerance=config.data.integrity.ohlc_tolerance)


async def cmd_backfill(config: Config) -> int:
    store = _store(config)
    source = build_source(config, store)
    _log.info(
        "backfill.start",
        mode=config.data.mode,
        provenance=source.provenance.as_dict(),
        symbols=config.symbols,
        timeframes=config.timeframes,
        history_days=config.history_days,
    )
    failures = 0
    try:
        for symbol in config.symbols:
            for timeframe in config.timeframes:
                try:
                    report = await source.ensure_history(symbol, timeframe)
                    _log.info(
                        "backfill.series_done",
                        symbol=symbol,
                        timeframe=timeframe,
                        **report.as_dict(),
                    )
                except Exception as exc:
                    failures += 1
                    _log.error(
                        "backfill.series_failed",
                        symbol=symbol,
                        timeframe=timeframe,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
    finally:
        await source.aclose()
    return 1 if failures else 0


async def cmd_verify(config: Config) -> int:
    store = _store(config)
    bad = 0
    for symbol in config.symbols:
        for timeframe in config.timeframes:
            interval_ms = interval_to_ms(timeframe)
            report = store.verify(symbol, timeframe, interval_ms)
            status = "ok" if report.ok else "REPAIRS NEEDED"
            print(
                f"{symbol:<10} {timeframe:<4} rows={report.rows_out:<8} "
                f"gaps={len(report.gaps):<4} missing={report.missing_bars:<6} {status}"
            )
            if not report.ok:
                bad += 1
    return 1 if bad else 0


async def cmd_stats(config: Config) -> int:
    store = _store(config)
    now_ms = _now_ms()
    rows: list[dict[str, Any]] = []
    for symbol in config.symbols:
        for timeframe in config.timeframes:
            interval_ms = interval_to_ms(timeframe)
            frame = store.read(symbol, timeframe)
            expected_start = floor_to_interval(
                now_ms - config.history_days * 86_400_000, interval_ms
            )
            expected_end = last_closed_open_time(now_ms, interval_ms)
            coverage = summarize_coverage(
                frame, interval_ms, expected_start=expected_start, expected_end=expected_end
            )
            rows.append({"symbol": symbol, "timeframe": timeframe, **coverage})
    print(json.dumps(rows, indent=2, default=str))
    return 0


async def cmd_check_connectivity(config: Config) -> int:
    """Probe Binance reachability and report precisely why it failed.

    Worth its own command: 'the backfill produced nothing' has very different
    remedies depending on whether it is a geo-block, an egress policy, or a
    transient outage.
    """
    async with BinanceRestClient(config.data.rest, config.data.rate_limit) as client:
        try:
            await client.ping()
        except EgressBlockedError as exc:
            print(f"BLOCKED BY NETWORK POLICY  {client.base_url}", file=sys.stderr)
            print(f"    {exc}", file=sys.stderr)
            print(
                "\nThis is a policy decision, not an outage. Live mode cannot work here.\n"
                "Set data.mode to 'synthetic' (offline development) or 'replay' "
                "(previously captured data).",
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            print(f"UNREACHABLE  {client.base_url}  {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        server_ms = await client.server_time_ms()
        advertised = await client.verify_rate_limits()
        print(f"OK  {client.base_url}")
        print(f"    server_time_ms       = {server_ms}")
        print(f"    clock_skew_ms        = {server_ms - _now_ms()}")
        print(f"    advertised_weight_1m = {advertised}")
        print(f"    configured_weight_1m = {config.data.rate_limit.weight_limit_per_minute}")
    return 0


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


COMMANDS = {
    "backfill": cmd_backfill,
    "verify": cmd_verify,
    "stats": cmd_stats,
    "check-connectivity": cmd_check_connectivity,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cse", description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    configure_logging(config.logging)
    return asyncio.run(COMMANDS[args.command](config))


if __name__ == "__main__":
    raise SystemExit(main())
