"""Shared indicator primitives, and the no-look-ahead rules everything obeys.

THE RULE
--------
A feature value at bar *t* may use bars ``<= t`` and nothing else. Every helper
here is written so that holds, and :mod:`tests.test_lookahead` proves it by
truncating the frame and re-computing.

The traps this module exists to avoid, all of which produce a beautiful
backtest and a worthless strategy:

* ``shift(-n)`` anywhere in a feature. Only ``shift(+n)`` is legal.
* Global statistics — ``series.quantile(0.9)``, ``series.mean()``, a fitted
  scaler over the whole history. Each embeds the future into every bar. Use the
  rolling equivalents here instead.
* ``center=True`` on a rolling window: it straddles the current bar.
* ``bfill()``. Filling backwards *is* copying the future into the past.
  Forward-fill is fine.
* Pivot/peak detection that confirms a pivot at the bar it occurred, rather
  than ``window`` bars later once it can actually be known.
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd


def log_returns(close: pd.Series[float]) -> pd.Series[float]:
    """Bar-over-bar log returns, ``ln(close_t / close_{t-1})``.

    Centralised because several modules need it and because going through numpy
    on the raw values keeps the result a properly typed Series rather than a
    bare ndarray.
    """
    ratio = (close / close.shift(1)).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        values = np.log(ratio)
    return pd.Series(values, index=close.index, name="log_return")


def wilder_smooth(series: pd.Series[float], period: int) -> pd.Series[float]:
    """Wilder's smoothing (RMA), the basis of RSI, ATR and ADX.

    Equivalent to an EWM with ``alpha = 1/period``. Wilder's original
    formulation seeds with a simple average of the first ``period`` values;
    ``adjust=False`` with an SMA seed reproduces that exactly, which is what
    reference implementations (TradingView's RMA, pandas-ta) do.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    values = series.to_numpy(dtype=np.float64)
    out = np.full(values.shape, np.nan, dtype=np.float64)

    # Seed from the first `period` VALID observations, not the first `period`
    # positions. Callers feed this the output of `.diff()`, whose first element
    # is always NaN — averaging that in would make the seed, and therefore the
    # entire recursion, NaN forever.
    first = _first_valid_position(values)
    if first is None or len(values) - first < period:
        return pd.Series(out, index=series.index, name=series.name)

    seed_end = first + period
    previous = float(np.mean(values[first:seed_end]))
    out[seed_end - 1] = previous
    alpha = 1.0 / period
    for i in range(seed_end, len(values)):
        current = values[i]
        if np.isnan(current):
            # Hold the last estimate rather than propagating NaN forever.
            out[i] = previous
            continue
        previous = previous + alpha * (current - previous)
        out[i] = previous
    return pd.Series(out, index=series.index, name=series.name)


def _first_valid_position(values: np.ndarray) -> int | None:
    """Index of the first non-NaN value, or None if there is none."""
    valid = np.flatnonzero(~np.isnan(values))
    return int(valid[0]) if valid.size else None


def ema(series: pd.Series[float], period: int) -> pd.Series[float]:
    """Exponential moving average, ``alpha = 2/(period+1)``, SMA-seeded.

    SMA seeding (rather than pandas' default of seeding on the first value)
    makes the result match the conventional TA definition and, more usefully,
    makes early values ``NaN`` instead of quietly wrong — a silently wrong
    EMA(200) over the first 200 bars is a subtle way to poison a backtest's
    opening trades.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    values = series.to_numpy(dtype=np.float64)
    out = np.full(values.shape, np.nan, dtype=np.float64)

    first = _first_valid_position(values)
    if first is None or len(values) - first < period:
        return pd.Series(out, index=series.index, name=series.name)

    alpha = 2.0 / (period + 1.0)
    seed_end = first + period
    previous = float(np.mean(values[first:seed_end]))
    out[seed_end - 1] = previous
    for i in range(seed_end, len(values)):
        current = values[i]
        if np.isnan(current):
            out[i] = previous
            continue
        previous = previous + alpha * (current - previous)
        out[i] = previous
    return pd.Series(out, index=series.index, name=series.name)


def true_range(
    high: pd.Series[float], low: pd.Series[float], close: pd.Series[float]
) -> pd.Series[float]:
    """Wilder's True Range: max(h-l, |h-prev_close|, |l-prev_close|).

    Uses ``shift(+1)`` for the previous close — the only legal shift direction.
    """
    previous_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    result: pd.Series[float] = ranges.max(axis=1)
    result.name = "true_range"
    return result


def rolling_percentile_rank(series: pd.Series[float], window: int) -> pd.Series[float]:
    """Fraction of the trailing ``window`` values at or below the current one.

    Rolling rather than global: a global percentile would rank today's ATR
    against volatility that has not happened yet.
    """
    if window <= 1:
        raise ValueError(f"window must exceed 1, got {window}")

    def _rank(values: np.ndarray) -> float:
        if np.isnan(values[-1]):
            return np.nan
        valid = values[~np.isnan(values)]
        if valid.size == 0:
            return np.nan
        return float(np.mean(valid <= values[-1]))

    return series.rolling(window, min_periods=window).apply(_rank, raw=True)


def rolling_zscore(series: pd.Series[float], window: int) -> pd.Series[float]:
    """(x - rolling mean) / rolling stdev, with a zero-variance guard."""
    if window <= 1:
        raise ValueError(f"window must exceed 1, got {window}")
    mean = series.rolling(window, min_periods=window).mean()
    # ddof=0: this is the population stdev of the window we actually observed,
    # not an estimate of a wider population.
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return safe_divide(series - mean, std)


def safe_divide(numerator: pd.Series[float], denominator: pd.Series[float]) -> pd.Series[float]:
    """Element-wise division where a zero denominator yields NaN, not inf.

    An inf propagates silently through later arithmetic and ends up as a
    plausible-looking feature value; NaN is loud and is handled explicitly.
    """
    safe = denominator.replace(0.0, np.nan)
    return numerator / safe


def rolling_slope(series: pd.Series[float], window: int) -> pd.Series[float]:
    """Least-squares slope over a trailing window, in units per bar.

    Closed form rather than ``np.polyfit`` per window: the x values are always
    ``0..window-1``, so the denominator is constant and this is ~100x faster
    over a multi-year series.
    """
    if window <= 1:
        raise ValueError(f"window must exceed 1, got {window}")
    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    x_centred = x - x_mean
    denominator = float((x_centred**2).sum())

    def _slope(values: np.ndarray) -> float:
        if np.isnan(values).any():
            return np.nan
        return float((x_centred * (values - values.mean())).sum() / denominator)

    return series.rolling(window, min_periods=window).apply(_slope, raw=True)


def crossover(fast: pd.Series[float], slow: pd.Series[float]) -> pd.Series[int]:
    """+1 where fast crosses above slow, -1 where it crosses below, else 0.

    Compares the current bar against the previous one, so the cross is reported
    on the bar that completes it — never on the bar before.
    """
    difference = fast - slow
    previous = difference.shift(1)
    crossed_up = (difference > 0) & (previous <= 0)
    crossed_down = (difference < 0) & (previous >= 0)
    result = pd.Series(0, index=fast.index, dtype="int64")
    result[crossed_up] = 1
    result[crossed_down] = -1
    # Where either side is unknown, the state is unknown rather than "no cross".
    result[difference.isna() | previous.isna()] = 0
    return result


def confirmed_pivots(series: pd.Series[float], window: int, *, kind: str) -> pd.Series[bool]:
    """Local extrema, marked only once they can legitimately be known.

    A pivot high at bar *t* requires the ``window`` bars on *both* sides to be
    lower — which is not knowable until bar ``t + window``. This returns a mask
    aligned to the *confirmation* bar, so acting on it never uses the future.
    The caller recovers the pivot's own location by looking back ``window`` bars.
    """
    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")
    if kind not in {"high", "low"}:
        raise ValueError(f"kind must be 'high' or 'low', got {kind!r}")

    values = series.to_numpy(dtype=np.float64)
    n = len(values)
    confirmed = np.zeros(n, dtype=bool)
    span = 2 * window + 1
    if n < span:
        return cast("pd.Series[bool]", pd.Series(confirmed, index=series.index))

    for centre in range(window, n - window):
        left = values[centre - window : centre]
        right = values[centre + 1 : centre + window + 1]
        centre_value = values[centre]
        if np.isnan(centre_value) or np.isnan(left).any() or np.isnan(right).any():
            continue
        if kind == "high":
            is_pivot = bool((left < centre_value).all() and (right < centre_value).all())
        else:
            is_pivot = bool((left > centre_value).all() and (right > centre_value).all())
        if is_pivot:
            # Report at the bar where the pivot becomes knowable.
            confirmed[centre + window] = True
    return cast("pd.Series[bool]", pd.Series(confirmed, index=series.index))
