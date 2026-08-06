"""Integrity checks: gaps, duplicates, bad bars and missed corporate actions."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from nifty50.data.integrity import (
    Severity,
    check_bars,
    find_duplicate_bars,
    find_gaps_across_sessions,
    find_invalid_ohlc,
    find_missing_bars,
    find_out_of_order,
    find_suspected_unadjusted_actions,
    find_unexpected_bars,
    reconcile_candles,
    suggest_missing_holidays,
)
from nifty50.data.synthetic import apply_unadjusted_split, generate_session_bars
from nifty50.domain import IST, Timeframe
from nifty50.trading_calendar import TradingCalendar

WEEK_START = dt.date(2025, 8, 4)
WEEK_END = dt.date(2025, 8, 8)


@pytest.fixture
def week(calendar: TradingCalendar) -> pd.DataFrame:
    return generate_session_bars(
        calendar, WEEK_START, WEEK_END, Timeframe.M15, start_price=1400.0, seed=11
    )


class TestStructuralChecks:
    def test_a_clean_week_has_nothing_missing(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        assert find_missing_bars(week, calendar, Timeframe.M15, WEEK_START, WEEK_END) == []

    def test_missing_bars_are_found(self, week: pd.DataFrame, calendar: TradingCalendar) -> None:
        holed = week.drop(week.index[10:13])
        missing = find_missing_bars(holed, calendar, Timeframe.M15, WEEK_START, WEEK_END)
        assert len(missing) == 3
        assert missing == list(week.index[10:13])

    def test_duplicates_are_found(self, week: pd.DataFrame) -> None:
        doubled = pd.concat([week, week.iloc[:2]]).sort_index()
        found = find_duplicate_bars(doubled)
        assert len(found) == 2

    def test_out_of_order_is_found(self, week: pd.DataFrame) -> None:
        shuffled = pd.concat([week.iloc[5:6], week.iloc[0:5]])
        assert find_out_of_order(shuffled)
        assert find_out_of_order(week) == []

    def test_invalid_ohlc_is_annotated(self, week: pd.DataFrame) -> None:
        broken = week.copy()
        broken.iloc[0, broken.columns.get_loc("high")] = broken["low"].iloc[0] - 1.0
        broken.iloc[1, broken.columns.get_loc("volume")] = -5
        found = find_invalid_ohlc(broken)
        assert len(found) == 2
        assert "high<low" in found["reason"].iloc[0]
        assert "negative volume" in found["reason"].iloc[1]

    def test_invalid_ohlc_tolerates_duplicate_timestamps(self, week: pd.DataFrame) -> None:
        # The frame under inspection may itself be malformed; the check must not
        # assume a unique index.
        broken = pd.concat([week.iloc[:2], week.iloc[:2]])
        broken.iloc[0, broken.columns.get_loc("volume")] = -1
        assert len(find_invalid_ohlc(broken)) == 1

    def test_session_gaps_are_found(self, week: pd.DataFrame, calendar: TradingCalendar) -> None:
        assert find_gaps_across_sessions(week, calendar) == []
        without_wednesday = week[[ts.date() != dt.date(2025, 8, 6) for ts in week.index]]
        gaps = find_gaps_across_sessions(without_wednesday, calendar)
        assert gaps == [(dt.date(2025, 8, 5), dt.date(2025, 8, 7))]


class TestCalendarDisagreement:
    def test_bars_on_a_holiday_are_flagged(self, calendar: TradingCalendar) -> None:
        """A phantom holiday silently deletes a real session from every backtest."""
        real = generate_session_bars(
            calendar, dt.date(2025, 8, 14), dt.date(2025, 8, 14), Timeframe.M15, seed=2
        )
        # Pretend the exchange traded on Independence Day.
        holiday_index = pd.DatetimeIndex([ts.replace(day=15) for ts in real.index], name="ts")
        on_holiday = real.set_axis(holiday_index)
        found = find_unexpected_bars(on_holiday, calendar, Timeframe.M15)
        assert len(found) == len(real)

    def test_bars_off_the_grid_are_flagged(self, calendar: TradingCalendar) -> None:
        real = generate_session_bars(
            calendar, dt.date(2025, 8, 4), dt.date(2025, 8, 4), Timeframe.M15, seed=2
        )
        # A bar at 15:47 belongs to the closing session, not the continuous tape.
        stray = real.iloc[:1].set_axis(
            pd.DatetimeIndex([dt.datetime(2025, 8, 4, 15, 47, tzinfo=IST)], name="ts")
        )
        found = find_unexpected_bars(pd.concat([real, stray]), calendar, Timeframe.M15)
        assert found == [dt.datetime(2025, 8, 4, 15, 47, tzinfo=IST)]

    def test_a_clean_week_has_no_unexpected_bars(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        assert find_unexpected_bars(week, calendar, Timeframe.M15) == []

    def test_missing_holidays_are_suggested_from_data(self, calendar: TradingCalendar) -> None:
        # Every symbol silent on the same trading day is near-conclusive evidence
        # of an exchange holiday the file does not know about.
        frames = {}
        for i in range(6):
            frame = generate_session_bars(calendar, WEEK_START, WEEK_END, Timeframe.D1, seed=i)
            frames[f"SYM{i}"] = frame[[ts.date() != dt.date(2025, 8, 6) for ts in frame.index]]
        assert suggest_missing_holidays(frames, calendar, WEEK_START, WEEK_END) == [
            dt.date(2025, 8, 6)
        ]

    def test_suggestion_needs_enough_symbols_to_be_meaningful(
        self, calendar: TradingCalendar
    ) -> None:
        frame = generate_session_bars(calendar, WEEK_START, WEEK_END, Timeframe.D1, seed=1)
        assert (
            suggest_missing_holidays({"ONE": frame.iloc[:1]}, calendar, WEEK_START, WEEK_END) == []
        )


class TestUnadjustedActionDetection:
    def test_an_unadjusted_split_is_caught(self, calendar: TradingCalendar) -> None:
        clean = generate_session_bars(
            calendar, dt.date(2019, 9, 10), dt.date(2019, 9, 30), Timeframe.D1, seed=4
        )
        raw = apply_unadjusted_split(clean, dt.date(2019, 9, 19), ratio_new=2, ratio_old=1)
        suspects = find_suspected_unadjusted_actions(raw, threshold_abs_log_return=0.20)
        assert [day for day, _ in suspects] == [dt.date(2019, 9, 19)]
        assert suspects[0][1] < -0.6

    def test_a_known_action_suppresses_the_alarm(self, calendar: TradingCalendar) -> None:
        clean = generate_session_bars(
            calendar, dt.date(2019, 9, 10), dt.date(2019, 9, 30), Timeframe.D1, seed=4
        )
        raw = apply_unadjusted_split(clean, dt.date(2019, 9, 19), ratio_new=2, ratio_old=1)
        suspects = find_suspected_unadjusted_actions(
            raw,
            threshold_abs_log_return=0.20,
            known_action_dates=[dt.date(2019, 9, 19)],
        )
        assert suspects == []

    def test_ordinary_volatility_is_not_flagged(self, week: pd.DataFrame) -> None:
        assert find_suspected_unadjusted_actions(week, threshold_abs_log_return=0.20) == []


class TestReconciliation:
    def test_matching_candles_reconcile(self, week: pd.DataFrame) -> None:
        assert (
            reconcile_candles(week, week, price_rel_tolerance=0.0005, volume_rel_tolerance=0.02)
            == []
        )

    def test_price_drift_beyond_tolerance_is_reported(self, week: pd.DataFrame) -> None:
        drifted = week.copy()
        drifted.iloc[0, drifted.columns.get_loc("close")] *= 1.01
        mismatches = reconcile_candles(
            drifted, week, price_rel_tolerance=0.0005, volume_rel_tolerance=0.02
        )
        assert len(mismatches) == 1
        assert mismatches[0].field == "close"
        assert mismatches[0].relative_error == pytest.approx(0.01, rel=1e-3)

    def test_volume_has_its_own_looser_tolerance(self, week: pd.DataFrame) -> None:
        drifted = week.copy()
        drifted.iloc[0, drifted.columns.get_loc("volume")] = int(week["volume"].iloc[0] * 1.01)
        assert (
            reconcile_candles(drifted, week, price_rel_tolerance=0.0005, volume_rel_tolerance=0.02)
            == []
        )


class TestFullSuite:
    def test_a_clean_series_passes(self, week: pd.DataFrame, calendar: TradingCalendar) -> None:
        report = check_bars(
            week,
            symbol="RELIANCE",
            timeframe=Timeframe.M15,
            calendar=calendar,
            start=WEEK_START,
            end=WEEK_END,
            suspect_abs_log_return=0.20,
        )
        assert report.is_clean, report.describe()

    def test_a_dirty_series_reports_errors(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        dirty = pd.concat([week, week.iloc[:1]]).sort_index()
        report = check_bars(
            dirty,
            symbol="RELIANCE",
            timeframe=Timeframe.M15,
            calendar=calendar,
            start=WEEK_START,
            end=WEEK_END,
            suspect_abs_log_return=0.20,
        )
        assert not report.is_clean
        assert any(f.check == "duplicate_bars" for f in report.errors)

    def test_a_large_hole_escalates_to_an_error(
        self, week: pd.DataFrame, calendar: TradingCalendar
    ) -> None:
        holed = week.iloc[:50]  # two of five sessions
        report = check_bars(
            holed,
            symbol="RELIANCE",
            timeframe=Timeframe.M15,
            calendar=calendar,
            start=WEEK_START,
            end=WEEK_END,
            suspect_abs_log_return=0.20,
            max_missing_pct=0.02,
        )
        missing = [f for f in report.findings if f.check == "missing_bars"]
        assert missing and missing[0].severity is Severity.ERROR
