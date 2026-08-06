"""Session-aware features: resets at the open, and the U-shaped volume problem."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import IST, Timeframe
from nifty50.features import core, session, volume
from nifty50.trading_calendar import TradingCalendar

MONDAY = dt.date(2025, 8, 4)
FRIDAY = dt.date(2025, 8, 8)


@pytest.fixture
def week(calendar: TradingCalendar) -> pd.DataFrame:
    return generate_session_bars(
        calendar, MONDAY, FRIDAY, Timeframe.M15, start_price=1000.0, seed=21
    )


class TestSessionStructure:
    def test_ordinal_is_constant_within_a_session_and_increments_across(
        self, week: pd.DataFrame
    ) -> None:
        ordinal = session.session_ordinal(week)
        assert ordinal.nunique() == 5
        monday = ordinal[[ts.date() == MONDAY for ts in week.index]]
        assert (monday == monday.iloc[0]).all()
        assert list(ordinal.unique()) == [0, 1, 2, 3, 4]

    def test_bar_of_session_runs_zero_to_twenty_four(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        slot = session.bar_of_session(week, calendar, Timeframe.M15)
        assert slot.min() == 0
        assert slot.max() == 24  # 375 minutes / 15 == 25 bars
        assert (slot.value_counts() == 5).all()  # every slot appears once per session

    def test_the_first_bar_of_each_session_is_flagged(self, week: pd.DataFrame) -> None:
        flags = session.is_session_open_bar(week)
        assert flags.sum() == 5
        assert all(ts.time() == dt.time(9, 15) for ts in week.index[flags])

    def test_overnight_gap_exists_only_at_the_open(self, week: pd.DataFrame) -> None:
        gap = session.overnight_gap(week)
        # Four gaps, not five: the first session has no prior close to gap from.
        assert gap.notna().sum() == 4
        assert all(ts.time() == dt.time(9, 15) for ts in week.index[gap.notna()])

    def test_overnight_gap_is_computed_against_the_prior_session_close(
        self, week: pd.DataFrame
    ) -> None:
        gap = session.overnight_gap(week)
        tuesday_open_ts = next(ts for ts in week.index if ts.date() == dt.date(2025, 8, 5))
        monday_close = week[[ts.date() == MONDAY for ts in week.index]]["close"].iloc[-1]
        tuesday_open = week.at[tuesday_open_ts, "open"]
        assert gap[tuesday_open_ts] == pytest.approx(tuesday_open / monday_close - 1.0)


class TestOpeningRange:
    def test_the_first_hour_range_is_invisible_until_the_hour_has_closed(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        """A level known at 09:20 would be a look-ahead."""
        highs = session.session_window_extreme(week, calendar, minutes=60, column="high", how="max")
        inside = [ts for ts in week.index if ts.date() == MONDAY and ts.time() < dt.time(10, 15)]
        after = [ts for ts in week.index if ts.date() == MONDAY and ts.time() >= dt.time(10, 15)]
        assert highs[inside].isna().all()
        assert highs[after].notna().all()

    def test_the_level_equals_the_windows_actual_extreme(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        highs = session.session_window_extreme(week, calendar, minutes=60, column="high", how="max")
        lows = session.session_window_extreme(week, calendar, minutes=60, column="low", how="min")
        window = week[[ts.date() == MONDAY and ts.time() < dt.time(10, 15) for ts in week.index]]
        after = next(
            ts for ts in week.index if ts.date() == MONDAY and ts.time() == dt.time(10, 15)
        )
        assert highs[after] == pytest.approx(window["high"].max())
        assert lows[after] == pytest.approx(window["low"].min())

    def test_each_session_gets_its_own_range(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        highs = session.session_window_extreme(week, calendar, minutes=60, column="high", how="max")
        per_day = {ts.date(): highs[ts] for ts in week.index if highs.notna()[ts]}
        assert len(set(per_day.values())) == 5  # five distinct levels


class TestSessionMatchedVolume:
    """NSE intraday volume is U-shaped; a flat baseline measures the clock."""

    def build(
        self, calendar: TradingCalendar, per_slot: dict[int, list[float]]
    ) -> tuple[pd.DataFrame, pd.Series]:
        sessions = calendar.trading_days(MONDAY, dt.date(2025, 9, 30))
        rows, index = [], []
        for day_number, day in enumerate(sessions):
            for slot, ts in enumerate(calendar.bar_starts(day, Timeframe.M15)):
                volumes = per_slot.get(slot)
                value = volumes[day_number] if volumes else 1000.0
                rows.append(
                    {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": value}
                )
                index.append(ts)
        frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index, name="ts"))
        return frame, session.bar_of_session(frame, calendar, Timeframe.M15)

    def jittered(self, level: float, count: int, seed: int) -> list[float]:
        """A repeating volume profile with realistic day-to-day noise."""
        rng = np.random.default_rng(seed)
        return list(level * rng.lognormal(mean=0.0, sigma=0.2, size=count))

    def test_a_u_shaped_day_beats_a_flat_baseline(self, calendar: TradingCalendar) -> None:
        """The claim this feature exists to make.

        Slot 0 is always heavy and slot 12 always thin — a genuine U-shape that
        repeats every session. Nothing about it is unusual. A flat trailing
        average flags both extremes every single day; the session-matched
        baseline does not.
        """
        sessions = len(calendar.trading_days(MONDAY, dt.date(2025, 9, 30)))
        frame, slot = self.build(
            calendar,
            {
                0: self.jittered(500_000.0, sessions, seed=1),
                12: self.jittered(20_000.0, sessions, seed=2),
            },
        )
        matched = volume.session_matched_volume_zscore(frame["volume"], slot, lookback_sessions=20)
        flat = core.rolling_zscore(frame["volume"].astype("float64"), 20 * 25)

        # The opening slot is the clearest case: it is heavy every single day,
        # by construction, and nothing about that is news.
        opening_bars = frame.index[(slot == 0).to_numpy()]
        matched_open = matched.reindex(opening_bars).dropna()
        flat_open = flat.reindex(opening_bars).dropna()
        assert len(matched_open) > 0
        assert len(flat_open) > 0

        # Flat baseline: every ordinary open looks like a 4-sigma event.
        assert flat_open.abs().median() > 4.0
        # Session-matched: an ordinary open looks ordinary.
        assert matched_open.abs().median() < 1.0

    def test_a_genuine_spike_is_flagged(self, calendar: TradingCalendar) -> None:
        sessions = len(calendar.trading_days(MONDAY, dt.date(2025, 9, 30)))
        open_volumes = self.jittered(500_000.0, sessions, seed=3)
        open_volumes[-1] = 5_000_000.0  # ten times the usual opening burst
        frame, slot = self.build(calendar, {0: open_volumes})
        scores = volume.session_matched_volume_zscore(frame["volume"], slot, lookback_sessions=20)
        assert scores[frame.index[-25]] > 5.0  # slot 0 of the final session

    def test_the_current_bar_is_excluded_from_its_own_baseline(
        self, calendar: TradingCalendar
    ) -> None:
        # If today's reading were inside the distribution it is scored against,
        # a large enough spike would partly mask itself and read lower.
        sessions = len(calendar.trading_days(MONDAY, dt.date(2025, 9, 30)))
        volumes = self.jittered(100_000.0, sessions, seed=4)
        volumes[-1] = 2_000_000.0
        frame, slot = self.build(calendar, {0: volumes})
        scores = volume.session_matched_volume_zscore(frame["volume"], slot, lookback_sessions=20)
        excluded = scores[frame.index[-25]]

        history = np.array(volumes[-21:-1])
        including_today = (volumes[-1] - np.append(history, volumes[-1]).mean()) / np.append(
            history, volumes[-1]
        ).std(ddof=0)
        assert excluded > including_today

    def test_zero_dispersion_history_is_undefined_not_zero(self, calendar: TradingCalendar) -> None:
        # A perfectly constant history has no scale to measure against. NaN is
        # the honest answer; relative_volume covers this case with a ratio.
        sessions = len(calendar.trading_days(MONDAY, dt.date(2025, 9, 30)))
        frame, slot = self.build(calendar, {0: [100_000.0] * sessions})
        scores = volume.session_matched_volume_zscore(frame["volume"], slot, lookback_sessions=20)
        assert np.isnan(scores[frame.index[-25]])

    def test_relative_volume_uses_a_median_and_survives_one_outlier(
        self, calendar: TradingCalendar
    ) -> None:
        sessions = len(calendar.trading_days(MONDAY, dt.date(2025, 9, 30)))
        volumes = [100_000.0] * sessions
        volumes[5] = 50_000_000.0  # one absurd print in the history
        volumes[-1] = 200_000.0
        frame, slot = self.build(calendar, {0: volumes})
        ratios = volume.relative_volume(frame["volume"], slot, lookback_sessions=20)
        # Median baseline is untouched by the outlier, so today reads as 2x.
        assert ratios[frame.index[-25]] == pytest.approx(2.0)


class TestVwapAndFlow:
    def test_vwap_resets_at_every_session_open(self, week: pd.DataFrame) -> None:
        ordinal = session.session_ordinal(week)
        vwap = volume.session_vwap(
            week["high"], week["low"], week["close"], week["volume"].astype(float), ordinal
        )
        for day in (MONDAY, dt.date(2025, 8, 5)):
            first = next(ts for ts in week.index if ts.date() == day)
            typical = (week.at[first, "high"] + week.at[first, "low"] + week.at[first, "close"]) / 3
            # The session's first bar has only itself in the average.
            assert vwap[first] == pytest.approx(typical)

    def test_vwap_is_hand_computable_over_two_bars(self, calendar: TradingCalendar) -> None:
        index = pd.DatetimeIndex(
            [
                dt.datetime(2025, 8, 4, 9, 15, tzinfo=IST),
                dt.datetime(2025, 8, 4, 9, 30, tzinfo=IST),
            ],
            name="ts",
        )
        frame = pd.DataFrame(
            {
                "high": [12.0, 22.0],
                "low": [6.0, 14.0],
                "close": [9.0, 18.0],
                "volume": [100.0, 300.0],
            },
            index=index,
        )
        ordinal = session.session_ordinal(frame)
        vwap = volume.session_vwap(
            frame["high"], frame["low"], frame["close"], frame["volume"], ordinal
        )
        # typical prices are 9 and 18; (9*100 + 18*300) / 400 = 15.75
        assert vwap.iloc[1] == pytest.approx(15.75)

    def test_obv_accumulates_signed_volume(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=4, freq="15min", tz=IST)
        close = pd.Series([10.0, 11.0, 10.5, 10.5], index=index)
        volumes = pd.Series([100.0, 200.0, 300.0, 400.0], index=index)
        obv = volume.obv(close, volumes)
        # up +200, down -300, unchanged contributes nothing
        assert list(obv) == [0.0, 200.0, -100.0, -100.0]

    def test_distance_from_vwap_is_scaled_by_atr(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=2, freq="15min", tz=IST)
        close = pd.Series([110.0, 110.0], index=index)
        vwap = pd.Series([100.0, 100.0], index=index)
        atr = pd.Series([5.0, 20.0], index=index)
        distance = volume.distance_from_vwap_in_atr(close, vwap, atr)
        # The same 10-rupee gap is 2 ATR on a quiet name and 0.5 on a wild one.
        assert distance.iloc[0] == pytest.approx(2.0)
        assert distance.iloc[1] == pytest.approx(0.5)

    def test_order_book_imbalance_is_bounded_and_nan_without_depth(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=3, freq="15min", tz=IST)
        bids = pd.Series([300.0, 0.0, np.nan], index=index)
        asks = pd.Series([100.0, 400.0, np.nan], index=index)
        imbalance = volume.order_book_imbalance(bids, asks)
        assert imbalance.iloc[0] == pytest.approx(0.5)
        assert imbalance.iloc[1] == pytest.approx(-1.0)
        # Historical bars carry no depth, so a backtest sees NaN by construction.
        assert np.isnan(imbalance.iloc[2])

    def test_breakout_state(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=4, freq="15min", tz=IST)
        close = pd.Series([105.0, 95.0, 100.0, 105.0], index=index)
        upper = pd.Series([104.0, 104.0, 104.0, np.nan], index=index)
        lower = pd.Series([96.0, 96.0, 96.0, np.nan], index=index)
        state = volume.breakout_state(close, upper, lower)
        assert list(state) == [1, -1, 0, 0]
