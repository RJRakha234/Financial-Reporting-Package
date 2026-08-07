"""
Fetch NIFTY index history and historical data for all NIFTY 50 constituents.

Usage examples:
    from datetime import date
    from nifty_company_fetcher import (
        get_nifty_components_wikipedia,
        fetch_nifty_index_yfinance,
        fetch_companies_history_yfinance,
        save_company_histories,
    )

    # Get constituents
    comps = get_nifty_components_wikipedia()
    tickers = comps['yahoo_ticker'].tolist()

    # Fetch index
    idx_df = fetch_nifty_index_yfinance("2015-01-01", "2026-08-01")

    # Fetch all company histories (returns dict: {ticker: DataFrame})
    histories = fetch_companies_history_yfinance(tickers, "2015-01-01", "2026-08-01", threads=8)

    # Save everything
    save_company_histories(histories, out_dir="nifty_companies_csv")
"""

from typing import List, Dict, Tuple
from datetime import datetime
import os
import time
import logging
import pandas as pd
import yfinance as yf
import concurrent.futures

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_nifty_components_wikipedia() -> pd.DataFrame:
    """
    Scrape the NIFTY 50 constituents table from Wikipedia and return a DataFrame
    with at least columns: ['Company', 'Symbol', 'yahoo_ticker'].

    Note: The Wikipedia table layout can change; this function tries to find a table
    with a 'Symbol' or 'Ticker' column.
    """
    url = "https://en.wikipedia.org/wiki/NIFTY_50"
    tables = pd.read_html(url)
    # Find table with 'Symbol' or 'Ticker' column
    for t in tables:
        cols = [c.lower() for c in t.columns.astype(str)]
        if any("symbol" in c or "ticker" in c for c in cols):
            df = t.copy()
            # Try to standardize column names
            # Find column name that contains 'symbol' or 'ticker'
            sym_col = next((c for c in df.columns if "symbol" in c.lower() or "ticker" in c.lower()), None)
            name_col = next((c for c in df.columns if "company" in c.lower() or "name" in c.lower()), None)
            if sym_col is None:
                raise RuntimeError("Could not find a symbol/ticker column on the Wikipedia tables.")
            # Normalize symbol values
            df = df.rename(columns={sym_col: "Symbol"})
            if name_col:
                df = df.rename(columns={name_col: "Company"})
            else:
                # If no company column found, just keep Symbol
                df["Company"] = df["Symbol"]
            df["Symbol"] = df["Symbol"].astype(str).str.strip()
            # Build Yahoo tickers by appending '.NS' (if not already)
            def to_yahoo(s: str) -> str:
                s = s.strip()
                if s.endswith(".NS") or s.endswith(".BO"):
                    return s
                return f"{s}.NS"
            df["yahoo_ticker"] = df["Symbol"].apply(to_yahoo)
            return df[["Company", "Symbol", "yahoo_ticker"]]
    # If no suitable table found:
    raise RuntimeError("Failed to find NIFTY constituents table on Wikipedia.")


def fetch_nifty_index_yfinance(start: str, end: str, interval: str = "1d") -> pd.DataFrame:
    """
    Fetch NIFTY 50 index historical data from Yahoo Finance using ticker '^NSEI'.
    start/end are strings 'YYYY-MM-DD'. Returns DataFrame with Date index.
    """
    ticker = "^NSEI"
    df = yf.download(ticker, start=start, end=end, interval=interval, progress=False)
    df.index = pd.to_datetime(df.index)
    return df


def _fetch_single_history(ticker: str, start: str, end: str, interval: str = "1d", retries: int = 3, pause: float = 1.0) -> pd.DataFrame:
    """
    Fetch single ticker history with simple retry/backoff.
    Uses yf.Ticker.history which may be more stable per-symbol.
    """
    attempt = 0
    while attempt < retries:
        try:
            t = yf.Ticker(ticker)
            df = t.history(start=start, end=end, interval=interval)
            if df is None or df.empty:
                # Sometimes yfinance returns empty for a ticker; return empty DataFrame
                return pd.DataFrame()
            df.index = pd.to_datetime(df.index)
            # Keep standard OHLCV (and Adj Close if present)
            return df
        except Exception as e:
            attempt += 1
            sleep = pause * attempt
            logger.debug("Error fetching %s attempt %d/%d: %s — sleeping %.1fs", ticker, attempt, retries, e, sleep)
            time.sleep(sleep)
    logger.warning("Failed to fetch %s after %d attempts.", ticker, retries)
    return pd.DataFrame()


def fetch_companies_history_yfinance(tickers: List[str], start: str, end: str,
                                     interval: str = "1d", threads: int = 6,
                                     retries: int = 3) -> Dict[str, pd.DataFrame]:
    """
    Fetch historical data for a list of Yahoo tickers in parallel.
    Returns a dict mapping ticker -> DataFrame.
    """
    results: Dict[str, pd.DataFrame] = {}

    # Use ThreadPoolExecutor to parallelize (yfinance uses network I/O)
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as exc:
        futures = {
            exc.submit(_fetch_single_history, t, start, end, interval, retries): t
            for t in tickers
        }
        for fut in concurrent.futures.as_completed(futures):
            t = futures[fut]
            try:
                df = fut.result()
                results[t] = df
                logger.info("Fetched %s rows for %s", len(df), t)
            except Exception as e:
                logger.exception("Exception fetching %s: %s", t, e)
                results[t] = pd.DataFrame()
    return results


def combine_histories_to_long(df_map: Dict[str, pd.DataFrame], keep_cols: List[str] = None) -> pd.DataFrame:
    """
    Convert dict of {ticker: df} into a long DataFrame:
    columns: ['Date', 'Ticker', 'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']
    """
    rows = []
    for ticker, df in df_map.items():
        if df is None or df.empty:
            continue
        d = df.copy()
        d = d.reset_index()
        d["Ticker"] = ticker
        rows.append(d)
    if not rows:
        return pd.DataFrame()
    long = pd.concat(rows, ignore_index=True, sort=False)
    # Standardize column names if present
    if keep_cols:
        cols = [c for c in keep_cols if c in long.columns]
        return long[["Date", "Ticker"] + cols]
    # default selection
    pick = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"] if c in long.columns]
    return long[["Date", "Ticker"] + pick]


def save_company_histories(histories: Dict[str, pd.DataFrame], out_dir: str = "company_histories"):
    """
    Save each ticker's DataFrame as CSV into out_dir/<ticker>.csv
    """
    os.makedirs(out_dir, exist_ok=True)
    for ticker, df in histories.items():
        path = os.path.join(out_dir, f"{ticker}.csv")
        try:
            df.to_csv(path, index=True)
            logger.info("Saved %s (%d rows)", path, len(df))
        except Exception as e:
            logger.exception("Failed to save %s: %s", path, e)


def fetch_index_and_companies(start: str, end: str, interval: str = "1d", threads: int = 6) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, pd.DataFrame]]:
    """
    Convenience function to:
      - get constituents (wikipedia)
      - fetch index (^NSEI)
      - fetch all company histories in parallel

    Returns: (components_df, index_df, histories_dict)
    """
    comps = get_nifty_components_wikipedia()
    index_df = fetch_nifty_index_yfinance(start, end, interval)
    tickers = comps["yahoo_ticker"].tolist()
    histories = fetch_companies_history_yfinance(tickers, start, end, interval, threads)
    return comps, index_df, histories


if __name__ == "__main__":
    # Quick local test example
    comps = get_nifty_components_wikipedia()
    print("Found constituents:", len(comps))
    idx = fetch_nifty_index_yfinance("2020-01-01", "2023-12-31")
    print("Index sample:", idx.head())
    tickers = comps["yahoo_ticker"].tolist()
    histories = fetch_companies_history_yfinance(tickers[:6], "2020-01-01", "2023-12-31", threads=4)
    for t, df in histories.items():
        print(t, df.shape)
