"""Lay a directory of downloaded vendor files out as a replay root.

    python -m nifty50.scripts.build_replay --source ../data/historical

The Kite downloader writes flat files named ``SYMBOL_INTERVAL.parquet``. The
replay adapter reads a directory tree instead::

    data/replay/instruments.csv
    data/replay/NSE/RELIANCE/15m.parquet
    data/replay/NFO/NIFTY26OCTFUT/15m.parquet

Those are two different conventions for the same bars, and nothing in between
them was written until now -- which is why ``--replay`` had no data to serve.
This script is the bridge. It reads through the vendor loader, so the same
timestamp parsing, column mapping and corporate-action checks that guard the
backtest also guard what the dashboard replays; a file the loader will not read
is reported here rather than silently producing an empty chart.

Files are copied, not moved. The download directory stays the source of truth.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from nifty50.config import load_config
from nifty50.data.vendor_csv import LoadResult, scan_directory
from nifty50.domain import OHLCV_COLUMNS, Exchange, InstrumentKind, Timeframe
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

# NSE index names carried in vendor filenames. Indices have no volume and are
# never tradeable, so mislabelling one as equity would put a phantom position
# size on the dashboard.
_INDEX_HINTS: frozenset[str] = frozenset(
    {"NIFTY 50", "NIFTY50", "NIFTY BANK", "BANKNIFTY", "NIFTY FIN SERVICE", "FINNIFTY"}
)

_DEFAULT_TICK_SIZE: float = 0.05


def classify(symbol: str) -> tuple[Exchange, InstrumentKind]:
    """Exchange and instrument kind from the symbol alone.

    Derivatives live on NFO and cash equities on NSE, and the replay adapter
    keys its directory tree off the exchange -- so getting this wrong means the
    bars are written where nothing will look for them.
    """
    upper = symbol.upper()
    if upper.endswith("FUT"):
        return Exchange.NFO, InstrumentKind.FUTURE
    if upper.endswith(("CE", "PE")) and any(ch.isdigit() for ch in upper):
        return Exchange.NFO, InstrumentKind.OPTION
    if upper in _INDEX_HINTS:
        return Exchange.NSE, InstrumentKind.INDEX
    return Exchange.NSE, InstrumentKind.EQUITY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True,
        help="directory of downloaded vendor files (SYMBOL_INTERVAL.parquet)",
    )
    parser.add_argument(
        "--dest", type=Path, default=None,
        help="replay root to write (default: broker.replay.root from config.yaml)",
    )
    parser.add_argument(
        "--symbols", default=None,
        help="comma-separated symbols to include (default: everything found)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="replace bars already present in the destination",
    )
    args = parser.parse_args(argv)

    config = load_config()
    source = args.source.expanduser().resolve()
    dest = (args.dest or config.path(config.broker.replay.root)).expanduser().resolve()

    if not source.is_dir():
        print(f"source directory not found: {source}", file=sys.stderr)
        return 2

    try:
        scan = scan_directory(source)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    wanted = (
        {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        if args.symbols
        else None
    )

    print(f"source  {source}")
    print(f"dest    {dest}\n")

    # symbol -> exchange, kind; and (symbol, timeframe) -> the file to write
    instruments: dict[str, tuple[Exchange, InstrumentKind]] = {}
    written: dict[str, list[str]] = defaultdict(list)
    skipped = 0

    for result in scan.loaded:
        symbol = result.symbol_hint
        if symbol is None:
            print(f"  ?  {result.path.name}: no symbol could be inferred")
            skipped += 1
            continue
        if wanted is not None and symbol not in wanted:
            continue
        timeframe = result.timeframe
        if timeframe is None:
            print(f"  ?  {result.path.name}: bar size could not be inferred")
            skipped += 1
            continue
        if result.frame.empty:
            print(f"  -  {result.path.name}: no rows")
            skipped += 1
            continue

        exchange, kind = classify(symbol)
        instruments[symbol] = (exchange, kind)
        target = dest / exchange.value / symbol / f"{timeframe.value}.parquet"
        if target.exists() and not args.overwrite:
            print(f"  =  {symbol} {timeframe.value}: exists, skipped (--overwrite to replace)")
            written[symbol].append(timeframe.value)
            continue

        _write(result, target)
        span = result.span
        span_text = f"{span[0]} .. {span[1]}" if span else "empty"
        print(f"  +  {symbol:<18} {timeframe.value:<4} {result.rows:>7,} bars   {span_text}")
        written[symbol].append(timeframe.value)

    for path, reason in scan.failed:
        print(f"  !  {path.name}: {reason}")
        skipped += 1

    if not instruments:
        print("\nnothing written -- no readable OHLCV files found.", file=sys.stderr)
        return 1

    manifest = _write_manifest(dest, instruments)
    print(f"\n{len(instruments)} instrument(s), {sum(len(v) for v in written.values())} series")
    if skipped:
        print(f"{skipped} file(s) skipped -- see the lines marked ? ! - above")
    print(f"manifest -> {manifest}")
    print("\nnow run:  python -m nifty50.scripts.dashboard --replay")
    return 0


def _write(result: LoadResult, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Only the OHLCV contract is carried across. Vendor files arrive with extra
    # columns -- open interest, symbol, an unnamed index -- and the replay
    # adapter reads positionally enough that a stray column is a hazard.
    frame = result.frame.loc[:, [c for c in OHLCV_COLUMNS if c in result.frame.columns]].copy()
    frame.index.name = "ts"
    frame.to_parquet(target)


def _write_manifest(
    dest: Path, instruments: dict[str, tuple[Exchange, InstrumentKind]]
) -> Path:
    """Write instruments.csv, merging with any manifest already there.

    Merging rather than replacing means building the replay root one download
    at a time -- equities today, futures next week -- does not silently drop
    the symbols added last time.
    """
    manifest = dest / "instruments.csv"
    rows: dict[str, dict[str, str]] = {}
    if manifest.exists():
        with manifest.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rows[row["symbol"].strip()] = row

    for token, (symbol, (exchange, kind)) in enumerate(sorted(instruments.items()), start=1):
        existing = rows.get(symbol)
        rows[symbol] = {
            "symbol": symbol,
            "exchange": exchange.value,
            "kind": kind.value,
            # Replay tokens are local identifiers, not Kite's. Keeping any
            # token already on file means a manifest hand-edited to carry real
            # broker tokens is not overwritten on the next run.
            "token": existing["token"] if existing else str(token),
            "lot_size": existing.get("lot_size", "") if existing else "",
            "tick_size": (existing.get("tick_size") if existing else "")
            or str(_DEFAULT_TICK_SIZE),
        }

    dest.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "exchange", "kind", "token", "lot_size", "tick_size"]
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for symbol in sorted(rows):
            writer.writerow({field: rows[symbol].get(field, "") for field in fields})
    return manifest


def timeframes_present(dest: Path, exchange: Exchange, symbol: str) -> list[Timeframe]:
    """Bar sizes stored for one symbol. Used by the dashboard to pick a default."""
    directory = dest / exchange.value / symbol
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.parquet")):
        try:
            found.append(Timeframe(path.stem))
        except ValueError:
            continue
    return found


if __name__ == "__main__":
    raise SystemExit(main())
