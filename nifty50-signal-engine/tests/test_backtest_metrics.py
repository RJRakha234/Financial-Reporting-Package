"""Performance metrics against analytically known answers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nifty50.backtest.metrics import (
    annualised_return,
    calmar_ratio,
    deflated_sharpe,
    max_drawdown,
    monte_carlo_drawdown,
    sharpe_ratio,
    sortino_ratio,
    summarise,
)

SESSIONS_PER_YEAR = 250.0


def curve(values: list[float]) -> pd.Series:
    return pd.Series(
        values, index=pd.date_range("2025-01-01", periods=len(values), freq="D", tz="Asia/Kolkata")
    )


class TestDrawdown:
    def test_a_monotonic_rise_has_no_drawdown(self) -> None:
        result = max_drawdown(curve([100.0, 110.0, 120.0, 130.0]))
        assert result.max_drawdown == pytest.approx(0.0)
        assert result.peak_date is None

    def test_depth_dates_and_recovery(self) -> None:
        # Peak 120 at index 2, trough 60 at index 4 (-50%), recovers at index 6.
        result = max_drawdown(curve([100.0, 110.0, 120.0, 90.0, 60.0, 100.0, 125.0]))
        assert result.max_drawdown == pytest.approx(-0.5)
        assert result.peak_date == curve([0.0] * 7).index[2]
        assert result.trough_date == curve([0.0] * 7).index[4]
        assert result.recovered
        assert result.recovery_date == curve([0.0] * 7).index[6]
        assert result.duration_days == 4

    def test_an_unrecovered_drawdown_is_measured_to_the_end(self) -> None:
        result = max_drawdown(curve([100.0, 120.0, 60.0, 70.0]))
        assert result.max_drawdown == pytest.approx(-0.5)
        assert not result.recovered
        assert result.recovery_date is None


class TestRatios:
    def test_sharpe_of_a_constant_return_stream_is_undefined(self) -> None:
        # Zero variance: the ratio has no denominator.
        steady = pd.Series([0.001] * 100)
        assert np.isnan(sharpe_ratio(steady, periods_per_year=SESSIONS_PER_YEAR))

    def test_sharpe_matches_the_definition(self) -> None:
        rng = np.random.default_rng(3)
        returns = pd.Series(rng.normal(loc=0.0005, scale=0.01, size=1000))
        expected = returns.mean() / returns.std(ddof=1) * np.sqrt(SESSIONS_PER_YEAR)
        assert sharpe_ratio(returns, periods_per_year=SESSIONS_PER_YEAR) == pytest.approx(expected)

    def test_a_risk_free_rate_lowers_sharpe(self) -> None:
        rng = np.random.default_rng(4)
        returns = pd.Series(rng.normal(loc=0.0005, scale=0.01, size=1000))
        zero = sharpe_ratio(returns, periods_per_year=SESSIONS_PER_YEAR)
        realistic = sharpe_ratio(returns, periods_per_year=SESSIONS_PER_YEAR, risk_free_rate=0.06)
        # Indian overnight rates have been 4-7%; assuming zero flatters the ratio.
        assert realistic < zero

    def test_sortino_ignores_upside_volatility(self) -> None:
        # Same mean, but one series is volatile only to the upside.
        symmetric = pd.Series([0.01, -0.01] * 100)
        upside = pd.Series([0.03, -0.01] * 100)
        assert sortino_ratio(upside, periods_per_year=SESSIONS_PER_YEAR) > sortino_ratio(
            symmetric, periods_per_year=SESSIONS_PER_YEAR
        )

    def test_sortino_exceeds_sharpe_when_losses_are_small(self) -> None:
        returns = pd.Series([0.05, 0.05, -0.005] * 60)
        assert sortino_ratio(returns, periods_per_year=SESSIONS_PER_YEAR) > sharpe_ratio(
            returns, periods_per_year=SESSIONS_PER_YEAR
        )

    def test_calmar(self) -> None:
        assert calmar_ratio(0.20, -0.10) == pytest.approx(2.0)
        assert np.isnan(calmar_ratio(0.20, 0.0))

    def test_annualised_return_compounds(self) -> None:
        # Doubling over exactly one year of sessions.
        equity = curve([100.0] + [100.0] * (int(SESSIONS_PER_YEAR) - 1) + [200.0])
        equity.iloc[-1] = 200.0
        result = annualised_return(equity, periods_per_year=SESSIONS_PER_YEAR)
        assert result == pytest.approx(1.0, rel=0.01)

    def test_annualisation_depends_on_the_bar_size(self) -> None:
        rng = np.random.default_rng(5)
        returns = pd.Series(rng.normal(loc=0.0005, scale=0.01, size=1000))
        daily = sharpe_ratio(returns, periods_per_year=250)
        intraday = sharpe_ratio(returns, periods_per_year=250 * 25)
        # Getting periods_per_year wrong rescales Sharpe by its square root.
        assert intraday == pytest.approx(daily * 5.0)


class TestDeflatedSharpe:
    def test_a_single_trial_is_not_deflated(self) -> None:
        assert deflated_sharpe(1.5, trials=1, sample_size=1000) == pytest.approx(1.5)

    def test_more_trials_means_a_bigger_haircut(self) -> None:
        few = deflated_sharpe(1.5, trials=10, sample_size=1000)
        many = deflated_sharpe(1.5, trials=1000, sample_size=1000)
        assert few < 1.5
        assert many < few

    def test_a_longer_sample_reduces_the_haircut(self) -> None:
        short = deflated_sharpe(1.5, trials=100, sample_size=250)
        long = deflated_sharpe(1.5, trials=100, sample_size=5000)
        assert long > short


class TestMonteCarlo:
    def test_reshuffling_explores_worse_paths_than_the_one_that_happened(self) -> None:
        # Same trades, same total PnL — only the order changes.
        pnls = [500.0] * 20 + [-400.0] * 20
        result = monte_carlo_drawdown(pnls, starting_equity=100_000.0, simulations=500, seed=1)
        assert result["worst"] <= result["p99"] <= result["p95"] <= result["median"] <= 0
        # A run of losses is possible, so the tail is materially worse than the median.
        assert result["worst"] < result["median"]

    def test_an_empty_trade_list_returns_nothing(self) -> None:
        assert monte_carlo_drawdown([], starting_equity=100_000.0) == {}


class TestSummarise:
    def test_costs_eating_the_edge_is_surfaced_not_buried(self) -> None:
        equity = curve([100_000.0, 101_000.0, 99_000.0])
        report = summarise(
            equity,
            [200.0, -150.0],
            periods_per_year=SESSIONS_PER_YEAR,
            gross_pnl=5_000.0,
            total_costs=6_000.0,
            exposure_time=0.5,
            turnover=1_000_000.0,
        )
        assert report.costs_ate_the_edge
        assert report.net_pnl == pytest.approx(-1_000.0)
        assert report.costs_as_pct_of_gross == pytest.approx(1.2)
        assert "PROFITABLE GROSS, LOSS-MAKING NET" in report.describe()

    def test_a_thin_sample_is_flagged(self) -> None:
        equity = curve([100_000.0, 101_000.0])
        report = summarise(
            equity,
            [100.0, 200.0],
            periods_per_year=SESSIONS_PER_YEAR,
            gross_pnl=300.0,
            total_costs=10.0,
            exposure_time=0.1,
            turnover=1000.0,
        )
        assert any("not meaningful" in note for note in report.notes)

    def test_win_rate_profit_factor_and_expectancy(self) -> None:
        equity = curve([100_000.0, 100_500.0])
        report = summarise(
            equity,
            [300.0, 300.0, -200.0, -200.0],
            periods_per_year=SESSIONS_PER_YEAR,
            gross_pnl=200.0,
            total_costs=0.0,
            exposure_time=0.5,
            turnover=1000.0,
        )
        assert report.win_rate == pytest.approx(0.5)
        assert report.profit_factor == pytest.approx(600 / 400)
        assert report.average_win == pytest.approx(300.0)
        assert report.average_loss == pytest.approx(-200.0)
        assert report.expectancy == pytest.approx(50.0)

    def test_an_empty_curve_says_so_rather_than_reporting_zeroes(self) -> None:
        report = summarise(
            pd.Series(dtype="float64"),
            [],
            periods_per_year=SESSIONS_PER_YEAR,
            gross_pnl=0.0,
            total_costs=0.0,
            exposure_time=0.0,
            turnover=0.0,
        )
        assert any("never traded" in note for note in report.notes)
