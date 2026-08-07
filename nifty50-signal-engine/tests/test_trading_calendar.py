"""Session model: holidays, windows, the bar grid and session boundaries."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import pytest

from nifty50.domain import IST, SessionPhase, Timeframe
from nifty50.trading_calendar import NotATradingDayError, TradingCalendar

MONDAY = dt.date(2025, 8, 4)
SATURDAY = dt.date(2025, 8, 9)
INDEPENDENCE_DAY = dt.date(2025, 8, 15)  # Friday
MUHURAT_2025 = dt.date(2025, 10, 21)  # Diwali: regular market shut, evening session


def at(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


class TestTradingDays:
    def test_weekday_is_a_trading_day(self, calendar: TradingCalendar) -> None:
        assert calendar.is_trading_day(MONDAY)

    def test_weekend_is_not(self, calendar: TradingCalendar) -> None:
        assert not calendar.is_trading_day(SATURDAY)

    def test_holiday_is_not(self, calendar: TradingCalendar) -> None:
        assert not calendar.is_trading_day(INDEPENDENCE_DAY)
        holiday = calendar.holiday(INDEPENDENCE_DAY)
        assert holiday is not None
        assert "Independence" in holiday.name

    def test_schedule_refuses_a_closed_day(self, calendar: TradingCalendar) -> None:
        # Enforced by exception rather than by a bool nobody checks: this is what
        # makes "never compute a signal on a non-trading day" hard to get wrong.
        with pytest.raises(NotATradingDayError, match="holiday"):
            calendar.schedule(INDEPENDENCE_DAY)
        with pytest.raises(NotATradingDayError, match="weekend"):
            calendar.schedule(SATURDAY)

    def test_next_and_previous_skip_weekend_and_holiday(self, calendar: TradingCalendar) -> None:
        # Thursday 14 Aug -> next is Monday 18 Aug (15th is a holiday, 16/17 weekend).
        assert calendar.next_trading_day(dt.date(2025, 8, 14)) == dt.date(2025, 8, 18)
        assert calendar.previous_trading_day(dt.date(2025, 8, 18)) == dt.date(2025, 8, 14)

    def test_sessions_between_counts_the_t_plus_one_clock(self, calendar: TradingCalendar) -> None:
        assert calendar.sessions_between(dt.date(2025, 8, 14), dt.date(2025, 8, 18)) == 1
        assert calendar.sessions_between(MONDAY, MONDAY) == 0


class TestSessionWindows:
    def test_standard_windows(self, calendar: TradingCalendar) -> None:
        schedule = calendar.schedule(MONDAY)
        assert schedule.open == at(MONDAY, 9, 15)
        assert schedule.close == at(MONDAY, 15, 30)
        assert schedule.pre_open is not None
        assert schedule.pre_open.start == at(MONDAY, 9, 0)
        assert schedule.post_close is not None
        assert schedule.post_close.end == at(MONDAY, 16, 0)

    @pytest.mark.parametrize(
        ("hour", "minute", "expected"),
        [
            (8, 30, SessionPhase.CLOSED),
            (9, 5, SessionPhase.PRE_OPEN),
            (9, 15, SessionPhase.CONTINUOUS),
            (12, 0, SessionPhase.CONTINUOUS),
            (15, 29, SessionPhase.CONTINUOUS),
            (15, 30, SessionPhase.CLOSED),  # gap between continuous and closing session
            (15, 45, SessionPhase.POST_CLOSE),
            (16, 30, SessionPhase.CLOSED),
        ],
    )
    def test_phase(
        self, calendar: TradingCalendar, hour: int, minute: int, expected: SessionPhase
    ) -> None:
        assert calendar.phase(at(MONDAY, hour, minute)) is expected

    def test_phase_is_closed_all_day_on_a_holiday(self, calendar: TradingCalendar) -> None:
        assert calendar.phase(at(INDEPENDENCE_DAY, 11, 0)) is SessionPhase.CLOSED

    def test_only_continuous_is_tradeable(self) -> None:
        assert SessionPhase.CONTINUOUS.is_tradeable
        assert not SessionPhase.PRE_OPEN.is_tradeable
        assert not SessionPhase.POST_CLOSE.is_tradeable
        assert not SessionPhase.CLOSED.is_tradeable


class TestBarGrid:
    @pytest.mark.parametrize(
        ("timeframe", "expected"),
        [(Timeframe.M1, 375), (Timeframe.M5, 75), (Timeframe.M15, 25), (Timeframe.D1, 1)],
    )
    def test_bar_counts_for_a_375_minute_session(
        self, calendar: TradingCalendar, timeframe: Timeframe, expected: int
    ) -> None:
        assert calendar.bars_per_session(MONDAY, timeframe) == expected

    def test_hourly_session_ends_in_a_ragged_stub_bar(self, calendar: TradingCalendar) -> None:
        # 375 minutes does not divide by 60. Six full hours plus a 15-minute stub.
        starts = calendar.bar_starts(MONDAY, Timeframe.H1)
        assert len(starts) == 7
        assert starts[0] == at(MONDAY, 9, 15)
        assert starts[-1] == at(MONDAY, 15, 15)
        assert not calendar.is_partial_bar(starts[-2], Timeframe.H1)
        assert calendar.is_partial_bar(starts[-1], Timeframe.H1)
        # The stub is clamped to the close, not stretched into 16:15.
        assert calendar.bar_end(starts[-1], Timeframe.H1) == at(MONDAY, 15, 30)

    def test_no_15m_bar_is_partial(self, calendar: TradingCalendar) -> None:
        assert not any(
            calendar.is_partial_bar(ts, Timeframe.M15)
            for ts in calendar.bar_starts(MONDAY, Timeframe.M15)
        )

    def test_daily_bar_is_stamped_at_the_session_open(self, calendar: TradingCalendar) -> None:
        # Not midnight: a bar stamped 00:00 would appear available nine hours
        # before the session it summarises had begun.
        assert calendar.bar_starts(MONDAY, Timeframe.D1) == [at(MONDAY, 9, 15)]

    @pytest.mark.parametrize(
        ("hour", "minute", "expected_hour", "expected_minute"),
        [(9, 15, 9, 15), (9, 29, 9, 15), (9, 30, 9, 30), (15, 29, 15, 15)],
    )
    def test_floor_to_bar(
        self,
        calendar: TradingCalendar,
        hour: int,
        minute: int,
        expected_hour: int,
        expected_minute: int,
    ) -> None:
        floored = calendar.floor_to_bar(at(MONDAY, hour, minute), Timeframe.M15)
        assert floored == at(MONDAY, expected_hour, expected_minute)

    @pytest.mark.parametrize(("hour", "minute"), [(9, 5), (15, 47), (18, 0)])
    def test_floor_to_bar_refuses_ticks_outside_the_continuous_session(
        self, calendar: TradingCalendar, hour: int, minute: int
    ) -> None:
        # Rounding a 15:47 closing-session print into the 15:15 bar would fold
        # post-close prints into the regular tape.
        with pytest.raises(NotATradingDayError):
            calendar.floor_to_bar(at(MONDAY, hour, minute), Timeframe.M15)

    def test_is_session_open_bar(self, calendar: TradingCalendar) -> None:
        assert calendar.is_session_open_bar(at(MONDAY, 9, 15), Timeframe.M15)
        assert not calendar.is_session_open_bar(at(MONDAY, 9, 30), Timeframe.M15)


class TestSessionBoundaries:
    """The spec's "no window rolls across the overnight gap" requirement."""

    def test_expected_grid_has_no_bar_between_the_close_and_the_next_open(
        self, calendar: TradingCalendar
    ) -> None:
        starts = calendar.expected_bar_starts(
            dt.date(2025, 8, 4), dt.date(2025, 8, 8), Timeframe.M15
        )
        assert len(starts) == 5 * 25
        overnight = [
            ts for ts in starts if ts.time() > dt.time(15, 15) or ts.time() < dt.time(9, 15)
        ]
        assert overnight == []

    def test_consecutive_bars_across_a_day_boundary_are_not_one_bar_apart(
        self, calendar: TradingCalendar
    ) -> None:
        # A naive "index + 1 bar == +15 minutes" assumption is exactly what
        # produces indicators that treat the overnight gap as a price move.
        starts = calendar.expected_bar_starts(
            dt.date(2025, 8, 4), dt.date(2025, 8, 5), Timeframe.M15
        )
        gaps = {b - a for a, b in pairwise(starts)}
        assert dt.timedelta(minutes=15) in gaps
        overnight_gaps = gaps - {dt.timedelta(minutes=15)}
        assert len(overnight_gaps) == 1
        assert overnight_gaps.pop() > dt.timedelta(hours=17)

    def test_a_weekend_is_skipped_entirely(self, calendar: TradingCalendar) -> None:
        starts = calendar.expected_bar_starts(
            dt.date(2025, 8, 8), dt.date(2025, 8, 11), Timeframe.D1
        )
        assert [ts.date() for ts in starts] == [dt.date(2025, 8, 8), dt.date(2025, 8, 11)]

    def test_holidays_are_absent_from_the_grid(self, calendar: TradingCalendar) -> None:
        starts = calendar.expected_bar_starts(
            dt.date(2025, 8, 14), dt.date(2025, 8, 18), Timeframe.D1
        )
        assert INDEPENDENCE_DAY not in {ts.date() for ts in starts}


class TestSpecialSessions:
    def test_muhurat_is_not_a_regular_trading_day_but_has_a_session(
        self, calendar: TradingCalendar
    ) -> None:
        assert not calendar.is_trading_day(MUHURAT_2025)
        assert calendar.has_any_session(MUHURAT_2025)

    def test_muhurat_window_and_bar_grid(self, calendar: TradingCalendar) -> None:
        schedule = calendar.schedule(MUHURAT_2025)
        assert schedule.is_special
        assert schedule.label is not None
        assert "Muhurat" in schedule.label
        assert schedule.open == at(MUHURAT_2025, 13, 45)
        assert schedule.close == at(MUHURAT_2025, 14, 45)
        # One hour, so four 15m bars — not the usual 25.
        assert calendar.bars_per_session(MUHURAT_2025, Timeframe.M15) == 4

    def test_muhurat_is_excluded_from_regular_ranges(self, calendar: TradingCalendar) -> None:
        days = calendar.trading_days(dt.date(2025, 10, 20), dt.date(2025, 10, 23))
        assert MUHURAT_2025 not in days
        with_special = calendar.trading_days(
            dt.date(2025, 10, 20), dt.date(2025, 10, 23), include_special=True
        )
        assert MUHURAT_2025 in with_special


class TestReferenceData:
    def test_no_holiday_row_falls_on_a_weekend(self, calendar: TradingCalendar) -> None:
        # A weekend "holiday" is a data-entry error: harmless but a signal that
        # the row was not checked against a real circular.
        weekend_rows = [
            record
            for record in calendar.holidays_in(dt.date(2019, 1, 1), dt.date(2026, 12, 31))
            if record.date.weekday() >= 5
        ]
        assert weekend_rows == []

    def test_holiday_file_covers_the_configured_history_window(
        self, calendar: TradingCalendar
    ) -> None:
        for year in range(2019, 2026):
            found = calendar.holidays_in(dt.date(year, 1, 1), dt.date(year, 12, 31))
            assert len(found) >= 10, f"{year} has only {len(found)} holidays on file"


class TestTradeableSpecialSessions:
    """A weekend session that the exchange actually runs is a trading day.

    NSE holds a full 09:15-15:30 session on the Saturday or Sunday a Union
    Budget is presented. Those days carry enormous volume and enormous
    information; classifying by weekday alone drops them from every backtest
    without a word. Muhurat is the opposite case and must stay excluded --
    hence the decision keys off the session's own `tradeable` flag rather than
    off it merely being special.
    """

    def test_a_budget_saturday_is_a_trading_day(self, calendar) -> None:
        import datetime as dt

        budget = dt.date(2025, 2, 1)
        assert budget.weekday() == 5  # Saturday
        assert calendar.is_weekend(budget)
        assert calendar.is_trading_day(budget)
        assert calendar.bars_per_session(budget, Timeframe.M15) == 25

    def test_muhurat_is_still_not_a_trading_day(self, calendar) -> None:
        import datetime as dt

        muhurat = dt.date(2024, 11, 1)
        assert calendar.special_session(muhurat) is not None
        assert not calendar.is_trading_day(muhurat)
        assert calendar.has_any_session(muhurat)

    def test_a_dr_drill_is_not_tradeable(self, calendar) -> None:
        import datetime as dt

        drill = dt.date(2024, 3, 2)
        assert calendar.special_session(drill) is not None
        assert not calendar.is_trading_day(drill)
