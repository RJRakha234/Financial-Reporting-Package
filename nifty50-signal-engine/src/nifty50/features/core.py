"""Indicator primitives.

Everything here is backward-looking by construction. There is no ``shift(-n)``
anywhere in this package, and the look-ahead test in
``tests/test_lookahead.py`` proves it empirically by truncating the future and
checking that no feature value at *t* moves.

Two conventions worth stating once:

*Wilder smoothing is not the same as an EMA.* RSI, ATR and ADX all use Wilder's
smoother, ``alpha = 1/n``, whereas MACD and the EMA ribbon use the conventional
``alpha = 2/(n+1)``. Mixing them up shifts every level by a fixed amount and is
the most common reason a home-built RSI disagrees with a charting package.

*Warm-up is NaN, never zero.* An indicator that has not seen enough history
returns NaN. Filling with zero would let the decision engine act on a value that
does not exist yet.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Conventional EMA, ``alpha = 2/(span+1)``.

    ``adjust=False`` gives the recursive form charting packages use; ``True``
    would produce a different warm-up and quietly disagree with every reference.
    """
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder_ema(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoother, ``alpha = 1/period``. Used by RSI, ATR and ADX."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(H-L, |H-prev C|, |L-prev C|).

    The previous close is what makes this a *true* range: it accounts for the
    gap between sessions, which for NSE equities is where a large share of the
    total move lives.
    """
    previous_close = close.shift(1)
    spans = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return spans.max(axis=1, skipna=False)


def rolling_zscore(series: pd.Series, window: int, *, min_periods: int | None = None) -> pd.Series:
    """Z-score against the trailing window, current bar included."""
    periods = min_periods or window
    rolling = series.rolling(window=window, min_periods=periods)
    mean = rolling.mean()
    # ddof=0: the window is the population we are scoring against, not a sample
    # drawn from a larger one.
    std = rolling.std(ddof=0)
    return (series - mean).where(std > 0) / std.replace(0.0, np.nan)


def rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    """Fraction of the trailing window at or below the current value, in [0, 1].

    Used for ATR percentile and volatility regime: "is today's volatility high"
    only means something relative to this instrument's own recent history.
    """
    return series.rolling(window=window, min_periods=window).apply(
        lambda values: float((values <= values[-1]).mean()), raw=True
    )


def rolling_slope(series: pd.Series, window: int) -> pd.Series:
    """OLS slope per bar over the trailing window.

    Ordinary least squares against ``0..window-1``; the x values are fixed so
    the denominator is a constant and this stays cheap.
    """
    if window < 2:
        raise ValueError("slope window must be at least 2")
    x = np.arange(window, dtype="float64")
    x_centred = x - x.mean()
    denominator = float((x_centred**2).sum())

    def _slope(values: np.ndarray) -> float:
        return float((x_centred * (values - values.mean())).sum() / denominator)

    return series.rolling(window=window, min_periods=window).apply(_slope, raw=True)


def crossover_state(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """+1 while ``fast`` is above ``slow``, -1 below, 0 where either is NaN."""
    difference = fast - slow
    state = pd.Series(0, index=fast.index, dtype="int64")
    state[difference > 0] = 1
    state[difference < 0] = -1
    state[difference.isna()] = 0
    return state


def crossover_events(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """+1 on the bar a cross up completes, -1 on a cross down, else 0.

    The event is stamped on the bar where the relationship *changed*, which is
    the first bar an observer could have acted on — not the bar before.
    """
    state = crossover_state(fast, slow)
    previous = state.shift(1).fillna(0).astype("int64")
    events = pd.Series(0, index=fast.index, dtype="int64")
    events[(state == 1) & (previous == -1)] = 1
    events[(state == -1) & (previous == 1)] = -1
    return events


def bars_since(condition: pd.Series) -> pd.Series:
    """Bars elapsed since ``condition`` was last True. NaN until the first one."""
    truthy = condition.fillna(value=False).astype(bool)
    positions = pd.Series(np.arange(len(truthy), dtype="float64"), index=truthy.index)
    last_true = positions.where(truthy).ffill()
    return positions - last_true


def pct_change_n(series: pd.Series, periods: int) -> pd.Series:
    """Simple return over ``periods`` bars, as a fraction."""
    return series.pct_change(periods=periods, fill_method=None)
