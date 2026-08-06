"""Trend and momentum indicators.

Two look-ahead traps are handled explicitly here, because both are easy to get
wrong and neither announces itself:

**Ichimoku's Chikou span is deliberately not emitted.** Chikou is the close
displaced 26 periods *backwards*, so its value at bar ``t-26`` is ``close[t]``.
Reading it as a feature at its plotted position is reading the future. The
Senkou spans are the opposite — displaced *forwards*, so at bar ``t`` they carry
information computed at ``t-26`` — and those are safe and are emitted.

**Divergence is reported at confirmation, not at the pivot.** A swing high is
only a swing high once ``right`` further bars have failed to exceed it. Stamping
the divergence on the pivot bar would credit the strategy with knowledge it
could not have had for another ``right`` bars.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd

from nifty50.features.core import (
    crossover_state,
    ema,
    pct_change_n,
    true_range,
    wilder_ema,
)

_PERCENT: float = 100.0


def ema_ribbon(close: pd.Series, periods: tuple[int, ...]) -> pd.DataFrame:
    """One EMA column per period, named ``ema_<n>``."""
    return pd.DataFrame({f"ema_{period}": ema(close, period) for period in periods})


def ema_stack_score(ribbon: pd.DataFrame, periods: tuple[int, ...]) -> pd.Series:
    """+1 when the EMAs are stacked fast-over-slow, -1 when fully inverted.

    Intermediate states scale linearly, so a partially-aligned ribbon reads as a
    weaker trend rather than as no trend at all.
    """
    columns = [f"ema_{period}" for period in periods]
    pairs = list(pairwise(columns))
    if not pairs:
        return pd.Series(0.0, index=ribbon.index)
    votes = [
        pd.Series(
            np.sign((ribbon[fast] - ribbon[slow]).to_numpy(dtype="float64")),
            index=ribbon.index,
        )
        for fast, slow in pairs
    ]
    score: pd.Series = pd.concat(votes, axis=1).mean(axis=1).rename("ema_stack_score")
    return score


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    line = ema(close, fast) - ema(close, slow)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": line,
            "macd_signal": signal_line,
            "macd_hist": line - signal_line,
        }
    )


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI.

    Uses Wilder smoothing (``alpha = 1/period``), not a conventional EMA. With
    ``2/(period+1)`` the values are systematically different and will not match
    any charting package.
    """
    change = close.diff()
    gains = change.clip(lower=0.0)
    losses = (-change).clip(lower=0.0)
    average_gain = wilder_ema(gains, period)
    average_loss = wilder_ema(losses, period)
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    result = _PERCENT - (_PERCENT / (1.0 + relative_strength))

    # An all-gain window has a zero denominator and RSI is 100 by definition.
    all_gains = (average_loss == 0.0) & (average_gain > 0.0)
    result = result.where(~all_gains, _PERCENT)
    # A perfectly flat window has neither gains nor losses, so RSI is genuinely
    # undefined. Returning 100 there would read as maximally overbought on a
    # stock that has not moved — NaN is the honest answer.
    flat = (average_loss == 0.0) & (average_gain == 0.0)
    result = result.where(~flat)
    return result.where(average_gain.notna() & average_loss.notna()).rename(f"rsi_{period}")


def pivot_highs(series: pd.Series, left: int, right: int) -> pd.Series:
    """Confirmed swing highs, stamped on the bar that CONFIRMS them.

    The value is the pivot's price; the position is ``pivot + right``, which is
    the first bar on which an observer could know the pivot held.
    """
    rolling_max = series.rolling(window=left + right + 1, center=False).max()
    candidate = series.shift(right)
    is_pivot = (candidate == rolling_max) & candidate.notna()
    return candidate.where(is_pivot)


def pivot_lows(series: pd.Series, left: int, right: int) -> pd.Series:
    rolling_min = series.rolling(window=left + right + 1, center=False).min()
    candidate = series.shift(right)
    is_pivot = (candidate == rolling_min) & candidate.notna()
    return candidate.where(is_pivot)


def rsi_divergence(close: pd.Series, rsi_values: pd.Series, *, left: int, right: int) -> pd.Series:
    """+1 bullish divergence, -1 bearish, 0 otherwise — at confirmation.

    Bearish: price makes a higher confirmed swing high while RSI makes a lower
    one. Bullish is the mirror. Both are stamped ``right`` bars after the pivot
    itself, which is when they actually become knowable.
    """
    price_highs = pivot_highs(close, left, right)
    price_lows = pivot_lows(close, left, right)
    rsi_highs = pivot_highs(rsi_values, left, right)
    rsi_lows = pivot_lows(rsi_values, left, right)

    signal = pd.Series(0, index=close.index, dtype="int64")

    previous_price_high = price_highs.ffill().shift(1)
    previous_rsi_high = rsi_highs.ffill().shift(1)
    bearish = (
        price_highs.notna()
        & rsi_highs.notna()
        & (price_highs > previous_price_high)
        & (rsi_highs < previous_rsi_high)
    )

    previous_price_low = price_lows.ffill().shift(1)
    previous_rsi_low = rsi_lows.ffill().shift(1)
    bullish = (
        price_lows.notna()
        & rsi_lows.notna()
        & (price_lows < previous_price_low)
        & (rsi_lows > previous_rsi_low)
    )

    signal[bearish.fillna(value=False)] = -1
    signal[bullish.fillna(value=False)] = 1
    return signal.rename("rsi_divergence")


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.DataFrame:
    """Wilder's ADX with +DI and -DI.

    ADX measures trend *strength* without direction: it rises in a strong
    downtrend exactly as it does in a strong uptrend. Direction comes from the
    DI pair, which is why all three are returned together.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )

    smoothed_tr = wilder_ema(true_range(high, low, close), period)
    plus_di = _PERCENT * wilder_ema(plus_dm, period) / smoothed_tr.replace(0.0, np.nan)
    minus_di = _PERCENT * wilder_ema(minus_dm, period) / smoothed_tr.replace(0.0, np.nan)

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    directional_index = _PERCENT * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame(
        {
            "plus_di": plus_di,
            "minus_di": minus_di,
            "adx": wilder_ema(directional_index, period),
        }
    )


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_values: pd.Series,
    multiplier: float,
) -> pd.DataFrame:
    """Supertrend line and direction (+1 up, -1 down).

    The band recursion is inherently sequential — each final band depends on the
    previous one — so this is an explicit loop rather than a vectorised
    expression. Fifty symbols of seven years is small enough that clarity wins.
    """
    midpoint = (high + low) / 2.0
    upper_basic = midpoint + multiplier * atr_values
    lower_basic = midpoint - multiplier * atr_values

    upper = np.full(len(close), np.nan)
    lower = np.full(len(close), np.nan)
    direction = np.zeros(len(close), dtype="int64")
    line = np.full(len(close), np.nan)

    closes = close.to_numpy(dtype="float64")
    upper_values = upper_basic.to_numpy(dtype="float64")
    lower_values = lower_basic.to_numpy(dtype="float64")

    started = False
    for i in range(len(closes)):
        if not np.isfinite(upper_values[i]) or not np.isfinite(lower_values[i]):
            continue
        if not started:
            upper[i], lower[i] = upper_values[i], lower_values[i]
            direction[i] = 1 if closes[i] >= lower_values[i] else -1
            line[i] = lower[i] if direction[i] == 1 else upper[i]
            started = True
            continue

        previous = i - 1
        # Bands only ratchet toward price; they never loosen while the trend holds.
        upper[i] = (
            min(upper_values[i], upper[previous])
            if closes[previous] <= upper[previous]
            else upper_values[i]
        )
        lower[i] = (
            max(lower_values[i], lower[previous])
            if closes[previous] >= lower[previous]
            else lower_values[i]
        )

        if direction[previous] == 1:
            direction[i] = -1 if closes[i] < lower[i] else 1
        else:
            direction[i] = 1 if closes[i] > upper[i] else -1
        line[i] = lower[i] if direction[i] == 1 else upper[i]

    return pd.DataFrame(
        {
            "supertrend": pd.Series(line, index=close.index),
            "supertrend_direction": pd.Series(direction, index=close.index),
        }
    )


def ichimoku(
    high: pd.Series,
    low: pd.Series,
    *,
    tenkan: int,
    kijun: int,
    senkou_b: int,
    displacement: int,
) -> pd.DataFrame:
    """Ichimoku, minus the Chikou span.

    Chikou is omitted on purpose: it is the close displaced *backwards*, so
    reading it at its plotted position means reading a price that has not
    happened yet. The Senkou spans are displaced forwards and are therefore
    computed from data ``displacement`` bars old — safe, and emitted.
    """

    def _midpoint(window: int) -> pd.Series:
        highest = high.rolling(window=window, min_periods=window).max()
        lowest = low.rolling(window=window, min_periods=window).min()
        return (highest + lowest) / 2.0

    tenkan_sen = _midpoint(tenkan)
    kijun_sen = _midpoint(kijun)
    return pd.DataFrame(
        {
            "ichimoku_tenkan": tenkan_sen,
            "ichimoku_kijun": kijun_sen,
            # shift(+d) reads a value computed d bars ago: past, not future.
            "ichimoku_senkou_a": ((tenkan_sen + kijun_sen) / 2.0).shift(displacement),
            "ichimoku_senkou_b": _midpoint(senkou_b).shift(displacement),
        }
    )


def cloud_position(close: pd.Series, senkou_a: pd.Series, senkou_b: pd.Series) -> pd.Series:
    """+1 above the cloud, -1 below, 0 inside it."""
    upper = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
    lower = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1)
    position = pd.Series(0, index=close.index, dtype="int64")
    position[close > upper] = 1
    position[close < lower] = -1
    position[upper.isna() | lower.isna()] = 0
    return position.rename("ichimoku_cloud_position")


def rate_of_change(close: pd.Series, periods: int) -> pd.Series:
    return pct_change_n(close, periods).rename(f"roc_{periods}")


def relative_strength(close: pd.Series, index_close: pd.Series, periods: int) -> pd.Series:
    """Return over ``periods`` minus the index's return over the same span.

    A stock up 1% on a day the Nifty is up 2% is weak, not strong. Absolute
    momentum on a single name mostly measures the market, which is why every
    momentum read here is carried alongside its index-relative version.
    """
    aligned = index_close.reindex(close.index).ffill()
    return (pct_change_n(close, periods) - pct_change_n(aligned, periods)).rename(
        f"relative_strength_{periods}"
    )


def trend_state(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return crossover_state(fast, slow).rename("trend_state")
