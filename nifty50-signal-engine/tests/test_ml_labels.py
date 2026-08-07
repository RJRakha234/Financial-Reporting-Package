"""Triple-barrier labelling and the purged splitter.

The splitter tests are the important ones. A purge that silently does nothing
produces folds that look correct — chronological, non-overlapping indices — and
scores that are inflated by an amount nobody can see. So the tests here assert
on *what was removed*, not just on the shape of what is left.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nifty50.ml.labels import (
    Barrier,
    assert_no_label_leakage,
    average_uniqueness,
    label_horizon_end,
    meta_labels,
    triple_barrier_labels,
)
from nifty50.ml.splits import PurgedWalkForward, train_test_by_date

INDEX = pd.date_range("2025-01-01 09:15", periods=400, freq="15min", tz="Asia/Kolkata")


def frame(closes: list[float], *, highs: list[float] | None = None,
          lows: list[float] | None = None) -> pd.DataFrame:
    index = INDEX[: len(closes)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": highs if highs is not None else closes,
            "low": lows if lows is not None else closes,
        },
        index=index,
    )


def flat_volatility(size: int, value: float = 0.01) -> pd.Series:
    return pd.Series([value] * size, index=INDEX[:size])


class TestBarriers:
    def test_a_rise_to_the_target_labels_plus_one(self) -> None:
        # +1% barrier on a 1% volatility with a 1x multiple; price rises 2%.
        closes = [100.0, 100.5, 101.5, 102.0, 102.0]
        bars = frame(closes)
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(5),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        assert result.label.iloc[0] == 1.0
        assert result.barrier.iloc[0] == Barrier.UPPER
        assert result.touch_time.iloc[0] == INDEX[2]

    def test_a_fall_to_the_stop_labels_minus_one(self) -> None:
        closes = [100.0, 99.5, 98.5, 98.0, 98.0]
        bars = frame(closes)
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(5),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        assert result.label.iloc[0] == -1.0
        assert result.barrier.iloc[0] == Barrier.LOWER

    def test_neither_barrier_within_the_horizon_labels_zero(self) -> None:
        closes = [100.0, 100.1, 100.2, 100.1, 100.0]
        bars = frame(closes)
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(5),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        assert result.label.iloc[0] == 0.0
        assert result.barrier.iloc[0] == Barrier.VERTICAL

    def test_a_bar_straddling_both_barriers_resolves_pessimistically(self) -> None:
        # One bar whose range covers +1% and -1%. OHLC cannot say which came
        # first, so the stop is assumed. Assuming the target instead is how a
        # backtest manufactures an edge out of wide bars.
        bars = frame([100.0, 100.0], highs=[100.0, 102.0], lows=[100.0, 98.0])
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(2),
            horizon_bars=1, upper_multiple=1.0, lower_multiple=1.0,
        )
        assert result.label.iloc[0] == -1.0
        assert result.ambiguous_count == 1
        assert "too tight" in result.describe()

    def test_the_entry_bar_own_range_is_not_used(self) -> None:
        # The entry bar's own high already exceeds the target. Entry is at its
        # close, so that high happened *before* the position existed.
        bars = frame([100.0, 100.0, 100.0], highs=[105.0, 100.0, 100.0], lows=[100.0] * 3)
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(3),
            horizon_bars=2, upper_multiple=1.0, lower_multiple=1.0,
        )
        assert result.label.iloc[0] == 0.0

    def test_the_unresolvable_tail_is_marked_not_labelled_zero(self) -> None:
        bars = frame([100.0] * 10)
        result = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(10),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        # The last bar has no future at all.
        assert result.barrier.iloc[-1] == Barrier.UNRESOLVED
        assert np.isnan(result.label.iloc[-1])
        assert INDEX[9] not in result.usable()

    def test_the_session_cap_stops_a_label_crossing_the_gap(self) -> None:
        bars = frame([100.0] * 10)
        sessions = pd.Series([0] * 5 + [1] * 5, index=bars.index)
        capped = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=flat_volatility(10),
            horizon_bars=8, upper_multiple=1.0, lower_multiple=1.0,
            session_ordinal=sessions,
        )
        # Bar 0's vertical barrier is pulled back to bar 4, the last of its
        # own session, rather than reaching bar 8 in the next one.
        assert capped.touch_time.iloc[0] == INDEX[4]
        # The final bar of a session has no room left and cannot be labelled.
        assert capped.barrier.iloc[4] == Barrier.UNRESOLVED

    def test_the_minimum_barrier_width_floors_a_collapsed_volatility(self) -> None:
        # Volatility of 1bp would put the barriers inside the round-trip cost.
        bars = frame([100.0, 100.0, 100.3], highs=[100.0, 100.0, 100.3], lows=[100.0] * 3)
        tiny = pd.Series([0.0001] * 3, index=bars.index)
        without_floor = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=tiny,
            horizon_bars=2, upper_multiple=1.0, lower_multiple=1.0,
        )
        with_floor = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=tiny,
            horizon_bars=2, upper_multiple=1.0, lower_multiple=1.0,
            min_barrier_pct=0.01,
        )
        assert without_floor.label.iloc[0] == 1.0  # a 1bp "win"
        assert with_floor.label.iloc[0] == 0.0  # correctly, nothing happened


class TestMetaLabels:
    def test_agreement_is_one_and_disagreement_is_zero(self) -> None:
        bars = frame([100.0, 102.0, 104.0])
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(3),
            horizon_bars=2, upper_multiple=1.0, lower_multiple=1.0,
        )
        long_side = pd.Series([1, 1, 1], index=bars.index)
        short_side = pd.Series([-1, -1, -1], index=bars.index)
        assert meta_labels(long_side, labels).iloc[0] == 1.0
        assert meta_labels(short_side, labels).iloc[0] == 0.0

    def test_a_flat_primary_signal_is_nan_not_zero(self) -> None:
        bars = frame([100.0, 102.0, 104.0])
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(3),
            horizon_bars=2, upper_multiple=1.0, lower_multiple=1.0,
        )
        flat = pd.Series([0, 0, 0], index=bars.index)
        # "No opinion" is not "wrong". Training on it as 0 teaches the meta
        # model that doing nothing is a mistake.
        assert meta_labels(flat, labels).isna().all()


class TestUniqueness:
    def test_overlapping_labels_are_downweighted(self) -> None:
        bars = frame([100.0] * 20)
        overlapping = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(20),
            horizon_bars=8, upper_multiple=5.0, lower_multiple=5.0,
        )
        immediate = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(20),
            horizon_bars=1, upper_multiple=5.0, lower_multiple=5.0,
        )
        assert average_uniqueness(overlapping).mean() < 0.5
        assert average_uniqueness(immediate).mean() == pytest.approx(1.0, abs=0.3)


class TestLeakageGuard:
    def test_a_label_column_in_the_features_is_caught(self) -> None:
        bars = frame([100.0] * 20)
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(20),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        features = pd.DataFrame({"rsi": range(20), "label": range(20)}, index=bars.index)
        with pytest.raises(ValueError, match="label columns"):
            assert_no_label_leakage(features, labels)

    def test_a_renamed_copy_of_the_label_is_still_caught(self) -> None:
        rng = np.random.default_rng(0)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
        bars = frame(list(closes))
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(200),
            horizon_bars=6, upper_multiple=1.0, lower_multiple=1.0,
        )
        features = pd.DataFrame({"momentum_signal": labels.label}, index=bars.index)
        with pytest.raises(ValueError, match="that is the answer"):
            assert_no_label_leakage(features, labels)


class TestPurgedWalkForward:
    @pytest.fixture
    def labelled(self) -> tuple[pd.Index, pd.Series]:
        rng = np.random.default_rng(1)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.005, 400)))
        bars = frame(list(closes))
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(400),
            horizon_bars=10, upper_multiple=1.0, lower_multiple=1.0,
        )
        horizon = label_horizon_end(labels)
        return labels.usable(), horizon

    def test_folds_are_chronological_and_disjoint(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, horizon = labelled
        splitter = PurgedWalkForward(folds=4, embargo_fraction=0.0, min_train_size=20)
        folds = list(splitter.split(index, horizon))
        assert len(folds) >= 2
        for fold in folds:
            assert len(fold.train.intersection(fold.test)) == 0
        # Test windows walk forward and do not overlap each other.
        starts = [fold.test.min() for fold in folds]
        assert starts == sorted(starts)

    def test_training_data_never_comes_from_after_the_test_window(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        """The difference between walk-forward and k-fold-with-purging.

        A splitter that trains on both sides of the test window fits the
        earliest fold on the latest data. It looks fine in a fold summary and
        it manufactures out-of-sample skill out of nothing, so this is asserted
        directly rather than inferred from the fold shapes.
        """
        index, horizon = labelled
        splitter = PurgedWalkForward(folds=4, embargo_fraction=0.0, min_train_size=20)
        folds = list(splitter.split(index, horizon))
        assert len(folds) >= 2
        for fold in folds:
            assert fold.train.max() < fold.test.min(), (
                f"fold {fold.number} trains on data after its test window"
            )

    def test_purging_actually_removes_overlapping_labels(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, horizon = labelled
        splitter = PurgedWalkForward(folds=4, embargo_fraction=0.0, min_train_size=20)
        folds = list(splitter.split(index, horizon))
        assert sum(fold.purged for fold in folds) > 0

        # The real assertion: no surviving training label reaches into its test window.
        for fold in folds:
            test_first, test_last = fold.test.min(), fold.test.max()
            ends = horizon.reindex(fold.train)
            reaches_in = (ends >= test_first) & (pd.Series(fold.train, index=fold.train) <= test_last)
            assert not reaches_in.any(), f"fold {fold.number} kept a leaking training label"

    def test_the_embargo_removes_data_after_the_test_window(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, horizon = labelled
        without = PurgedWalkForward(folds=4, embargo_fraction=0.0, min_train_size=20)
        with_embargo = PurgedWalkForward(folds=4, embargo_fraction=0.05, min_train_size=20)
        assert sum(f.embargoed for f in with_embargo.split(index, horizon)) > 0
        assert sum(f.train_size for f in with_embargo.split(index, horizon)) < sum(
            f.train_size for f in without.split(index, horizon)
        )

    def test_a_missing_horizon_is_refused_rather_than_assumed(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, horizon = labelled
        splitter = PurgedWalkForward(folds=3, min_train_size=20)
        with pytest.raises(ValueError, match="no label horizon"):
            list(splitter.split(index, horizon.iloc[:-10]))

    def test_the_report_flags_a_purge_that_did_nothing(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, _ = labelled
        # Labels that resolve on the bar they are stamped on: nothing to purge.
        instant = pd.Series(index, index=index)
        splitter = PurgedWalkForward(folds=3, embargo_fraction=0.0, min_train_size=20)
        assert "nothing was purged" in splitter.report(index, instant)

    def test_an_unsorted_index_is_refused(
        self, labelled: tuple[pd.Index, pd.Series]
    ) -> None:
        index, horizon = labelled
        splitter = PurgedWalkForward(folds=3, min_train_size=20)
        with pytest.raises(ValueError, match="sorted"):
            list(splitter.split(index[::-1], horizon))


class TestHoldout:
    def test_the_holdout_split_purges_the_boundary(self) -> None:
        rng = np.random.default_rng(2)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.005, 400)))
        bars = frame(list(closes))
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(400),
            horizon_bars=10, upper_multiple=1.0, lower_multiple=1.0,
        )
        index, horizon = labels.usable(), label_horizon_end(labels)

        fold = train_test_by_date(index, horizon, split_at=index[200])
        assert fold.purged > 0
        assert fold.train.max() < fold.test.min()
        # Every surviving training label resolved before the test began.
        assert (horizon.reindex(fold.train) < fold.test.min()).all()

    def test_a_split_date_outside_the_sample_is_refused(self) -> None:
        bars = frame([100.0] * 50)
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"], volatility=flat_volatility(50),
            horizon_bars=4, upper_multiple=1.0, lower_multiple=1.0,
        )
        index, horizon = labels.usable(), label_horizon_end(labels)
        with pytest.raises(ValueError, match="empty"):
            train_test_by_date(index, horizon, split_at=pd.Timestamp("2030-01-01", tz="Asia/Kolkata"))
