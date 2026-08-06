"""Trend and momentum indicators.

EMA stack and crossover states, MACD, RSI with divergence, ADX, Ichimoku cloud
position, and Supertrend. Every value at bar *t* is computed from bars ``<= t``.

One indicator here is deliberately incomplete: see :func:`ichimoku` on why the
Chikou span is computed but never exposed as a feature.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cse.config import TrendFeatureConfig
from cse.features.base import (
    confirmed_pivots,
    crossover,
    ema,
    safe_divide,
    true_range,
    wilder_smooth,
)


def ema_stack(close: pd.Series[float], periods: list[int]) -> pd.DataFrame:
    """EMAs plus their pairwise ordering, which is the actual trend signal.

    ``ema_alignment`` is +1 when every EMA is stacked shortest-above-longest
    (textbook uptrend), -1 when fully inverted, and 0 when mixed.
    """
    out = pd.DataFrame(index=close.index)
    ordered = sorted(periods)
    for period in ordered:
        out[f"ema_{period}"] = ema(close, period)

    columns = [f"ema_{p}" for p in ordered]
    values = out[columns].to_numpy()
    with np.errstate(invalid="ignore"):
        ascending = np.all(np.diff(values, axis=1) < 0, axis=1)  # short > long
        descending = np.all(np.diff(values, axis=1) > 0, axis=1)  # short < long
    alignment = np.where(ascending, 1, np.where(descending, -1, 0))
    alignment = np.where(np.isnan(values).any(axis=1), 0, alignment)
    out["ema_alignment"] = alignment.astype("int64")

    # Price relative to the longest EMA: the coarsest regime filter there is.
    longest = f"ema_{ordered[-1]}"
    out["price_above_slow_ema"] = (close > out[longest]).astype("int64")
    out.loc[out[longest].isna(), "price_above_slow_ema"] = 0

    if len(ordered) >= 2:
        out["ema_cross_fast_slow"] = crossover(out[f"ema_{ordered[0]}"], out[f"ema_{ordered[1]}"])
    return out


def macd(close: pd.Series[float], fast: int, slow: int, signal: int) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    # The signal line is an EMA of the MACD line, which only exists from bar
    # `slow-1` onward; seed it from that point so it is not skewed by NaNs.
    signal_line = ema(macd_line.dropna(), signal).reindex(close.index)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": histogram,
            "macd_cross": crossover(macd_line, signal_line),
        }
    )


def rsi(close: pd.Series[float], period: int) -> pd.Series[float]:
    """Wilder's RSI.

    Bounded [0, 100]. A flat series has no losses and no gains; that is defined
    here as 50 (neutral) rather than the 100 a naive 0/0 guard produces.
    """
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    average_gain = wilder_smooth(gains, period)
    average_loss = wilder_smooth(losses, period)

    relative_strength = safe_divide(average_gain, average_loss)
    result = 100.0 - (100.0 / (1.0 + relative_strength))
    # average_loss == 0 with positive gains -> RSI 100; both zero -> neutral.
    both_flat = (average_gain == 0.0) & (average_loss == 0.0)
    only_gains = (average_loss == 0.0) & (average_gain > 0.0)
    result[only_gains] = 100.0
    result[both_flat] = 50.0
    result.name = "rsi"
    return result


def rsi_divergence(
    close: pd.Series[float], rsi_values: pd.Series[float], config: TrendFeatureConfig
) -> pd.Series[int]:
    """Regular RSI divergence: +1 bullish, -1 bearish, 0 none.

    Bearish: price makes a higher high while RSI makes a lower high.
    Bullish: price makes a lower low while RSI makes a higher low.

    Pivots are only considered once *confirmed* — ``rsi_pivot_window`` bars
    after they occurred. Detecting divergence at the pivot bar itself is the
    single most common look-ahead bug in published TA code: at that moment you
    cannot yet know it was a pivot.
    """
    window = config.rsi_pivot_window
    lookback = config.rsi_divergence_lookback

    high_confirmations = confirmed_pivots(close, window, kind="high")
    low_confirmations = confirmed_pivots(close, window, kind="low")

    price = close.to_numpy(dtype=np.float64)
    momentum = rsi_values.to_numpy(dtype=np.float64)
    n = len(price)
    result = np.zeros(n, dtype=np.int64)

    # Pivot located `window` bars before its confirmation bar.
    high_pivot_positions = [int(i) - window for i in np.flatnonzero(high_confirmations.to_numpy())]
    low_pivot_positions = [int(i) - window for i in np.flatnonzero(low_confirmations.to_numpy())]

    def _scan(positions: list[int], *, bearish: bool) -> None:
        for index, pivot in enumerate(positions):
            if index == 0:
                continue
            previous_pivot = positions[index - 1]
            if pivot - previous_pivot > lookback:
                continue
            confirmation_bar = pivot + window
            if confirmation_bar >= n:
                continue
            if np.isnan(momentum[pivot]) or np.isnan(momentum[previous_pivot]):
                continue
            if bearish:
                if (
                    price[pivot] > price[previous_pivot]
                    and momentum[pivot] < momentum[previous_pivot]
                ):
                    result[confirmation_bar] = -1
            else:
                if (
                    price[pivot] < price[previous_pivot]
                    and momentum[pivot] > momentum[previous_pivot]
                ):
                    result[confirmation_bar] = 1

    _scan(high_pivot_positions, bearish=True)
    _scan(low_pivot_positions, bearish=False)
    return pd.Series(result, index=close.index, name="rsi_divergence")


def adx(
    high: pd.Series[float],
    low: pd.Series[float],
    close: pd.Series[float],
    period: int,
) -> pd.DataFrame:
    """Wilder's ADX with +DI and -DI.

    ADX measures trend *strength* only, never direction; direction comes from
    which DI dominates. Used in Phase 5 to gate trend-following signals.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
        dtype="float64",
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
        dtype="float64",
    )

    smoothed_tr = wilder_smooth(true_range(high, low, close), period)
    plus_di = 100.0 * safe_divide(wilder_smooth(plus_dm, period), smoothed_tr)
    minus_di = 100.0 * safe_divide(wilder_smooth(minus_dm, period), smoothed_tr)

    directional_index = 100.0 * safe_divide((plus_di - minus_di).abs(), plus_di + minus_di)
    adx_values = wilder_smooth(directional_index.fillna(0.0), period)
    # Keep NaN where the inputs were genuinely unknown rather than filled.
    adx_values[directional_index.isna()] = np.nan

    return pd.DataFrame({"adx": adx_values, "plus_di": plus_di, "minus_di": minus_di})


def ichimoku(
    high: pd.Series[float],
    low: pd.Series[float],
    close: pd.Series[float],
    config: TrendFeatureConfig,
) -> pd.DataFrame:
    """Ichimoku cloud position.

    The two Senkou spans are shifted **forward** by ``displacement``, so the
    value sitting at bar *t* was computed from data at ``t - displacement``.
    That is legal: it is past data plotted ahead.

    The Chikou span is the opposite — close shifted *backward*. Reading it at
    bar *t* returns the close from ``t + displacement``, which is the future.
    It is therefore deliberately **not** emitted as a feature. Any TA library
    that hands you a ``chikou`` column and any strategy that reads it at the
    current bar is using data it cannot have.
    """
    conversion = (
        high.rolling(config.ichimoku_conversion, min_periods=config.ichimoku_conversion).max()
        + low.rolling(config.ichimoku_conversion, min_periods=config.ichimoku_conversion).min()
    ) / 2.0
    base = (
        high.rolling(config.ichimoku_base, min_periods=config.ichimoku_base).max()
        + low.rolling(config.ichimoku_base, min_periods=config.ichimoku_base).min()
    ) / 2.0
    span_a = ((conversion + base) / 2.0).shift(config.ichimoku_displacement)
    span_b = (
        (
            high.rolling(config.ichimoku_span_b, min_periods=config.ichimoku_span_b).max()
            + low.rolling(config.ichimoku_span_b, min_periods=config.ichimoku_span_b).min()
        )
        / 2.0
    ).shift(config.ichimoku_displacement)

    cloud_top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    cloud_bottom = pd.concat([span_a, span_b], axis=1).min(axis=1)

    position = pd.Series(0, index=close.index, dtype="int64")
    position[close > cloud_top] = 1
    position[close < cloud_bottom] = -1
    position[cloud_top.isna() | cloud_bottom.isna()] = 0

    return pd.DataFrame(
        {
            "ichimoku_conversion": conversion,
            "ichimoku_base": base,
            "ichimoku_span_a": span_a,
            "ichimoku_span_b": span_b,
            "ichimoku_cloud_position": position,
            "ichimoku_cloud_thickness": cloud_top - cloud_bottom,
        }
    )


def supertrend(
    high: pd.Series[float],
    low: pd.Series[float],
    close: pd.Series[float],
    period: int,
    multiplier: float,
) -> pd.DataFrame:
    """Supertrend line and direction (+1 up, -1 down).

    The band-ratchet is genuinely recursive — each bar's band depends on the
    previous bar's band and the previous close — so this is an explicit loop
    rather than a vectorised expression. Vectorising it is the usual source of
    subtly wrong Supertrend implementations.
    """
    atr_values = wilder_smooth(true_range(high, low, close), period).to_numpy(dtype=np.float64)
    median_price = ((high + low) / 2.0).to_numpy(dtype=np.float64)
    close_values = close.to_numpy(dtype=np.float64)
    n = len(close_values)

    upper_basic = median_price + multiplier * atr_values
    lower_basic = median_price - multiplier * atr_values

    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    direction = np.zeros(n, dtype=np.int64)
    line = np.full(n, np.nan, dtype=np.float64)

    started = False
    for i in range(n):
        if np.isnan(atr_values[i]):
            continue
        if not started:
            upper[i], lower[i] = upper_basic[i], lower_basic[i]
            direction[i] = 1 if close_values[i] >= upper_basic[i] else -1
            line[i] = lower[i] if direction[i] == 1 else upper[i]
            started = True
            continue

        previous = i - 1
        # The band only tightens while price stays on its side of the prior band.
        upper[i] = (
            min(upper_basic[i], upper[previous])
            if close_values[previous] <= upper[previous]
            else upper_basic[i]
        )
        lower[i] = (
            max(lower_basic[i], lower[previous])
            if close_values[previous] >= lower[previous]
            else lower_basic[i]
        )

        if close_values[i] > upper[previous]:
            direction[i] = 1
        elif close_values[i] < lower[previous]:
            direction[i] = -1
        else:
            direction[i] = direction[previous]

        line[i] = lower[i] if direction[i] == 1 else upper[i]

    return pd.DataFrame(
        {
            "supertrend": line,
            "supertrend_direction": direction,
            "supertrend_upper": upper,
            "supertrend_lower": lower,
        },
        index=close.index,
    )


def compute(frame: pd.DataFrame, config: TrendFeatureConfig) -> pd.DataFrame:
    """All trend/momentum features for a canonical candle frame."""
    high, low, close = frame["high"], frame["low"], frame["close"]
    rsi_values = rsi(close, config.rsi_period)

    parts = [
        ema_stack(close, config.ema_periods),
        macd(close, config.macd_fast, config.macd_slow, config.macd_signal),
        rsi_values.to_frame(),
        rsi_divergence(close, rsi_values, config).to_frame(),
        adx(high, low, close, config.adx_period),
        ichimoku(high, low, close, config),
        supertrend(high, low, close, config.supertrend_period, config.supertrend_multiplier),
    ]
    out = pd.concat(parts, axis=1)
    out["adx_trending"] = (out["adx"] >= config.adx_trend_threshold).astype("int64")
    out.loc[out["adx"].isna(), "adx_trending"] = 0
    return out
