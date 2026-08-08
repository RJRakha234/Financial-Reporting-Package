"""Open interest, and the contract cycle that contaminates it.

Every test here plants the specific artifact that real NSE futures data
contains. The numbers in the docstrings are measured from seven years of
SBIN-FUT, not invented: OI printing exactly zero on expiry day, a +1738%
jump the session after, and 84 rolls in 1,735 sessions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nifty50.features.open_interest import (
    Positioning,
    basis,
    compute_open_interest_features,
    detect_contract_cycles,
    oi_change,
    oi_cycle_matched_zscore,
    oi_zscore,
    positioning_state,
)

SESSIONS = 20


def cycles_of(
    count: int = 6, length: int = SESSIONS, *, base: float = 1_000_000.0,
    drift: float = 0.0, seed: int = 0,
) -> pd.Series:
    """OI that builds through each cycle then collapses at expiry, as real data does.

    ``drift`` defaults to zero. An earlier version ramped each cycle's peak by
    5%, which made the cycle-matched z-score positive everywhere -- correctly,
    since the current cycle really was always larger than its predecessors.
    That is a property of the fixture, not of the estimator, and it made the
    centring test assert something false.
    """
    rng = np.random.default_rng(seed)
    values: list[float] = []
    for cycle in range(count):
        peak = base * (1.0 + drift * cycle) * float(rng.uniform(0.95, 1.05))
        for day in range(length):
            fraction = day / (length - 1)
            # rises for the first ~70% of the cycle, bleeds out into expiry
            level = peak * (0.5 + 0.5 * fraction) if fraction < 0.7 else peak * (1.0 - fraction)
            values.append(max(level, 1.0))
    index = pd.date_range("2024-01-01", periods=len(values), freq="B", tz="Asia/Kolkata")
    return pd.Series(values, index=index, name="open_interest")


class TestRollDetection:
    def test_each_expiry_produces_exactly_one_roll(self) -> None:
        cycle = detect_contract_cycles(cycles_of(count=6))
        assert len(cycle.roll_dates) == 5  # 6 cycles = 5 transitions

    def test_a_zero_print_on_expiry_does_not_double_count(self) -> None:
        """Real SBIN data printed OI of exactly 0 on 2019-08-28.

        That makes the ratio infinite and trips the threshold twice -- once
        leaving zero, once establishing the new contract -- which created a
        one-session "cycle" that poisoned every cycle-grouped statistic.
        """
        oi = cycles_of(count=3).copy()
        boundary = SESSIONS - 1
        oi.iloc[boundary] = 0.0            # expiry-day zero print
        oi.iloc[boundary + 1] = 200_000.0  # stub
        cycle = detect_contract_cycles(oi)
        assert len(cycle.roll_dates) == 2
        lengths = cycle.session_in_cycle.groupby(cycle.cycle_id).max() + 1
        assert int(lengths.min()) > 1  # no phantom one-session cycle

    def test_a_gentle_rise_is_not_a_roll(self) -> None:
        steady = pd.Series(
            np.linspace(1e6, 1.5e6, 200),
            index=pd.date_range("2024-01-01", periods=200, freq="B", tz="Asia/Kolkata"),
        )
        assert len(detect_contract_cycles(steady).roll_dates) == 0

    def test_sessions_to_roll_counts_down(self) -> None:
        cycle = detect_contract_cycles(cycles_of(count=4))
        first = cycle.sessions_to_roll.iloc[:SESSIONS]
        assert first.iloc[0] > first.iloc[-1]
        assert int(first.iloc[-1]) == 0


class TestCycleSafeChanges:
    def test_a_change_spanning_a_roll_is_nan_not_enormous(self) -> None:
        """The whole point. A naive 5-day change read -80.6% at the 5th
        percentile on real data -- a contract switch, not mass unwinding."""
        oi = cycles_of(count=4)
        cycle = detect_contract_cycles(oi)
        naive = oi.pct_change(5)
        safe = oi_change(oi, cycle, periods=5)

        crosses = cycle.cycle_id != cycle.cycle_id.shift(5)
        assert safe[crosses].isna().all()
        assert naive[crosses].notna().any()   # the naive version happily reports
        assert safe.abs().max() < naive.abs().max()

    def test_within_a_cycle_the_change_is_unchanged(self) -> None:
        oi = cycles_of(count=4)
        cycle = detect_contract_cycles(oi)
        safe = oi_change(oi, cycle, periods=1)
        inside = cycle.session_in_cycle > 2
        assert safe[inside].notna().all()

    def test_zero_periods_is_refused(self) -> None:
        oi = cycles_of(count=2)
        with pytest.raises(ValueError, match="at least 1"):
            oi_change(oi, detect_contract_cycles(oi), periods=0)


class TestZScores:
    def test_a_window_longer_than_half_the_cycle_is_refused(self) -> None:
        """A 20-session window on a 20-session cycle completes only at expiry.

        Run that way the score was negative essentially everywhere -- median
        -2.93, maximum +0.8. It measured the roll, not positioning.
        """
        oi = cycles_of(count=6)
        cycle = detect_contract_cycles(oi)
        with pytest.raises(ValueError, match="half the median cycle length"):
            oi_zscore(oi, cycle, window=20)

    def test_the_cycle_matched_score_is_centred(self) -> None:
        oi = cycles_of(count=10)
        cycle = detect_contract_cycles(oi)
        scores = oi_cycle_matched_zscore(oi, cycle).dropna()
        # Comparing day 7 to prior day 7s removes the within-cycle shape, so
        # the distribution sits around zero instead of drifting with the cycle.
        # Measured on seven years of real SBIN-FUT: median -0.12, mean +0.06,
        # against -1.28 / -0.73 for the trailing-window version.
        assert abs(float(scores.median())) < 1.0
        assert len(scores) > 50

    def test_a_trending_series_scores_positive_and_that_is_correct(self) -> None:
        """Sanity check on the check: real growth SHOULD register."""
        oi = cycles_of(count=10, drift=0.10)
        scores = oi_cycle_matched_zscore(oi, detect_contract_cycles(oi)).dropna()
        assert float(scores.median()) > 0.5

    def test_the_cycle_matched_score_needs_prior_cycles(self) -> None:
        oi = cycles_of(count=10)
        cycle = detect_contract_cycles(oi)
        scores = oi_cycle_matched_zscore(oi, cycle)
        # The first cycle has nothing to be scored against.
        assert scores[cycle.cycle_id == 0].isna().all()

    def test_too_few_lookback_cycles_is_refused(self) -> None:
        oi = cycles_of(count=6)
        with pytest.raises(ValueError, match="at least two"):
            oi_cycle_matched_zscore(oi, detect_contract_cycles(oi), lookback_cycles=1)


class TestPositioning:
    def test_the_four_quadrants(self) -> None:
        index = pd.date_range("2024-01-01", periods=5, freq="B", tz="Asia/Kolkata")
        close = pd.Series([100.0, 101.0, 100.0, 101.0, 100.0], index=index)
        oi = pd.Series([1e6, 1.1e6, 1.2e6, 1.1e6, 1.0e6], index=index)
        cycle = detect_contract_cycles(oi)
        state = positioning_state(close, oi, cycle, exclude_roll_window=0)
        assert state.iloc[1] == Positioning.LONG_BUILDUP     # price up,   OI up
        assert state.iloc[2] == Positioning.SHORT_BUILDUP    # price down, OI up
        assert state.iloc[3] == Positioning.SHORT_COVERING   # price up,   OI down
        assert state.iloc[4] == Positioning.LONG_UNWINDING   # price down, OI down

    def test_the_pre_expiry_window_is_blanked(self) -> None:
        """OI falls before expiry because holders roll, not because they turned
        bearish. Left in, the quadrant reads 'long unwinding' every month."""
        oi = cycles_of(count=4)
        close = pd.Series(
            np.linspace(100.0, 120.0, len(oi)), index=oi.index
        )
        cycle = detect_contract_cycles(oi)
        state = positioning_state(close, oi, cycle, exclude_roll_window=5)
        near = cycle.sessions_to_roll < 5
        assert (state[near.to_numpy()] == Positioning.UNDEFINED).all()


class TestBasis:
    def test_a_premium_is_positive(self) -> None:
        index = pd.date_range("2024-01-01", periods=3, freq="B", tz="Asia/Kolkata")
        futures = pd.Series([1003.0, 1004.0, 1002.0], index=index)
        spot = pd.Series([1000.0, 1000.0, 1000.0], index=index)
        assert basis(futures, spot).iloc[0] == pytest.approx(0.003)

    def test_a_zero_spot_does_not_divide(self) -> None:
        index = pd.date_range("2024-01-01", periods=2, freq="B", tz="Asia/Kolkata")
        futures = pd.Series([100.0, 100.0], index=index)
        spot = pd.Series([0.0, 100.0], index=index)
        assert np.isnan(basis(futures, spot).iloc[0])


class TestFeatureFrame:
    def test_it_produces_every_column(self) -> None:
        oi = cycles_of(count=8)
        futures = pd.DataFrame(
            {"close": np.linspace(100.0, 130.0, len(oi)), "open_interest": oi.to_numpy()},
            index=oi.index,
        )
        spot = pd.Series(np.linspace(99.8, 129.6, len(oi)), index=oi.index)
        frame = compute_open_interest_features(futures, spot_close=spot)
        for column in (
            "oi_session_in_cycle", "oi_sessions_to_roll", "oi_change_1",
            "oi_change_5", "oi_zscore", "oi_cycle_matched_zscore", "basis",
        ):
            assert column in frame.columns
        quadrants = [c for c in frame.columns if c.startswith("pos_")]
        assert len(quadrants) == 4
        # Quadrants are mutually exclusive.
        assert frame[quadrants].sum(axis=1).max() <= 1.0

    def test_a_frame_without_open_interest_is_refused(self) -> None:
        index = pd.date_range("2024-01-01", periods=3, freq="B", tz="Asia/Kolkata")
        futures = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index)
        with pytest.raises(ValueError, match="open_interest"):
            compute_open_interest_features(futures)
