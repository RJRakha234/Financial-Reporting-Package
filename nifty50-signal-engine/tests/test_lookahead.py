"""Proof that no feature can see the future.

The spec asks for a look-ahead test that asserts feature values at *t* are
unchanged when future rows are truncated. That is implemented here against the
**entire** feature set rather than a hand-picked sample, so a new indicator with
a leak fails automatically the day it is added.

The method: compute features on the full history, then recompute on history
truncated at bar *k*, and compare row *k-1* column by column. Any feature that
peeks — a centred rolling window, an unconfirmed pivot, a higher-timeframe bar
joined on its start instead of its close — produces a different value once the
future is removed, and the comparison catches it.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.config import Config
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import Timeframe
from nifty50.features import FeatureInputs, compute_features
from nifty50.features.multiframe import align_higher_timeframe
from nifty50.trading_calendar import TradingCalendar

START = dt.date(2025, 1, 1)
END = dt.date(2025, 8, 8)
# Truncation points, chosen to land mid-session, at a session open and near the
# end of a session so the session-aware features are exercised at each boundary.
TRUNCATION_POINTS = (1200, 1225, 1237, 2000, 3000)


@pytest.fixture(scope="module")
def bars(real_config: Config) -> pd.DataFrame:
    calendar = TradingCalendar.from_config(real_config)
    return generate_session_bars(calendar, START, END, Timeframe.M15, start_price=1400.0, seed=31)


@pytest.fixture(scope="module")
def index_close(real_config: Config) -> pd.Series:
    calendar = TradingCalendar.from_config(real_config)
    return generate_session_bars(calendar, START, END, Timeframe.M15, start_price=24000.0, seed=32)[
        "close"
    ]


@pytest.fixture(scope="module")
def vix_close(real_config: Config) -> pd.Series:
    calendar = TradingCalendar.from_config(real_config)
    return generate_session_bars(calendar, START, END, Timeframe.M15, start_price=14.0, seed=33)[
        "close"
    ]


@pytest.fixture(scope="module")
def hourly(real_config: Config) -> pd.DataFrame:
    calendar = TradingCalendar.from_config(real_config)
    return generate_session_bars(calendar, START, END, Timeframe.H1, start_price=1400.0, seed=31)


def features_for(
    bars: pd.DataFrame,
    index_close: pd.Series,
    vix_close: pd.Series,
    hourly: pd.DataFrame,
    calendar: TradingCalendar,
    config: Config,
) -> pd.DataFrame:
    return compute_features(
        FeatureInputs(
            bars=bars,
            timeframe=Timeframe.M15,
            index_close=index_close,
            vix_close=vix_close,
            higher_timeframe_bars={Timeframe.H1: hourly},
        ),
        calendar,
        config,
    )


def assert_rows_match(full: pd.Series, truncated: pd.Series, label: str) -> None:
    for column in full.index:
        original, restricted = full[column], truncated[column]
        both_missing = _is_missing(original) and _is_missing(restricted)
        if both_missing:
            continue
        assert not _is_missing(original) and not _is_missing(restricted), (
            f"{label}: {column} appears/disappears when the future is truncated "
            f"(full={original!r}, truncated={restricted!r})"
        )
        if isinstance(original, (bool, np.bool_)):
            assert bool(original) == bool(restricted), f"{label}: {column} changed"
        else:
            assert float(original) == pytest.approx(float(restricted), rel=1e-9), (
                f"{label}: {column} changed when the future was truncated "
                f"({original} -> {restricted}) — this feature sees ahead"
            )


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, (bool, np.bool_)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


class TestNoLookAhead:
    @pytest.mark.parametrize("cut", TRUNCATION_POINTS)
    def test_every_feature_is_unchanged_when_the_future_is_removed(
        self,
        cut: int,
        bars: pd.DataFrame,
        index_close: pd.Series,
        vix_close: pd.Series,
        hourly: pd.DataFrame,
        real_config: Config,
    ) -> None:
        calendar = TradingCalendar.from_config(real_config)
        full = features_for(bars, index_close, vix_close, hourly, calendar, real_config)

        cutoff = bars.index[cut]
        truncated = features_for(
            bars.iloc[:cut],
            index_close[index_close.index < cutoff],
            vix_close[vix_close.index < cutoff],
            hourly[hourly.index < cutoff],
            calendar,
            real_config,
        )

        assert list(full.columns) == list(truncated.columns)
        assert_rows_match(full.iloc[cut - 1], truncated.iloc[-1], label=f"bar {cut - 1} ({cutoff})")

    def test_the_feature_set_under_test_is_not_trivially_small(
        self,
        bars: pd.DataFrame,
        index_close: pd.Series,
        vix_close: pd.Series,
        hourly: pd.DataFrame,
        real_config: Config,
    ) -> None:
        # A guard on the guard: if compute_features silently returned two
        # columns, the parametrised test above would pass and prove nothing.
        calendar = TradingCalendar.from_config(real_config)
        features = features_for(bars, index_close, vix_close, hourly, calendar, real_config)
        assert len(features.columns) > 40
        # And the values must actually be populated by the end of the history.
        tail = features.iloc[-1]
        assert tail.notna().sum() > 30

    def test_the_harness_actually_detects_a_leak(self) -> None:
        """A guard that cannot fail is not a guard.

        Feed the comparison a feature that genuinely peeks — a centred rolling
        mean, which is the classic accidental leak — and confirm it is rejected.
        """
        prices = pd.Series(
            np.arange(100, dtype="float64"),
            index=pd.date_range("2025-08-04 09:15", periods=100, freq="15min", tz="Asia/Kolkata"),
        )
        leaky_full = prices.rolling(window=11, center=True, min_periods=1).mean()
        leaky_truncated = prices.iloc[:60].rolling(window=11, center=True, min_periods=1).mean()

        full_row = pd.Series({"centred_mean": leaky_full.iloc[59]})
        truncated_row = pd.Series({"centred_mean": leaky_truncated.iloc[-1]})

        with pytest.raises(AssertionError, match="sees ahead"):
            assert_rows_match(full_row, truncated_row, label="leak check")

    def test_no_source_file_shifts_a_series_backwards(self) -> None:
        """A static backstop: ``shift(-n)`` is how the future gets pulled in."""
        import ast

        from nifty50.config import project_root

        offences: list[str] = []
        for path in sorted((project_root() / "src" / "nifty50").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute) or function.attr != "shift":
                    continue
                for argument in [*node.args, *(k.value for k in node.keywords)]:
                    if isinstance(argument, ast.UnaryOp) and isinstance(argument.op, ast.USub):
                        offences.append(f"{path.name}:{node.lineno} shift(-n)")
        assert offences == [], "negative shifts pull the future backwards:\n" + "\n".join(offences)


class TestHigherTimeframeAlignment:
    """The subtlest leak: joining higher-timeframe bars on start, not close."""

    def test_a_base_bar_only_sees_completed_higher_bars(self, real_config: Config) -> None:
        calendar = TradingCalendar.from_config(real_config)
        day = dt.date(2025, 8, 4)
        base = generate_session_bars(calendar, day, day, Timeframe.M15, seed=1)
        higher = generate_session_bars(calendar, day, day, Timeframe.H1, seed=1)

        aligned = align_higher_timeframe(base.index, higher[["close"]], calendar, Timeframe.H1)

        first_hour_start = higher.index[0]  # 09:15, completes at 10:15
        second_hour_start = higher.index[1]  # 10:15, completes at 11:15

        def at(hour: int, minute: int) -> float:
            ts = pd.Timestamp(dt.datetime.combine(day, dt.time(hour, minute)), tz=base.index.tz)
            return aligned.at[ts, "close_1h"]

        # Inside the first hour nothing has completed yet.
        assert np.isnan(at(9, 15))
        assert np.isnan(at(10, 0))
        # At 10:15 the 09:15 hourly bar has just closed and becomes visible.
        assert at(10, 15) == pytest.approx(higher.at[first_hour_start, "close"])
        # At 10:30 the 10:15 hourly bar is still running — the value must not move.
        assert at(10, 30) == pytest.approx(higher.at[first_hour_start, "close"])
        assert at(11, 0) == pytest.approx(higher.at[first_hour_start, "close"])
        # Only at 11:15 does the second hourly bar become available.
        assert at(11, 15) == pytest.approx(higher.at[second_hour_start, "close"])

    def test_the_naive_join_would_have_leaked(self, real_config: Config) -> None:
        """Demonstrates the bug this alignment exists to avoid."""
        calendar = TradingCalendar.from_config(real_config)
        day = dt.date(2025, 8, 4)
        base = generate_session_bars(calendar, day, day, Timeframe.M15, seed=1)
        higher = generate_session_bars(calendar, day, day, Timeframe.H1, seed=1)

        naive = higher[["close"]].reindex(base.index).ffill()
        correct = align_higher_timeframe(base.index, higher[["close"]], calendar, Timeframe.H1)
        ts = pd.Timestamp(dt.datetime.combine(day, dt.time(10, 30)), tz=base.index.tz)

        # The naive join hands 10:30 an hourly bar that does not finish until
        # 11:15 — 45 minutes of prices it could not have known.
        assert naive.at[ts, "close"] == pytest.approx(higher.at[higher.index[1], "close"])
        assert correct.at[ts, "close_1h"] != pytest.approx(naive.at[ts, "close"])

    def test_the_ragged_final_hourly_bar_is_available_at_the_session_close(
        self, real_config: Config
    ) -> None:
        # The 15:15-15:30 stub completes at 15:30, not at a fictional 16:15.
        calendar = TradingCalendar.from_config(real_config)
        day = dt.date(2025, 8, 4)
        higher = generate_session_bars(calendar, day, day, Timeframe.H1, seed=1)
        from nifty50.features.multiframe import higher_timeframe_close_times

        close_times = higher_timeframe_close_times(higher, calendar, Timeframe.H1)
        assert close_times[-1].time() == dt.time(15, 30)


class TestDivergenceConfirmation:
    def test_a_pivot_is_reported_only_after_it_is_confirmed(self) -> None:
        from nifty50.features.trend import pivot_highs

        # A clean peak at position 5, with five bars either side.
        values = pd.Series(
            [1.0, 2.0, 3.0, 4.0, 5.0, 9.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            index=pd.date_range("2025-08-04 09:15", periods=11, freq="15min", tz="Asia/Kolkata"),
        )
        pivots = pivot_highs(values, left=5, right=5)
        # Nothing is claimed at the peak itself — it is not yet knowable.
        assert np.isnan(pivots.iloc[5])
        # It is stamped five bars later, when the right-hand side has held.
        assert pivots.iloc[10] == pytest.approx(9.0)
