"""Cross-sectional features, tested against panels with planted structure.

The correlation and dispersion estimators are checked by constructing a panel
whose true parameter is known by construction, the same discipline used for the
single-series statistics: you cannot verify an estimator against data whose
answer you do not have.

The survivorship test is the one that matters most. A panel assembled from
whichever files exist on disk, rather than from point-in-time membership,
deletes every company dropped from the index for performing badly — and that
mistake does not produce an error, it produces a better backtest.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.features.cross_sectional import (
    Panel,
    average_pairwise_correlation,
    breadth,
    build_panel,
    cross_sectional_rank,
    cross_sectional_zscore,
    demean_by_cross_section,
    dispersion,
    rank_portfolio_weights,
    turnover,
)
from nifty50.trading_calendar import TradingCalendar
from nifty50.universe import Membership, PointInTimeUniverse, Provenance

SYMBOLS = [f"S{index:02d}" for index in range(20)]


def index_of(periods: int = 400) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01 09:15", periods=periods, freq="15min", tz="Asia/Kolkata")


def correlated_frames(
    *, beta: float = 0.9, market_sigma: float = 0.004, idio_sigma: float = 0.002, seed: int = 0
) -> tuple[dict[str, pd.DataFrame], float]:
    """Frames sharing one market factor, plus the true average pairwise correlation."""
    rng = np.random.default_rng(seed)
    stamps = index_of()
    market = rng.normal(0.0, market_sigma, len(stamps))
    frames = {
        symbol: pd.DataFrame(
            {"ret": beta * market + rng.normal(0.0, idio_sigma, len(stamps))}, index=stamps
        )
        for symbol in SYMBOLS
    }
    shared = (beta * market_sigma) ** 2
    true_rho = shared / (shared + idio_sigma**2)
    return frames, true_rho


def full_universe(symbols: list[str] | None = None) -> PointInTimeUniverse:
    names = symbols or SYMBOLS
    return PointInTimeUniverse(
        [Membership(s, dt.date(2020, 1, 1), None, Provenance.NSE_CIRCULAR) for s in names],
        index_name="MINI",
        expected_size=len(names),
    )


def panel_of(frames: dict[str, pd.DataFrame], calendar: TradingCalendar,
             universe: PointInTimeUniverse | None = None) -> Panel:
    return build_panel(frames, "ret", universe or full_universe(), calendar, name="returns")


class TestSurvivorship:
    def test_a_symbol_is_masked_outside_its_membership(
        self, calendar: TradingCalendar
    ) -> None:
        """The defence that matters. A non-member must not be rankable.

        If a company dropped from the index in 2022 still appears in the 2023
        cross-section, the backtest is trading something that was not there —
        and because index deletions skew towards poor performers, the error
        flatters the result rather than hurting it.
        """
        frames, _ = correlated_frames()
        left_early = PointInTimeUniverse(
            [
                Membership("S00", dt.date(2020, 1, 1), dt.date(2023, 12, 31),
                           Provenance.NSE_CIRCULAR),
                *[
                    Membership(s, dt.date(2020, 1, 1), None, Provenance.NSE_CIRCULAR)
                    for s in SYMBOLS[1:]
                ],
            ],
            index_name="MINI",
            expected_size=len(SYMBOLS),
        )
        panel = panel_of(frames, calendar, left_early)
        # The whole sample is 2024, after S00 was removed.
        assert panel.frame["S00"].isna().all()
        assert panel.frame["S01"].notna().any()

    def test_coverage_reports_what_was_actually_available(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        panel = panel_of(frames, calendar)
        assert int(panel.coverage.min()) == len(SYMBOLS)
        assert "20 symbols" in panel.describe()


class TestTransforms:
    def test_ranks_are_normalised_to_the_unit_interval(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        ranked = cross_sectional_rank(panel_of(frames, calendar))
        assert float(ranked.min().min()) > 0.0
        assert float(ranked.max().max()) == pytest.approx(1.0)

    def test_ranks_stay_comparable_when_coverage_drops(
        self, calendar: TradingCalendar
    ) -> None:
        """A raw integer rank silently changes meaning when symbols go missing.

        Rank 12 is "median" out of 20 and "top quartile" out of 14. Percentile
        ranks are immune, which is why they are the default.
        """
        frames, _ = correlated_frames()
        panel = panel_of(frames, calendar)
        thinned = Panel(frame=panel.frame.copy(), name=panel.name)
        thinned.frame.iloc[100:, :6] = np.nan  # six names stop reporting

        ranked = cross_sectional_rank(thinned, min_symbols=10)
        assert float(ranked.iloc[150].max()) == pytest.approx(1.0)
        assert float(ranked.iloc[150].min()) > 0.0

    def test_a_thin_cross_section_is_refused_not_ranked(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        panel = panel_of(frames, calendar)
        thin = Panel(frame=panel.frame.copy(), name=panel.name)
        thin.frame.iloc[200:, 3:] = np.nan  # only three names left
        ranked = cross_sectional_rank(thin, min_symbols=10)
        assert ranked.iloc[250].isna().all()

    def test_zscore_standardises_each_bar(self, calendar: TradingCalendar) -> None:
        frames, _ = correlated_frames()
        scores = cross_sectional_zscore(panel_of(frames, calendar))
        assert float(scores.mean(axis=1).abs().max()) < 1e-9
        assert float(scores.std(axis=1, ddof=0).dropna().mean()) == pytest.approx(1.0, abs=0.01)

    def test_winsorising_stops_one_gap_setting_the_scale(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        panel = panel_of(frames, calendar)
        shocked = Panel(frame=panel.frame.copy(), name=panel.name)
        shocked.frame.iloc[50, 0] = 0.25  # a 25% earnings gap in one name

        raw = cross_sectional_zscore(shocked, winsorise=0.0).iloc[50]
        clipped = cross_sectional_zscore(shocked, winsorise=0.10).iloc[50]
        # Untreated, every other name is squashed toward zero by the outlier.
        assert raw.drop("S00").abs().max() < clipped.drop("S00").abs().max()

    def test_demeaning_removes_the_market_leg_exactly(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        panel = panel_of(frames, calendar)
        residual = demean_by_cross_section(panel)
        # Exact by construction, not estimated: every bar sums to zero.
        assert float(residual.mean(axis=1).abs().max()) < 1e-15
        # And the residual is far less variable than the raw return.
        assert float(residual.std(axis=1).mean()) < float(panel.frame.std(axis=1).mean()) + 1e-12


class TestRegime:
    def test_average_pairwise_correlation_recovers_the_planted_value(
        self, calendar: TradingCalendar
    ) -> None:
        frames, true_rho = correlated_frames()
        estimate = average_pairwise_correlation(panel_of(frames, calendar), window=100).dropna()
        assert float(estimate.median()) == pytest.approx(true_rho, abs=0.06)

    def test_lower_beta_gives_lower_correlation(self, calendar: TradingCalendar) -> None:
        tight, _ = correlated_frames(beta=1.0, idio_sigma=0.001, seed=1)
        loose, _ = correlated_frames(beta=0.3, idio_sigma=0.006, seed=1)
        high = average_pairwise_correlation(panel_of(tight, calendar), window=100).median()
        low = average_pairwise_correlation(panel_of(loose, calendar), window=100).median()
        assert float(high) > float(low)

    def test_dispersion_rises_when_names_decouple(self, calendar: TradingCalendar) -> None:
        together, _ = correlated_frames(beta=1.0, idio_sigma=0.0005, seed=2)
        apart, _ = correlated_frames(beta=0.2, idio_sigma=0.008, seed=2)
        assert float(dispersion(panel_of(apart, calendar)).median()) > float(
            dispersion(panel_of(together, calendar)).median()
        )

    def test_breadth_is_a_fraction(self, calendar: TradingCalendar) -> None:
        frames, _ = correlated_frames()
        values = breadth(panel_of(frames, calendar)).dropna()
        assert values.between(0.0, 1.0).all()


class TestWeights:
    def test_a_long_short_book_is_market_neutral_and_correctly_levered(
        self, calendar: TradingCalendar
    ) -> None:
        """Gross exposure is split ACROSS the legs, not applied to each.

        Treating `gross_exposure=1.0` as "100% long and 100% short" is how a
        backtest labelled market-neutral quietly runs at 2x leverage, and the
        equity curve looks like skill.
        """
        frames, _ = correlated_frames()
        scores = cross_sectional_rank(panel_of(frames, calendar))
        weights = rank_portfolio_weights(scores, long_count=3, short_count=3)

        assert float(weights.sum(axis=1).abs().max()) < 1e-12  # net zero
        assert float(weights.abs().sum(axis=1).max()) == pytest.approx(1.0)
        assert int(weights.ne(0).sum(axis=1).max()) == 6

    def test_a_long_only_book_uses_the_whole_exposure(
        self, calendar: TradingCalendar
    ) -> None:
        frames, _ = correlated_frames()
        scores = cross_sectional_rank(panel_of(frames, calendar))
        weights = rank_portfolio_weights(scores, long_count=5, short_count=0)
        assert float(weights.sum(axis=1).max()) == pytest.approx(1.0)
        assert (weights >= 0.0).all().all()

    def test_the_top_ranked_names_are_the_ones_held(
        self, calendar: TradingCalendar
    ) -> None:
        stamps = index_of(3)
        scores = pd.DataFrame(
            [[0.1, 0.9, 0.5, 0.2]] * 3, index=stamps, columns=["A", "B", "C", "D"]
        )
        weights = rank_portfolio_weights(scores, long_count=1, short_count=1)
        assert weights.iloc[0]["B"] > 0  # highest score
        assert weights.iloc[0]["A"] < 0  # lowest score
        assert weights.iloc[0]["C"] == 0

    def test_a_bar_with_too_few_names_holds_nothing(self) -> None:
        stamps = index_of(2)
        scores = pd.DataFrame(
            [[0.5, np.nan, np.nan, np.nan], [0.1, 0.9, 0.5, 0.2]],
            index=stamps, columns=["A", "B", "C", "D"],
        )
        weights = rank_portfolio_weights(scores, long_count=2, short_count=2)
        assert float(weights.iloc[0].abs().sum()) == 0.0
        assert float(weights.iloc[1].abs().sum()) > 0.0

    def test_turnover_is_one_way_and_counts_the_opening_trade(self) -> None:
        stamps = index_of(3)
        weights = pd.DataFrame(
            [[0.5, 0.5, 0.0], [0.5, 0.5, 0.0], [0.0, 0.5, 0.5]],
            index=stamps, columns=["A", "B", "C"],
        )
        values = turnover(weights)
        assert float(values.iloc[0]) == pytest.approx(0.5)  # opening the book
        assert float(values.iloc[1]) == pytest.approx(0.0)  # unchanged
        assert float(values.iloc[2]) == pytest.approx(0.5)  # swapped A for C

    def test_bad_parameters_are_refused(self) -> None:
        scores = pd.DataFrame([[0.1, 0.2]], index=index_of(1), columns=["A", "B"])
        with pytest.raises(ValueError, match="long_count"):
            rank_portfolio_weights(scores, long_count=0)
        with pytest.raises(ValueError, match="gross_exposure"):
            rank_portfolio_weights(scores, long_count=1, gross_exposure=0.0)
