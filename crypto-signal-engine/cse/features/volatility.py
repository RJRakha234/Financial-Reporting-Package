"""Volatility, bands, and volatility-regime classification.

Bollinger Bands (%B and bandwidth), ATR and its rolling percentile, Keltner
Channels, realized volatility, and a low/normal/high regime label.

The regime thresholds are **rolling** quantiles. Classifying volatility against
a quantile of the whole series is one of the most common look-ahead bugs in
published strategies: it tells every bar in 2023 how volatile 2025 turned out.
"""

from __future__ import annotations

import pandas as pd

from cse.config import VolatilityFeatureConfig
from cse.features.base import (
    ema,
    log_returns,
    rolling_percentile_rank,
    safe_divide,
    true_range,
    wilder_smooth,
)

REGIME_LOW = -1
REGIME_NORMAL = 0
REGIME_HIGH = 1


def atr(
    high: pd.Series[float],
    low: pd.Series[float],
    close: pd.Series[float],
    period: int,
) -> pd.Series[float]:
    """Average True Range (Wilder)."""
    result = wilder_smooth(true_range(high, low, close), period)
    result.name = "atr"
    return result


def bollinger(close: pd.Series[float], period: int, num_std: float) -> pd.DataFrame:
    """Bollinger Bands with %B and bandwidth.

    ``%B`` locates price within the bands (0 = lower, 1 = upper, outside the
    band goes beyond that range). ``bandwidth`` is the band width normalised by
    the middle band, which is the squeeze/expansion signal.
    """
    middle = close.rolling(period, min_periods=period).mean()
    # ddof=0 to match the conventional TA definition (population stdev).
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_percent_b": safe_divide(close - lower, upper - lower),
            "bb_bandwidth": safe_divide(upper - lower, middle),
        }
    )


def keltner(
    high: pd.Series[float],
    low: pd.Series[float],
    close: pd.Series[float],
    period: int,
    atr_period: int,
    multiplier: float,
) -> pd.DataFrame:
    """Keltner Channels: EMA centre with ATR-scaled bands."""
    middle = ema(close, period)
    channel_atr = atr(high, low, close, atr_period)
    upper = middle + multiplier * channel_atr
    lower = middle - multiplier * channel_atr
    return pd.DataFrame(
        {
            "keltner_middle": middle,
            "keltner_upper": upper,
            "keltner_lower": lower,
            "keltner_position": safe_divide(close - lower, upper - lower),
        }
    )


def realized_volatility(close: pd.Series[float], window: int) -> pd.Series[float]:
    """Rolling standard deviation of log returns, per bar (not annualised).

    Left in per-bar units on purpose: annualising requires a bars-per-year
    constant that differs by timeframe, and baking one in here would silently
    mis-scale every timeframe but the one it was chosen for.
    """
    result = log_returns(close).rolling(window, min_periods=window).std(ddof=0)
    result.name = "realized_volatility"
    return result


def volatility_regime(
    volatility: pd.Series[float], config: VolatilityFeatureConfig
) -> pd.DataFrame:
    """Classify volatility as low / normal / high against ROLLING quantiles.

    At bar *t* the thresholds come from the trailing ``regime_window`` bars, so
    the label uses only what was knowable at *t*.
    """
    window = config.regime_window
    low_threshold = volatility.rolling(window, min_periods=window).quantile(
        config.regime_low_quantile
    )
    high_threshold = volatility.rolling(window, min_periods=window).quantile(
        config.regime_high_quantile
    )

    # A window with no variation collapses both quantiles onto the same value,
    # so every comparison is simultaneously true. Without this guard a dead-flat
    # stretch — an illiquid period, a halted market — is reported as HIGH
    # volatility, which is exactly backwards.
    degenerate = high_threshold <= low_threshold

    regime = pd.Series(REGIME_NORMAL, index=volatility.index, dtype="int64")
    regime[volatility <= low_threshold] = REGIME_LOW
    regime[volatility >= high_threshold] = REGIME_HIGH
    unknown = volatility.isna() | low_threshold.isna() | high_threshold.isna() | degenerate
    regime[unknown] = REGIME_NORMAL

    return pd.DataFrame(
        {
            "volatility_regime": regime,
            "volatility_regime_known": (~unknown).astype("int64"),
            "volatility_low_threshold": low_threshold,
            "volatility_high_threshold": high_threshold,
        }
    )


def compute(frame: pd.DataFrame, config: VolatilityFeatureConfig) -> pd.DataFrame:
    """All volatility features for a canonical candle frame."""
    high, low, close = frame["high"], frame["low"], frame["close"]

    atr_values = atr(high, low, close, config.atr_period)
    realized = realized_volatility(close, config.realized_vol_window)

    parts = [
        bollinger(close, config.bollinger_period, config.bollinger_std),
        keltner(
            high,
            low,
            close,
            config.keltner_period,
            config.keltner_atr_period,
            config.keltner_multiplier,
        ),
        atr_values.to_frame(),
        realized.to_frame(),
        volatility_regime(realized, config),
    ]
    out = pd.concat(parts, axis=1)

    out["atr_percentile"] = rolling_percentile_rank(atr_values, config.atr_percentile_window)
    # ATR as a fraction of price makes it comparable across symbols priced
    # three orders of magnitude apart (BTC vs SOL).
    out["atr_normalised"] = safe_divide(atr_values, close)

    # Bollinger inside Keltner is the classic "squeeze" precondition.
    squeeze = (out["bb_upper"] < out["keltner_upper"]) & (out["bb_lower"] > out["keltner_lower"])
    out["bb_squeeze"] = squeeze.astype("int64")
    out.loc[out["bb_upper"].isna() | out["keltner_upper"].isna(), "bb_squeeze"] = 0
    return out
