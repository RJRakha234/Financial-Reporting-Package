import pandas as pd
import yfinance as yf
from datetime import date
try:
    from nsepy import get_history
except Exception:
    get_history = None


def fetch_nifty_yfinance(start: str, end: str, interval: str = "1d") -> pd.DataFrame:
    """
    Fetch NIFTY 50 historical data using yfinance (Yahoo Finance symbol ^NSEI).
    start/end are YYYY-MM-DD strings. interval examples: '1d', '1wk', '1mo'.
    Returns a DataFrame with Date index and OHLCV.
    """
    ticker = "^NSEI"
    df = yf.download(ticker, start=start, end=end, interval=interval, progress=False)
    df.index = pd.to_datetime(df.index)
    return df


def fetch_nifty_nsepy(start_date: date, end_date: date) -> pd.DataFrame:
    """
    Fetch NIFTY 50 historical data using nsepy.
    start_date and end_date are datetime.date objects.
    Note: nsepy depends on NSE site behavior — can be less reliable than yfinance.
    """
    if get_history is None:
        raise RuntimeError("nsepy is not installed or failed to import. pip install nsepy")
    # For index data, pass symbol="NIFTY 50" and index=True
    df = get_history(symbol="NIFTY 50", start=start_date, end=end_date, index=True)
    # nsepy returns a DataFrame; ensure datetime index
    df.index = pd.to_datetime(df.index)
    return df


def save_to_csv(df: pd.DataFrame, path: str):
    """Save DataFrame to CSV with date as first column."""
    df.to_csv(path, index=True)
    print(f"Saved {len(df)} rows to {path}")


if __name__ == "__main__":
    # Example usage for quick testing
    # 1) yfinance example
    df_yf = fetch_nifty_yfinance("2020-01-01", "2023-12-31")
    print("yfinance sample rows:", df_yf.head())
    save_to_csv(df_yf, "nifty_yfinance_2020-2023.csv")

    # 2) nsepy example (uncomment to run)
    # from datetime import date
    # df_nsepy = fetch_nifty_nsepy(date(2020,1,1), date(2023,12,31))
    # print("nsepy sample rows:", df_nsepy.head())
    # save_to_csv(df_nsepy, "nifty_nsepy_2020-2023.csv")
