"""Statistical estimators against processes whose answers are known by construction.

Testing an estimator on real data proves nothing — you cannot check an answer
you do not have. Every test here generates a process with a known parameter
(a random walk has H = 0.5, an OU process has a half-life you chose) and asks
whether the estimator recovers it.

Tolerances are wide, and honestly so. These estimators are noisy on the window
lengths intraday trading permits; a test that passed with tight tolerances
would be testing the seed, not the estimator.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nifty50.features.statistical import (
    autocorrelation,
    distance_from_mean_in_sigma,
    distribution_shape,
    hurst_exponent,
    log_returns,
    mean_reversion_half_life,
    residual_return,
    residual_zscore,
    rolling_beta,
    variance_ratio,
    variance_ratio_zstat,
)

INDEX = pd.date_range("2020-01-01", periods=3000, freq="15min", tz="Asia/Kolkata")


def series(values: np.ndarray) -> pd.Series:
    return pd.Series(values, index=INDEX[: len(values)])


def random_walk(n: int = 3000, *, seed: int = 0, sigma: float = 0.002) -> pd.Series:
    rng = np.random.default_rng(seed)
    return series(100.0 * np.exp(np.cumsum(rng.normal(0.0, sigma, size=n))))


def trending(n: int = 3000, *, seed: int = 1, persistence: float = 0.35) -> pd.Series:
    """Positively autocorrelated returns: a persistent, H > 0.5 process."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0.0, 0.002, size=n)
    returns = np.zeros(n)
    for i in range(1, n):
        returns[i] = persistence * returns[i - 1] + shocks[i]
    return series(100.0 * np.exp(np.cumsum(returns)))


def ornstein_uhlenbeck(n: int = 3000, *, half_life: float, seed: int = 2) -> pd.Series:
    """Log-price mean-reverting to a constant with the requested half-life."""
    phi = 0.5 ** (1.0 / half_life)
    rng = np.random.default_rng(seed)
    log_price = np.zeros(n)
    log_price[0] = np.log(100.0)
    centre = np.log(100.0)
    for i in range(1, n):
        log_price[i] = centre + phi * (log_price[i - 1] - centre) + rng.normal(0.0, 0.002)
    return series(np.exp(log_price))


class TestHurst:
    def test_a_random_walk_scores_about_a_half(self) -> None:
        values = hurst_exponent(random_walk(), window=512).dropna()
        assert values.mean() == pytest.approx(0.5, abs=0.08)

    def test_a_persistent_process_scores_above_a_random_walk(self) -> None:
        walk = hurst_exponent(random_walk(seed=5), window=512).dropna().mean()
        persistent = hurst_exponent(trending(seed=5), window=512).dropna().mean()
        assert persistent > walk

    def test_a_mean_reverting_process_scores_below_a_random_walk(self) -> None:
        walk = hurst_exponent(random_walk(seed=6), window=512).dropna().mean()
        reverting = hurst_exponent(
            ornstein_uhlenbeck(half_life=10.0, seed=6), window=512
        ).dropna().mean()
        assert reverting < walk

    def test_a_window_too_short_to_estimate_is_refused_not_returned(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            hurst_exponent(random_walk(), window=32)

    def test_warm_up_is_nan_not_a_guess(self) -> None:
        values = hurst_exponent(random_walk(), window=512)
        assert values.iloc[:511].isna().all()
        assert pd.notna(values.iloc[511])


class TestVarianceRatio:
    def test_a_random_walk_sits_near_one(self) -> None:
        values = variance_ratio(random_walk(seed=7), window=500, lag=5).dropna()
        assert values.mean() == pytest.approx(1.0, abs=0.15)

    def test_persistence_pushes_the_ratio_above_one(self) -> None:
        values = variance_ratio(trending(seed=8), window=500, lag=5).dropna()
        assert values.mean() > 1.2

    def test_mean_reversion_pushes_the_ratio_below_one(self) -> None:
        values = variance_ratio(
            ornstein_uhlenbeck(half_life=5.0, seed=9), window=500, lag=5
        ).dropna()
        assert values.mean() < 0.8

    def test_the_zstat_does_not_reject_a_random_walk_on_average(self) -> None:
        z = variance_ratio_zstat(random_walk(seed=10), window=500, lag=5).dropna()
        # The whole point of the robust statistic: a genuine random walk should
        # not look significant. Overlapping windows make consecutive z values
        # highly correlated, so this checks the level, not a rejection rate.
        assert abs(z.mean()) < 2.0

    def test_the_zstat_rejects_a_strongly_persistent_process(self) -> None:
        z = variance_ratio_zstat(trending(seed=11, persistence=0.5), window=500, lag=5).dropna()
        assert z.mean() > 2.0

    def test_a_window_that_cannot_support_the_lag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="4\\*lag"):
            variance_ratio(random_walk(), window=20, lag=5)


class TestHalfLife:
    @pytest.mark.parametrize("target", [5.0, 20.0])
    def test_it_recovers_the_half_life_it_was_given(self, target: float) -> None:
        prices = ornstein_uhlenbeck(half_life=target, seed=3)
        estimate = mean_reversion_half_life(prices, window=750).dropna()
        finite = estimate[np.isfinite(estimate)]
        assert len(finite) > 100
        assert finite.median() == pytest.approx(target, rel=0.45)

    def test_a_random_walk_reports_infinity_not_a_number(self) -> None:
        estimate = mean_reversion_half_life(random_walk(seed=4), window=750).dropna()
        # A random walk does not revert. Reporting a large finite half-life
        # would invite the decision layer to wait for a reversion that is not
        # coming; inf says so unambiguously.
        assert np.isinf(estimate).mean() > 0.5

    def test_warm_up_is_nan_which_is_distinct_from_infinity(self) -> None:
        estimate = mean_reversion_half_life(random_walk(), window=750)
        assert estimate.iloc[:749].isna().all()
        assert not np.isinf(estimate.iloc[:749]).any()


class TestAutocorrelation:
    def test_it_recovers_an_ar1_coefficient(self) -> None:
        prices = trending(seed=12, persistence=0.4)
        values = autocorrelation(log_returns(prices), window=500, lag=1).dropna()
        assert values.mean() == pytest.approx(0.4, abs=0.12)

    def test_a_random_walk_has_no_autocorrelation(self) -> None:
        values = autocorrelation(log_returns(random_walk(seed=13)), window=500, lag=1).dropna()
        assert abs(values.mean()) < 0.1


class TestBeta:
    def test_it_recovers_a_constructed_beta(self) -> None:
        rng = np.random.default_rng(14)
        index_returns = series(rng.normal(0.0, 0.004, size=3000))
        noise = series(rng.normal(0.0, 0.001, size=3000))
        symbol_returns = 1.5 * index_returns + noise

        frame = rolling_beta(symbol_returns, index_returns, window=500)
        assert frame["beta"].dropna().mean() == pytest.approx(1.5, abs=0.05)
        # Most of the variance is explained by construction.
        assert frame["r_squared"].dropna().mean() > 0.9

    def test_beta_instability_is_higher_when_beta_moves(self) -> None:
        rng = np.random.default_rng(15)
        index_returns = series(rng.normal(0.0, 0.004, size=3000))
        noise = series(rng.normal(0.0, 0.001, size=3000))

        stable = rolling_beta(1.0 * index_returns + noise, index_returns, window=300)
        shifting_beta = np.concatenate([np.full(1500, 0.5), np.full(1500, 2.0)])
        shifting = rolling_beta(
            series(shifting_beta * index_returns.to_numpy() + noise.to_numpy()),
            index_returns,
            window=300,
        )
        assert (
            shifting["beta_instability"].dropna().mean()
            > stable["beta_instability"].dropna().mean()
        )

    def test_residuals_strip_the_index_move(self) -> None:
        rng = np.random.default_rng(16)
        index_returns = series(rng.normal(0.0, 0.004, size=3000))
        idiosyncratic = series(rng.normal(0.0, 0.001, size=3000))
        symbol_returns = 1.5 * index_returns + idiosyncratic

        frame = rolling_beta(symbol_returns, index_returns, window=500)
        residuals = residual_return(
            symbol_returns, index_returns, frame["beta"], frame["alpha"]
        ).dropna()
        # What is left should look like the idiosyncratic component, not the
        # symbol's raw returns.
        assert residuals.std() == pytest.approx(idiosyncratic.std(), rel=0.2)
        assert abs(residuals.corr(index_returns.reindex(residuals.index))) < 0.15

    def test_residual_zscore_flags_sustained_drift(self) -> None:
        rng = np.random.default_rng(17)
        index_returns = series(rng.normal(0.0, 0.004, size=3000))
        drift = np.zeros(3000)
        drift[2000:2100] = 0.002  # a hundred bars of one-way idiosyncratic move
        symbol_returns = series(index_returns.to_numpy() + drift)

        frame = rolling_beta(symbol_returns, index_returns, window=500)
        residuals = residual_return(symbol_returns, index_returns, frame["beta"], frame["alpha"])
        z = residual_zscore(residuals, window=50)
        assert z.loc[INDEX[2050:2100]].max() > 3.0


class TestDistributionShape:
    def test_it_detects_negative_skew(self) -> None:
        rng = np.random.default_rng(18)
        returns = series(-np.abs(rng.normal(0.0, 0.01, size=3000)) + 0.004)
        assert distribution_shape(returns, window=500)["return_skew"].dropna().mean() < -0.5

    def test_fat_tails_show_up_as_excess_kurtosis(self) -> None:
        rng = np.random.default_rng(19)
        gaussian = series(rng.normal(0.0, 0.01, size=3000))
        heavy = series(rng.standard_t(df=3, size=3000) * 0.005)
        gaussian_kurtosis = (
            distribution_shape(gaussian, window=500)["return_excess_kurtosis"].dropna().mean()
        )
        heavy_kurtosis = (
            distribution_shape(heavy, window=500)["return_excess_kurtosis"].dropna().mean()
        )
        assert abs(gaussian_kurtosis) < 0.5
        assert heavy_kurtosis > 2.0


class TestDistanceFromMean:
    def test_a_flat_series_is_undefined_not_zero(self) -> None:
        flat = series(np.full(500, 100.0))
        # Zero standard deviation: "how many sigma away" has no answer. Zero
        # would read as "exactly at the mean", which is a claim about a
        # distribution that does not exist.
        assert distance_from_mean_in_sigma(flat, window=100).dropna().empty

    def test_a_step_up_registers_as_positive_sigma(self) -> None:
        rng = np.random.default_rng(20)
        values = 100.0 + rng.normal(0.0, 0.5, size=600)
        values[500:] += 5.0
        result = distance_from_mean_in_sigma(series(values), window=100)
        assert result.iloc[505] > 2.0
