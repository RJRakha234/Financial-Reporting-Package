"""Fetch NIFTY 50 index and constituent history from Yahoo Finance.

Run locally from the ``nifty50-signal-engine`` directory::

    pip install -r tools/requirements-fetch.txt
    python tools/fetch_nifty_data.py --start 2015-01-01 --out-dir data/yahoo

or trigger the "Fetch NIFTY Data" workflow from the repository's Actions tab,
which runs this same script and publishes the result as an artifact.

This is a standalone ingestion utility. It does not import the ``nifty50``
package and the engine does not import it, so it can be run before the engine
is installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
INDEX_TICKER = "^NSEI"
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/NIFTY_50"
REFERENCE_DIR = Path(__file__).resolve().parents[1] / "data" / "reference"
BUNDLED_CONSTITUENTS = REFERENCE_DIR / "nifty50_constituents.csv"

# A NIFTY 50 table with far fewer rows than this is not the constituents table.
MIN_PLAUSIBLE_CONSTITUENTS = 40

# Fail the run if more than this fraction of constituents come back empty; a
# handful of misses is normal, half the index missing means we were throttled.
MAX_FAILURE_RATIO = 0.2

logger = logging.getLogger("fetch_nifty_data")


def _normalise_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Return ``raw`` with flat columns and a tz-aware Asia/Kolkata index.

    Two yfinance behaviours are handled here rather than at each call site:
    recent versions return MultiIndex columns even for a single ticker, and
    daily bars arrive with a timezone-naive index. The engine rejects naive
    datetimes by design, so the index is localised to IST.
    """
    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame.columns = [str(column) for column in frame.columns]

    index = pd.DatetimeIndex(frame.index)
    index = index.tz_localize(IST) if index.tz is None else index.tz_convert(IST)
    frame.index = index
    frame.index.name = "timestamp"
    return frame


def constituents_from_wikipedia() -> pd.DataFrame:
    """Scrape the current NIFTY 50 constituents from Wikipedia.

    Picks the first table that both exposes a symbol column and holds a
    plausible number of rows, so a layout change surfaces as an error instead
    of silently yielding the wrong table.
    """
    for table in pd.read_html(WIKIPEDIA_URL):
        columns = [str(column) for column in table.columns]
        symbol_column = next(
            (c for c in columns if "symbol" in c.lower() or "ticker" in c.lower()), None
        )
        if symbol_column is None:
            continue
        name_column = next(
            (c for c in columns if "company" in c.lower() or "name" in c.lower()), None
        )
        symbols = table[symbol_column].astype(str).str.strip().str.upper()
        names = table[name_column].astype(str).str.strip() if name_column else symbols
        frame = pd.DataFrame({"symbol": symbols, "company": names})
        frame = frame[frame["symbol"].str.fullmatch(r"[A-Z0-9&\-]+")]
        if len(frame) >= MIN_PLAUSIBLE_CONSTITUENTS:
            return frame.reset_index(drop=True)
    msg = "no NIFTY 50 constituents table found on Wikipedia; the page layout probably changed"
    raise RuntimeError(msg)


def constituents_from_bundle() -> pd.DataFrame:
    """Read the constituent snapshot committed under data/reference."""
    frame = pd.read_csv(BUNDLED_CONSTITUENTS, comment="#")
    return frame[["symbol", "company"]]


def load_constituents(source: str) -> tuple[pd.DataFrame, str]:
    """Resolve the constituent list, returning the frame and the source used.

    ``auto`` prefers the live Wikipedia table and falls back to the bundled
    snapshot, which may be stale. The source actually used is returned so it
    can be recorded in the output rather than being lost.
    """
    if source in {"auto", "wikipedia"}:
        try:
            frame = constituents_from_wikipedia()
        except Exception as exc:  # any scrape failure is a fallback trigger
            if source == "wikipedia":
                raise
            logger.warning("Wikipedia constituent lookup failed (%s)", exc)
            logger.warning("FALLING BACK to the bundled snapshot, which MAY BE STALE.")
        else:
            frame = frame.copy()
            frame["yahoo_ticker"] = frame["symbol"] + ".NS"
            return frame, "wikipedia"

    frame = constituents_from_bundle()
    frame["yahoo_ticker"] = frame["symbol"] + ".NS"
    return frame, "bundled-snapshot"


def fetch_index(start: str, end: str, interval: str) -> pd.DataFrame:
    """Fetch NIFTY 50 index bars (Yahoo symbol ^NSEI)."""
    raw = yf.download(
        INDEX_TICKER,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _normalise_frame(raw)


def fetch_one(
    ticker: str,
    start: str,
    end: str,
    interval: str,
    retries: int,
    pause: float,
) -> pd.DataFrame:
    """Fetch a single ticker, retrying on both errors and empty responses.

    Yahoo answers a throttled request with an empty frame rather than an error,
    so an empty result is retried too; treating it as "no such data" is what
    silently produces half-empty datasets.
    """
    for attempt in range(1, retries + 1):
        raw: pd.DataFrame | None = None
        try:
            raw = yf.Ticker(ticker).history(
                start=start, end=end, interval=interval, auto_adjust=False
            )
        except Exception as exc:  # network and parse errors are all retryable
            logger.debug("%s attempt %d/%d failed: %s", ticker, attempt, retries, exc)
        if raw is not None and not raw.empty:
            return _normalise_frame(raw)
        if attempt < retries:
            time.sleep(pause * attempt)
    logger.warning("no data for %s after %d attempts", ticker, retries)
    return pd.DataFrame()


def fetch_all(
    tickers: list[str],
    start: str,
    end: str,
    interval: str,
    threads: int,
    retries: int,
    pause: float,
) -> dict[str, pd.DataFrame]:
    """Fetch every ticker with a bounded thread pool."""
    results: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(fetch_one, ticker, start, end, interval, retries, pause): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            frame = future.result()
            results[ticker] = frame
            logger.info("%-16s %5d rows", ticker, len(frame))
    return results


def to_long(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Combine per-ticker frames into one long frame keyed by timestamp+ticker."""
    parts: list[pd.DataFrame] = []
    for ticker, frame in frames.items():
        if frame.empty:
            continue
        part = frame.reset_index()
        part["ticker"] = ticker
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    combined = pd.concat(parts, ignore_index=True, sort=False)
    price_columns = [
        column
        for column in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
        if column in combined.columns
    ]
    return combined[["timestamp", "ticker", *price_columns]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start", default="2015-01-01", help="start date, YYYY-MM-DD")
    parser.add_argument("--end", default="", help="end date, YYYY-MM-DD (default: today, IST)")
    parser.add_argument("--interval", default="1d", help="bar interval, e.g. 1d, 1wk, 1mo")
    parser.add_argument("--out-dir", default="data/yahoo", help="output directory")
    parser.add_argument("--threads", type=int, default=4, help="parallel downloads")
    parser.add_argument("--retries", type=int, default=3, help="attempts per ticker")
    parser.add_argument("--pause", type=float, default=1.5, help="backoff base, seconds")
    parser.add_argument(
        "--constituents",
        default="auto",
        choices=("auto", "wikipedia", "bundled"),
        help="constituent source; 'auto' tries Wikipedia then the bundled snapshot",
    )
    parser.add_argument("--index-only", action="store_true", help="skip constituents")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    args = parse_args(argv)

    now = datetime.now(tz=IST)
    end = args.end or now.strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    (out_dir / "companies").mkdir(parents=True, exist_ok=True)

    logger.info("range %s .. %s at %s", args.start, end, args.interval)

    index_frame = fetch_index(args.start, end, args.interval)
    if index_frame.empty:
        logger.error("index fetch returned no rows; aborting")
        return 1
    index_frame.to_csv(out_dir / "nifty_index.csv")
    logger.info("index: %d rows -> %s", len(index_frame), out_dir / "nifty_index.csv")

    source = "skipped"
    failed: list[str] = []
    ok = 0

    if not args.index_only:
        components, source = load_constituents(args.constituents)
        logger.info("constituents: %d from %s", len(components), source)
        components = components.assign(constituent_source=source)
        components.to_csv(out_dir / "nifty_components.csv", index=False)

        frames = fetch_all(
            components["yahoo_ticker"].tolist(),
            args.start,
            end,
            args.interval,
            args.threads,
            args.retries,
            args.pause,
        )
        for ticker, frame in sorted(frames.items()):
            if frame.empty:
                failed.append(ticker)
                continue
            frame.to_csv(out_dir / "companies" / f"{ticker}.csv")
            ok += 1

        long_frame = to_long(frames)
        if not long_frame.empty:
            long_frame.to_csv(out_dir / "nifty_companies_long.csv", index=False)

    metadata = {
        "generated_at": now.isoformat(),
        "start": args.start,
        "end": end,
        "interval": args.interval,
        "constituent_source": source,
        "index_rows": len(index_frame),
        "companies_ok": ok,
        "companies_failed": failed,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    if failed:
        logger.warning("%d ticker(s) returned no data: %s", len(failed), ", ".join(failed))
    total = ok + len(failed)
    if total and len(failed) / total > MAX_FAILURE_RATIO:
        logger.error("too many empty results (%d/%d); likely rate-limited", len(failed), total)
        return 1
    logger.info("done: %d company files, index %d rows", ok, len(index_frame))
    return 0


if __name__ == "__main__":
    sys.exit(main())
