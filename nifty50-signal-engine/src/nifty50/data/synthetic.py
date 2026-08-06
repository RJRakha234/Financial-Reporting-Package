"""Deterministic synthetic bar generation for tests and offline development.

Synthetic data is used to exercise plumbing — session boundaries, aggregation,
storage, gap detection — never to evaluate a strategy. A geometric random walk
has no microstructure, no volume seasonality and no fat tails, so any backtest
result computed on it is meaningless by construction. It is generated with an
explicit seed so failures reproduce.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from nifty50.domain import BAR_INDEX_NAME, IST, Timeframe
from nifty50.trading_calendar.calendar import TradingCalendar

# Shape parameters for the walk. Chosen to look plausible for a large-cap NSE
# name; not calibrated to anything and not to be used for inference.
_DEFAULT_ANNUAL_VOL: float = 0.25
_TRADING_DAYS_PER_YEAR: int = 250
_INTRABAR_RANGE_MULTIPLIER: float = 1.6
_BASE_VOLUME: int = 50_000
# NSE intraday volume is strongly U-shaped: heavy at the open, quiet at midday,
# heavy into the close. Flat volume would make the session-matched volume
# z-score look informative when it is not.
_U_SHAPE_DEPTH: float = 0.6


def generate_session_bars(
    calendar: TradingCalendar,
    start: dt.date,
    end: dt.date,
    timeframe: Timeframe,
    *,
    start_price: float = 1000.0,
    annual_vol: float = _DEFAULT_ANNUAL_VOL,
    seed: int = 0,
    drift: float = 0.0,
) -> pd.DataFrame:
    """Generate calendar-aligned OHLCV bars for one instrument."""
    bar_starts = calendar.expected_bar_starts(start, end, timeframe)
    if not bar_starts:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz=IST, name=BAR_INDEX_NAME)
        return empty

    rng = np.random.default_rng(seed)
    count = len(bar_starts)
    bars_per_day = calendar.bars_per_session(bar_starts[0].date(), timeframe)
    per_bar_vol = annual_vol / np.sqrt(_TRADING_DAYS_PER_YEAR * max(bars_per_day, 1))

    returns = rng.normal(loc=drift / count, scale=per_bar_vol, size=count)
    closes = start_price * np.exp(np.cumsum(returns))
    opens = np.concatenate([[start_price], closes[:-1]])

    spread = np.abs(returns) * closes * _INTRABAR_RANGE_MULTIPLIER
    highs = np.maximum(opens, closes) + spread * rng.uniform(0.0, 1.0, size=count)
    lows = np.minimum(opens, closes) - spread * rng.uniform(0.0, 1.0, size=count)

    volumes = _u_shaped_volume(bar_starts, bars_per_day, rng)

    frame = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.DatetimeIndex(bar_starts, name=BAR_INDEX_NAME),
    )
    # Enforce the OHLC contract exactly; the walk can otherwise produce a high
    # marginally below an open after rounding.
    frame["high"] = frame[["open", "high", "close"]].max(axis=1)
    frame["low"] = frame[["open", "low", "close"]].min(axis=1)
    frame[["open", "high", "low", "close"]] = frame[["open", "high", "low", "close"]].round(2)
    frame["volume"] = frame["volume"].astype("int64")
    return frame


def _u_shaped_volume(
    bar_starts: list[dt.datetime], bars_per_day: int, rng: np.random.Generator
) -> np.ndarray:
    """Volume with a realistic intraday U-shape and day-to-day noise."""
    count = len(bar_starts)
    if bars_per_day <= 1:
        return rng.integers(_BASE_VOLUME, _BASE_VOLUME * 3, size=count)
    positions = np.array([i % bars_per_day for i in range(count)], dtype="float64") / max(
        bars_per_day - 1, 1
    )
    # Parabola peaking at both ends, trough at midday.
    shape = 1.0 + _U_SHAPE_DEPTH * (2.0 * positions - 1.0) ** 2
    noise = rng.lognormal(mean=0.0, sigma=0.3, size=count)
    return np.maximum(1, (_BASE_VOLUME * shape * noise)).astype("int64")


def apply_unadjusted_split(
    frame: pd.DataFrame, ex_date: dt.date, ratio_new: float, ratio_old: float
) -> pd.DataFrame:
    """Re-introduce the raw, unadjusted print of a split into a clean series.

    Used to build fixtures that look exactly like vendor data before adjustment:
    prices before ``ex_date`` are scaled *up* by the split ratio and volumes
    scaled down, which is what actually traded at the time.
    """
    out = frame.copy()
    mask = np.array([ts.date() < ex_date for ts in out.index])
    factor = ratio_new / ratio_old
    for column in ("open", "high", "low", "close"):
        out.loc[mask, column] = out.loc[mask, column] * factor
    out.loc[mask, "volume"] = (out.loc[mask, "volume"] / factor).astype("int64")
    return out
