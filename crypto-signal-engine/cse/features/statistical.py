"""Statistical and mean-reversion features.

Rolling price z-score, Hurst exponent, Ornstein-Uhlenbeck half-life, rolling
ADF stationarity, and a BTC-beta-break measure for alts.

These decide *which family of signal is allowed to fire* in Phase 5: trend
signals in trending regimes, mean-reversion signals in mean-reverting ones.
Running both blindly at once is how a strategy ends up long a breakout and
short the same move on the same bar.

Every estimate is computed over a trailing window. Fitting any of these on the
full sample and applying the result historically would be look-ahead bias of
the most flattering kind.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

from cse.config import StatisticalFeatureConfig
from cse.features.base import log_returns, rolling_zscore, safe_divide

TREND_PERSISTENT = 1
MEAN_REVERTING = -1
RANDOM_WALK = 0


def hurst_exponent(values: np.ndarray, min_lag: int, max_lag: int) -> float:
    """Hurst exponent of one window, via the lagged-variance method.

    For each lag tau, the standard deviation of ``x[t+tau] - x[t]`` scales as
    ``tau**H``. A log-log regression of that relationship gives H:

    * ``H > 0.5`` trending / persistent,
    * ``H = 0.5`` random walk,
    * ``H < 0.5`` mean-reverting.

    Operates on log prices, so the result is scale-free.
    """
    if np.isnan(values).any() or values.size <= max_lag:
        return float("nan")
    lags = np.arange(min_lag, max_lag + 1)
    deviations = np.empty(lags.size, dtype=np.float64)
    for index, lag in enumerate(lags):
        differences = values[lag:] - values[:-lag]
        deviations[index] = np.std(differences)
    # A perfectly flat window has zero variation at every lag; H is undefined.
    if np.any(deviations <= 0.0):
        return float("nan")
    slope, _ = np.polyfit(np.log(lags), np.log(deviations), 1)
    return float(slope)


@lru_cache(maxsize=16)
def hurst_null_distribution(
    window: int, min_lag: int, max_lag: int, samples: int, seed: int
) -> tuple[float, float]:
    """Where this estimator lands on data that is *known* to be a random walk.

    Returns ``(median, standard_deviation)``.

    This exists because the textbook threshold is wrong in practice. The
    lagged-variance Hurst estimator is downward-biased in small samples, so
    testing ``H < 0.5`` does not test "is this mean-reverting?" — it mostly
    tests "is the window short?". Measured on independent-increment random
    walks (true H = 0.5 by construction):

        window  128 -> median 0.403, sd 0.139, P(H < 0.5) = 0.76
        window  256 -> median 0.461, sd 0.086, P(H < 0.5) = 0.65
        window 1024 -> median 0.490, sd 0.041, P(H < 0.5) = 0.60

    At the configured 256-bar window, a naive test therefore calls a pure random
    walk "mean-reverting" about two thirds of the time, and the sd of 0.086
    means the label flips on noise from bar to bar. Regime gating built on that
    fires essentially at random.

    Simulating the null for whatever window is actually configured keeps the
    calibration correct when someone changes ``hurst_window`` — a hard-coded
    0.461 would silently go stale.
    """
    rng = np.random.default_rng(seed)
    estimates: list[float] = []
    for _ in range(samples):
        walk = np.log(100.0 + np.cumsum(rng.standard_normal(window)) * 0.01)
        value = hurst_exponent(walk, min_lag, max_lag)
        if np.isfinite(value):
            estimates.append(value)
    if not estimates:  # pragma: no cover - only if the estimator is broken
        return 0.5, 0.0
    values = np.asarray(estimates, dtype=np.float64)
    return float(np.median(values)), float(values.std(ddof=0))


def classify_hurst_regime(
    hurst: pd.Series[float], config: StatisticalFeatureConfig
) -> pd.Series[int]:
    """Label each bar trending / mean-reverting / random walk.

    Classified only when H is ``hurst_deadband_sds`` standard deviations away
    from the estimator's own null centre (see
    :func:`hurst_null_distribution`). Everything inside that band is
    ``RANDOM_WALK``, where Phase 5 lets neither signal family fire — which is
    the correct behaviour when the data does not actually support a call.
    """
    centre, spread = hurst_null_distribution(
        config.hurst_window,
        config.hurst_min_lag,
        config.hurst_max_lag,
        config.hurst_null_samples,
        config.hurst_null_seed,
    )
    margin = config.hurst_deadband_sds * spread

    regime = pd.Series(RANDOM_WALK, index=hurst.index, dtype="int64")
    regime[hurst < centre - margin] = MEAN_REVERTING
    regime[hurst > centre + margin] = TREND_PERSISTENT
    regime[hurst.isna()] = RANDOM_WALK
    return regime


def rolling_hurst(
    close: pd.Series[float], window: int, min_lag: int, max_lag: int
) -> pd.Series[float]:
    """Hurst exponent over a trailing window, computed on log prices."""
    log_price = np.log(close.to_numpy(dtype=np.float64))
    n = len(log_price)
    out = np.full(n, np.nan, dtype=np.float64)
    for end in range(window - 1, n):
        out[end] = hurst_exponent(log_price[end - window + 1 : end + 1], min_lag, max_lag)
    return pd.Series(out, index=close.index, name="hurst")


def ou_half_life(values: np.ndarray, max_bars: float) -> float:
    """Half-life of mean reversion from an Ornstein-Uhlenbeck fit.

    Discretely, OU is the AR(1) regression ``dy_t = a + b * y_{t-1} + e``. The
    half-life is ``-ln(2) / ln(1 + b)``.

    ``b >= 0`` means the series is not reverting at all — the "half-life" is
    infinite. Returning ``max_bars`` rather than ``inf`` keeps the feature
    finite and comparable; an inf would poison every downstream mean/scaler.
    """
    if np.isnan(values).any() or values.size < 3:
        return float("nan")
    lagged = values[:-1]
    delta = np.diff(values)
    centred = lagged - lagged.mean()
    denominator = float((centred**2).sum())
    if denominator <= 0.0:
        return float("nan")
    b = float((centred * (delta - delta.mean())).sum() / denominator)
    if b >= 0.0 or (1.0 + b) <= 0.0:
        return max_bars
    half_life = -np.log(2.0) / np.log(1.0 + b)
    return float(min(half_life, max_bars))


def rolling_half_life(close: pd.Series[float], window: int, max_bars: float) -> pd.Series[float]:
    """OU half-life over a trailing window, in bars."""
    log_price = np.log(close.to_numpy(dtype=np.float64))
    n = len(log_price)
    out = np.full(n, np.nan, dtype=np.float64)
    for end in range(window - 1, n):
        out[end] = ou_half_life(log_price[end - window + 1 : end + 1], max_bars)
    return pd.Series(out, index=close.index, name="half_life")


def rolling_adf(close: pd.Series[float], window: int, stride: int) -> pd.DataFrame:
    """Rolling Augmented Dickey-Fuller statistic and p-value.

    ADF is expensive, so it is recomputed every ``stride`` bars and the last
    value carried forward. Forward-filling a *past* estimate is legitimate — the
    value was genuinely knowable at the bar it was computed on. Interpolating,
    or back-filling, would not be.
    """
    log_price = np.log(close.to_numpy(dtype=np.float64))
    n = len(log_price)
    statistic = np.full(n, np.nan, dtype=np.float64)
    p_value = np.full(n, np.nan, dtype=np.float64)

    for end in range(window - 1, n, stride):
        segment = log_price[end - window + 1 : end + 1]
        if np.isnan(segment).any() or np.allclose(segment, segment[0]):
            continue
        try:
            result = adfuller(segment, autolag=None, maxlag=1, regression="c")
        except (ValueError, np.linalg.LinAlgError):
            continue
        statistic[end] = float(result[0])
        p_value[end] = float(result[1])

    frame = pd.DataFrame({"adf_statistic": statistic, "adf_pvalue": p_value}, index=close.index)
    # Carry the last computed estimate forward only (never backward).
    return frame.ffill()


def rolling_beta_break(
    close: pd.Series[float],
    reference_close: pd.Series[float],
    window: int,
    threshold: float,
) -> pd.DataFrame:
    """Deviation of an alt from its usual beta to the reference asset.

    Fits ``r_alt = alpha + beta * r_ref`` over a trailing window, then measures
    the latest residual in standard deviations of that window's residuals. A
    large value means the alt is moving against the relationship it has held
    recently — a decoupling, which is often where alt-specific opportunity and
    alt-specific risk both live.
    """
    alt_returns = log_returns(close).to_numpy(dtype=np.float64)
    ref_returns = log_returns(reference_close).to_numpy(dtype=np.float64)
    n = len(alt_returns)

    beta = np.full(n, np.nan, dtype=np.float64)
    residual_z = np.full(n, np.nan, dtype=np.float64)

    for end in range(window, n):
        start = end - window + 1
        y = alt_returns[start : end + 1]
        x = ref_returns[start : end + 1]
        if np.isnan(y).any() or np.isnan(x).any():
            continue
        x_centred = x - x.mean()
        denominator = float((x_centred**2).sum())
        if denominator <= 0.0:
            continue
        slope = float((x_centred * (y - y.mean())).sum() / denominator)
        intercept = float(y.mean() - slope * x.mean())
        residuals = y - (intercept + slope * x)
        spread = float(residuals.std(ddof=0))
        beta[end] = slope
        if spread > 0.0:
            residual_z[end] = float(residuals[-1] / spread)

    frame = pd.DataFrame({"btc_beta": beta, "btc_residual_zscore": residual_z}, index=close.index)
    broken = frame["btc_residual_zscore"].abs() >= threshold
    frame["btc_beta_break"] = broken.astype("int64")
    frame.loc[frame["btc_residual_zscore"].isna(), "btc_beta_break"] = 0
    return frame


def compute(
    frame: pd.DataFrame,
    config: StatisticalFeatureConfig,
    *,
    reference_close: pd.Series[float] | None = None,
) -> pd.DataFrame:
    """All statistical features for a canonical candle frame.

    ``reference_close`` is the market-factor series (BTC) aligned to this
    frame's index. Omit it for the reference asset itself, whose beta to itself
    is trivially 1 and carries no information.
    """
    close = frame["close"]
    out = pd.DataFrame(index=frame.index)

    out["price_zscore"] = rolling_zscore(close, config.zscore_window)

    hurst = rolling_hurst(close, config.hurst_window, config.hurst_min_lag, config.hurst_max_lag)
    out["hurst"] = hurst

    out["hurst_regime"] = classify_hurst_regime(hurst, config)
    out["hurst_known"] = (~hurst.isna()).astype("int64")

    out["half_life"] = rolling_half_life(close, config.half_life_window, config.half_life_max_bars)
    out = pd.concat([out, rolling_adf(close, config.adf_window, config.adf_stride)], axis=1)

    if reference_close is not None:
        out = pd.concat(
            [
                out,
                rolling_beta_break(
                    close, reference_close, config.beta_window, config.beta_break_zscore
                ),
            ],
            axis=1,
        )

    # Distance from the rolling mean in units of that window's own volatility;
    # the raw input to any mean-reversion entry.
    mean = close.rolling(config.zscore_window, min_periods=config.zscore_window).mean()
    out["distance_from_mean_pct"] = safe_divide(close - mean, mean)
    return out
