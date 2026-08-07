"""Statistical structure: is this series trending, mean-reverting, or noise?

The trend layer in :mod:`nifty50.features.trend` answers "which way is it
going". This module answers a prior question — "is direction even the right
thing to ask here" — and that answer decides which of the two signal families
should be trusted on a given symbol at a given time.

Three warnings, stated once, that apply to everything below.

*Rolling estimates of long-memory parameters are noisy.* A Hurst exponent
computed on 250 bars has a standard error large enough that individual readings
mean very little; only the persistent, cross-sectional signal is worth acting
on. The functions here return the estimate, not a confidence interval, so the
decision layer must treat a single reading as weak evidence.

*Every estimator here is backward-looking and every window is trailing.* No
centred windows, no full-sample statistics. That is what makes these usable
online, and it is also why they lag: a regime change shows up gradually as it
walks into the window, never on the bar it happened.

*Non-stationarity is the elephant.* Beta, correlation and half-life are all
estimated as if the window were a stationary sample. It is not. A 250-bar
window spanning a results announcement contains two different processes, and
the estimate is a blend of both that describes neither.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# A rolling long-memory estimate below this many observations is not an
# estimate, it is noise with a decimal point. Callers asking for less get NaN.
_MIN_MEMORY_WINDOW: int = 64

# Half-lives longer than the estimation window are extrapolation: the window
# never observed a reversion that slow, so the number is unsupported by data.
_HALF_LIFE_UNBOUNDED = float("inf")

# Dickey-Fuller 5% critical value, constant-no-trend case, large sample. The
# unit-root t-statistic is not t-distributed — its distribution is skewed well
# left — so the usual -1.96 would reject far too readily. Using the correct
# value is the difference between a mean-reversion feature and a random number
# generator that says "mean-reverting" most of the time.
_DICKEY_FULLER_CRITICAL_5PCT: float = -2.86


def log_returns(close: pd.Series) -> pd.Series:
    """Log returns.

    Log rather than simple returns because everything downstream — variance
    ratios, Hurst, aggregation across horizons — assumes additivity across
    time, which only log returns have.
    """
    # np.log on a Series is typed as Any by pandas-stubs; the annotation
    # re-establishes the type rather than letting Any leak into every caller.
    logged: pd.Series = np.log(close)
    return logged.diff().rename("log_return")


# --------------------------------------------------------------------------
# Trend persistence
# --------------------------------------------------------------------------


def hurst_exponent(close: pd.Series, window: int, *, max_lag: int = 32) -> pd.Series:
    """Rolling Hurst exponent via the first-order structure function.

    ``H > 0.5`` means the series is persistent (a move is more likely to be
    followed by another in the same direction), ``H < 0.5`` antipersistent
    (mean-reverting), ``H = 0.5`` a random walk.

    Estimated by regressing ``log E|y(t+tau) - y(t)|`` on ``log tau`` over
    dyadic lags. This is the structure-function estimator rather than classical
    rescaled range: R/S is badly biased on the few-hundred-observation windows
    that intraday trading forces on us, and the bias is towards ``H > 0.5``,
    which would systematically flatter any trend-following logic built on top.

    Even so: treat a single reading as weak evidence. See the module docstring.
    """
    if window < _MIN_MEMORY_WINDOW:
        raise ValueError(f"hurst window must be at least {_MIN_MEMORY_WINDOW} bars, got {window}")
    lags = _dyadic_lags(max_lag=min(max_lag, window // 4))
    if len(lags) < 3:
        raise ValueError("hurst needs at least three usable lags; widen the window")
    log_lags = np.log(lags.astype("float64"))
    centred_lags = log_lags - log_lags.mean()
    denominator = float((centred_lags**2).sum())
    log_price: pd.Series = np.log(close)

    def _hurst(values: np.ndarray) -> float:
        moments = np.empty(len(lags), dtype="float64")
        for position, lag in enumerate(lags):
            differences = values[lag:] - values[:-lag]
            moments[position] = np.abs(differences).mean()
        if not np.all(moments > 0.0):
            # A perfectly flat stretch has no scaling behaviour to measure.
            return float("nan")
        log_moments = np.log(moments)
        return float((centred_lags * (log_moments - log_moments.mean())).sum() / denominator)

    estimate: pd.Series = log_price.rolling(window=window, min_periods=window).apply(
        _hurst, raw=True
    )
    return estimate.rename("hurst")


def variance_ratio(close: pd.Series, window: int, *, lag: int = 5) -> pd.Series:
    """Lo-MacKinlay variance ratio at ``lag`` horizons, over a trailing window.

    ``VR = Var(q-bar return) / (q * Var(1-bar return))``. Under a random walk
    variance scales linearly with time and ``VR = 1``. ``VR > 1`` indicates
    positive autocorrelation (trending); ``VR < 1`` indicates reversion.

    This is the same question Hurst asks, by a different route with a known
    sampling distribution — see :func:`variance_ratio_zstat`. When the two
    disagree, believe neither.
    """
    if lag < 2:
        raise ValueError("variance ratio lag must be at least 2")
    if window <= lag * 4:
        raise ValueError(f"variance ratio window must exceed 4*lag ({lag * 4}), got {window}")
    prices: pd.Series = np.log(close)
    single = prices.diff()
    aggregated = prices.diff(lag)

    rolling_single = single.rolling(window=window, min_periods=window).var(ddof=1)
    # The q-bar returns overlap, which is the point: overlapping windows use
    # every observation instead of throwing away (q-1)/q of the sample.
    rolling_aggregated = aggregated.rolling(window=window, min_periods=window).var(ddof=1)

    denominator = (float(lag) * rolling_single).replace(0.0, np.nan)
    ratio: pd.Series = rolling_aggregated / denominator
    return ratio.rename(f"variance_ratio_{lag}")


def variance_ratio_zstat(close: pd.Series, window: int, *, lag: int = 5) -> pd.Series:
    """Heteroskedasticity-robust z-statistic for the variance ratio.

    Lo & MacKinlay's ``z2``. The homoskedastic statistic is cheaper but wrong
    for equity returns: volatility clustering alone will reject a random walk
    at the 1% level on data that is one, so the homoskedastic version mostly
    detects GARCH effects and calls them predictability.

    ``|z| > 2`` is roughly the 5% level. Note that the window is chosen by us,
    not by the data, so the usual multiple-comparisons caveat applies with
    force: scan enough symbols and windows and significance appears for free.
    """
    ratio = variance_ratio(close, window, lag=lag)
    prices: pd.Series = np.log(close)
    single = prices.diff()

    def _theta(values: np.ndarray) -> float:
        deviations = values - values.mean()
        squared = deviations**2
        total = squared.sum()
        if total <= 0.0:
            return float("nan")
        accumulated = 0.0
        for j in range(1, lag):
            delta = float((squared[j:] * squared[:-j]).sum()) / float(total**2)
            weight = 2.0 * (lag - j) / lag
            accumulated += (weight**2) * delta
        return accumulated

    theta = single.rolling(window=window, min_periods=window).apply(_theta, raw=True)
    standard_error: pd.Series = np.sqrt(theta.where(theta > 0.0))
    statistic: pd.Series = (ratio - 1.0) / standard_error
    return statistic.rename(f"variance_ratio_z_{lag}")


def autocorrelation(returns: pd.Series, window: int, *, lag: int = 1) -> pd.Series:
    """Rolling autocorrelation of returns at ``lag``.

    Persistently positive lag-1 autocorrelation on a liquid Nifty 50 name is
    more likely a microstructure artefact — stale prices, bid-ask bounce
    asymmetry — than a tradeable effect. Negative lag-1 autocorrelation is the
    signature of bid-ask bounce and is definitely not tradeable at retail
    costs.
    """
    lagged = returns.shift(lag)
    return (
        returns.rolling(window=window, min_periods=window)
        .corr(lagged)
        .rename(f"autocorr_{lag}")
    )


# --------------------------------------------------------------------------
# Mean reversion
# --------------------------------------------------------------------------


def mean_reversion_half_life(close: pd.Series, window: int) -> pd.Series:
    """Half-life of mean reversion in bars, from a rolling AR(1) fit.

    Fits ``dy_t = alpha + beta * y_{t-1}`` and converts the AR coefficient
    ``phi = 1 + beta`` into ``ln(0.5) / ln(phi)``.

    **A point estimate alone is not usable here, and this is not a detail.**
    Run the naive version on a pure random walk and it reports a finite
    half-life of a few dozen bars roughly seven times in ten. That is not the
    estimator finding something; it is the Dickey-Fuller small-sample bias.
    Within any finite window the sample mean acts as an attractor, so OLS finds
    reversion towards it whether or not the process has any. A feature built on
    the raw estimate would tell the decision layer that every large-cap in the
    index is mean-reverting, all the time.

    So the estimate is gated on significance: ``beta`` must be far enough below
    zero to reject a unit root at the 5% level, using the Dickey-Fuller
    critical value rather than the normal one — the t-statistic here does not
    have a t distribution, and using -1.96 would reinstate most of the false
    positives.

    Three distinct return values, and the distinction is load-bearing:

    * ``NaN``   — could not estimate (warm-up, or a degenerate window).
    * ``inf``   — estimated, and the series is not demonstrably mean-reverting.
    * a number  — estimated, significant, and shorter than the window.

    Half-lives beyond the window are ``inf`` too: the window never observed a
    reversion that slow, so a number would be extrapolation.
    """
    log_price: pd.Series = np.log(close)
    lagged = log_price.shift(1)
    change = log_price.diff()

    rolling_lagged = lagged.rolling(window=window, min_periods=window)
    covariance = rolling_lagged.cov(change)
    lagged_variance = rolling_lagged.var(ddof=1)
    beta = covariance / lagged_variance.replace(0.0, np.nan)

    # Standard error of the OLS slope, from the residual variance implied by
    # the correlation. Cheaper than refitting and algebraically identical.
    correlation = rolling_lagged.corr(change)
    change_variance = change.rolling(window=window, min_periods=window).var(ddof=1)
    observations = float(window)
    residual_variance = (
        change_variance * (1.0 - correlation**2) * (observations - 1.0) / (observations - 2.0)
    )
    # ``** 0.5`` rather than ``np.sqrt``: it stays a Series through the
    # type checker, and a negative variance from floating-point error becomes
    # NaN rather than a warning-and-NaN.
    standard_error = (
        residual_variance / ((observations - 1.0) * lagged_variance.replace(0.0, np.nan))
    ) ** 0.5
    t_statistic = beta / standard_error.replace(0.0, np.nan)

    phi = 1.0 + beta
    significant = t_statistic < _DICKEY_FULLER_CRITICAL_5PCT
    reverting = significant & (phi > 0.0) & (phi < 1.0)

    decay: pd.Series = np.log(phi.where(reverting))
    half_life: pd.Series = float(np.log(0.5)) / decay
    result = half_life.where(reverting, _HALF_LIFE_UNBOUNDED)
    result = result.where(result <= float(window), _HALF_LIFE_UNBOUNDED)
    # Warm-up and degenerate windows are NaN, never inf: "no estimate" and
    # "estimated as non-reverting" are different claims.
    return result.where(beta.notna()).rename("half_life_bars")


def distance_from_mean_in_sigma(close: pd.Series, window: int) -> pd.Series:
    """How far price sits from its trailing mean, in trailing standard deviations.

    The raw mean-reversion signal. On its own it is a trap — a stock two sigma
    below its mean is as often repricing as it is oversold — which is exactly
    why it is paired with :func:`mean_reversion_half_life` and
    :func:`hurst_exponent` rather than used alone.
    """
    rolling = close.rolling(window=window, min_periods=window)
    mean = rolling.mean()
    deviation = rolling.std(ddof=0)
    return ((close - mean) / deviation.replace(0.0, np.nan)).rename("distance_from_mean_sigma")


# --------------------------------------------------------------------------
# Index-relative structure
# --------------------------------------------------------------------------


def rolling_beta(returns: pd.Series, index_returns: pd.Series, window: int) -> pd.DataFrame:
    """Rolling OLS of symbol returns on index returns.

    Returns ``beta``, ``alpha`` (per bar, not annualised), ``r_squared`` and
    ``beta_instability`` — the trailing standard deviation of beta itself.

    Beta instability is the column that matters most in practice: a name whose
    beta is wandering is one whose index-relative signal cannot be trusted,
    because the hedge ratio the signal implies is being estimated from a
    relationship that is not holding still.
    """
    aligned_index = index_returns.reindex(returns.index)
    joint = returns.rolling(window=window, min_periods=window)
    index_roll = aligned_index.rolling(window=window, min_periods=window)

    covariance = joint.cov(aligned_index)
    index_variance = index_roll.var(ddof=1)
    beta = covariance / index_variance.replace(0.0, np.nan)
    alpha = joint.mean() - beta * index_roll.mean()

    correlation = joint.corr(aligned_index)
    frame = pd.DataFrame(
        {
            "beta": beta,
            "alpha": alpha,
            "r_squared": correlation**2,
            "index_correlation": correlation,
            "beta_instability": beta.rolling(window=window, min_periods=window).std(ddof=0),
        }
    )
    return frame


def residual_return(
    returns: pd.Series, index_returns: pd.Series, beta: pd.Series, alpha: pd.Series
) -> pd.Series:
    """Idiosyncratic return: what is left after the index move is removed.

    ``beta`` and ``alpha`` must come from a *trailing* fit — the one
    :func:`rolling_beta` produces. Using a fit estimated over the same bar
    being residualised is the classic look-ahead in stat-arb, and it makes
    residuals look far more mean-reverting than they are.
    """
    aligned_index = index_returns.reindex(returns.index)
    return (returns - alpha - beta * aligned_index).rename("residual_return")


def residual_zscore(residuals: pd.Series, window: int) -> pd.Series:
    """Z-score of cumulative idiosyncratic drift over a trailing window.

    This is the pairs-trading signal expressed against the index rather than
    against a single partner: the stock has drifted away from where its beta
    says it should be, and the question is whether that gap closes.

    In the Nifty 50 the honest prior is that most such gaps are news, not
    noise. Earnings, block deals, promoter pledging and index reconstitution
    all produce large residuals that do not revert.
    """
    cumulative = residuals.rolling(window=window, min_periods=window).sum()
    deviation = residuals.rolling(window=window, min_periods=window).std(ddof=0) * np.sqrt(
        float(window)
    )
    ratio: pd.Series = cumulative / deviation.replace(0.0, np.nan)
    return ratio.rename("residual_zscore")


# --------------------------------------------------------------------------
# Return-distribution shape
# --------------------------------------------------------------------------


def distribution_shape(returns: pd.Series, window: int) -> pd.DataFrame:
    """Rolling skewness, excess kurtosis, and volatility-of-volatility.

    Included because position sizing that assumes normality is wrong in a
    direction that costs money. Nifty 50 constituents routinely show excess
    kurtosis above 5 on 15-minute bars, meaning the 1% tail is several times
    fatter than a Gaussian sizing rule expects.
    """
    rolling = returns.rolling(window=window, min_periods=window)
    realised = rolling.std(ddof=1)
    return pd.DataFrame(
        {
            "return_skew": rolling.skew(),
            "return_excess_kurtosis": rolling.kurt(),
            "vol_of_vol": realised.rolling(window=window, min_periods=window).std(ddof=1)
            / realised.replace(0.0, np.nan),
        }
    )


def _dyadic_lags(*, max_lag: int) -> np.ndarray:
    """Powers of two from 2 up to ``max_lag``."""
    lags: list[int] = []
    lag = 2
    while lag <= max_lag:
        lags.append(lag)
        lag *= 2
    return np.array(lags, dtype="int64")
