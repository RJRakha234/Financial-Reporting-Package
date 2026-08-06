"""Volatility, bands and regime.

Regime matters more than level. "ATR is 12" says nothing; "ATR is at the 92nd
percentile of its own trailing year" is a statement a decision engine can gate
on, which is why every level here is carried alongside its percentile.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nifty50.features.core import (
    ema,
    rolling_percentile_rank,
    sma,
    true_range,
    wilder_ema,
)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's ATR."""
    return wilder_ema(true_range(high, low, close), period).rename(f"atr_{period}")


def atr_percentile(atr_values: pd.Series, window: int) -> pd.Series:
    """Where current ATR sits in its own trailing distribution, in [0, 1]."""
    return rolling_percentile_rank(atr_values, window).rename("atr_percentile")


def bollinger(close: pd.Series, window: int, num_std: float) -> pd.DataFrame:
    """Bollinger bands with %B and bandwidth.

    ``%B`` locates price within the bands (0 = lower, 1 = upper) and
    ``bandwidth`` measures their width relative to the middle band — the squeeze
    metric. Both are scale-free, so they compare across a ₹200 name and a
    ₹40,000 one.
    """
    middle = sma(close, window)
    # ddof=0 to match the standard definition, which uses the population sigma
    # of the window rather than a sample estimate.
    deviation = close.rolling(window=window, min_periods=window).std(ddof=0)
    upper = middle + num_std * deviation
    lower = middle - num_std * deviation
    span = upper - lower
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            # %B locates price *within* the span, so a zero-width band makes it
            # genuinely undefined (0/0) rather than 0.
            "bb_percent_b": (close - lower) / span.replace(0.0, np.nan),
            # Bandwidth measures the span itself: zero width is a real, maximal
            # squeeze reading, not a missing value.
            "bb_bandwidth": span / middle.replace(0.0, np.nan),
        }
    )


def keltner(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    ema_period: int,
    atr_period: int,
    multiplier: float,
) -> pd.DataFrame:
    """Keltner channels: an EMA spine with ATR-scaled bands."""
    middle = ema(close, ema_period)
    band = multiplier * atr(high, low, close, atr_period)
    return pd.DataFrame(
        {
            "keltner_middle": middle,
            "keltner_upper": middle + band,
            "keltner_lower": middle - band,
        }
    )


def squeeze(bollinger_frame: pd.DataFrame, keltner_frame: pd.DataFrame) -> pd.Series:
    """True while the Bollinger bands sit inside the Keltner channel.

    The classic volatility-compression read: a squeeze says the market is coiled,
    not which way it will release, so it belongs in the regime layer rather than
    in a directional vote.
    """
    inside = (bollinger_frame["bb_upper"] < keltner_frame["keltner_upper"]) & (
        bollinger_frame["bb_lower"] > keltner_frame["keltner_lower"]
    )
    return inside.fillna(value=False).rename("bb_squeeze")


def realized_volatility(close: pd.Series, window: int, *, bars_per_year: float) -> pd.Series:
    """Annualised standard deviation of log returns over the trailing window.

    ``bars_per_year`` must match the timeframe: 250 for daily bars, 250x25 for
    15-minute NSE bars. Getting it wrong rescales every volatility-derived
    threshold in the system, so it comes from config rather than a constant here.
    """
    ratio = (close / close.shift(1)).to_numpy(dtype="float64")
    log_returns = pd.Series(np.log(ratio), index=close.index)
    deviation = log_returns.rolling(window=window, min_periods=window).std(ddof=1)
    annualised: pd.Series = (deviation * np.sqrt(bars_per_year)).rename(f"realized_vol_{window}")
    return annualised


def volatility_regime(
    series: pd.Series, window: int, *, low_quantile: float, high_quantile: float
) -> pd.Series:
    """-1 calm, 0 normal, +1 stressed, from the series' own rolling quantiles.

    Quantiles are rolling rather than fixed so the classification adapts: 2020's
    "normal" and 2024's "normal" are not the same number, and a hardcoded
    threshold would label an entire year as stressed.
    """
    percentile = rolling_percentile_rank(series, window)
    regime = pd.Series(0, index=series.index, dtype="int64")
    regime[percentile <= low_quantile] = -1
    regime[percentile >= high_quantile] = 1
    regime[percentile.isna()] = 0
    return regime.rename("volatility_regime")


def vix_gate(vix_close: pd.Series, target_index: pd.Index, *, panic_level: float) -> pd.DataFrame:
    """India VIX aligned to a symbol's bars, plus a panic flag.

    Note on the spec's "VIX term structure": India VIX is a single 30-day index
    and NSE publishes no term structure for it. A near/next-month implied-vol
    ratio would have to be computed from the NIFTY option chain, which is Phase 5
    data the engine does not yet ingest. Rather than fabricate a term structure
    from the spot index, only the level and the panic gate are emitted here.
    """
    aligned = vix_close.reindex(target_index).ffill()
    return pd.DataFrame(
        {
            "india_vix": aligned,
            "india_vix_panic": (aligned >= panic_level).fillna(value=False),
        },
        index=target_index,
    )
