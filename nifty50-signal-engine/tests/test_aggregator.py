"""Tick-to-candle aggregation, with the session boundary as the central concern."""

from __future__ import annotations

import datetime as dt

import pytest

from nifty50.data.aggregator import CandleAggregator, RejectionReason
from nifty50.domain import IST, Tick, Timeframe
from nifty50.trading_calendar import TradingCalendar

MONDAY = dt.date(2025, 8, 4)
TUESDAY = dt.date(2025, 8, 5)
HOLIDAY = dt.date(2025, 8, 15)
KEY = "NSE:RELIANCE"


def tick(
    day: dt.date,
    hour: int,
    minute: int,
    price: float,
    *,
    cumulative_volume: int | None = None,
    key: str = KEY,
    second: int = 0,
) -> Tick:
    return Tick(
        instrument_key=key,
        ts=dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST),
        last_price=price,
        volume_traded_today=cumulative_volume,
    )


@pytest.fixture
def aggregator(calendar: TradingCalendar) -> CandleAggregator:
    return CandleAggregator(calendar, Timeframe.M15)


class TestBarConstruction:
    def test_ticks_within_one_bar_build_its_ohlc(self, aggregator: CandleAggregator) -> None:
        for minute, price in ((15, 100.0), (18, 105.0), (22, 95.0), (29, 102.0)):
            assert aggregator.on_tick(tick(MONDAY, 9, minute, price)) is None
        pending = aggregator.in_progress(KEY)
        assert pending is not None
        assert (pending.open, pending.high, pending.low, pending.close) == (
            100.0,
            105.0,
            95.0,
            102.0,
        )
        assert not pending.closed

    def test_crossing_a_bar_boundary_emits_the_completed_bar(
        self, aggregator: CandleAggregator
    ) -> None:
        aggregator.on_tick(tick(MONDAY, 9, 20, 100.0))
        emitted = aggregator.on_tick(tick(MONDAY, 9, 31, 101.0))
        assert emitted is not None
        assert emitted.closed
        assert emitted.ts == dt.datetime(2025, 8, 4, 9, 15, tzinfo=IST)
        assert emitted.close == 100.0
        # The new bar is in progress, not emitted.
        assert aggregator.in_progress(KEY).ts == dt.datetime(2025, 8, 4, 9, 30, tzinfo=IST)

    def test_bars_are_start_stamped(self, aggregator: CandleAggregator) -> None:
        aggregator.on_tick(tick(MONDAY, 10, 44, 100.0))
        pending = aggregator.in_progress(KEY)
        assert pending is not None
        assert pending.ts == dt.datetime(2025, 8, 4, 10, 30, tzinfo=IST)

    def test_flush_closes_the_final_bar_of_the_day(self, aggregator: CandleAggregator) -> None:
        # 15:29 is the last tick of the session; no later tick will ever arrive to
        # push the bar out, so the session close has to do it.
        aggregator.on_tick(tick(MONDAY, 15, 29, 100.0))
        candles = aggregator.flush_all()
        assert len(candles) == 1
        assert candles[0].ts == dt.datetime(2025, 8, 4, 15, 15, tzinfo=IST)
        assert candles[0].closed

    def test_multiple_instruments_are_tracked_independently(
        self, aggregator: CandleAggregator
    ) -> None:
        aggregator.on_tick(tick(MONDAY, 9, 20, 100.0, key="NSE:RELIANCE"))
        aggregator.on_tick(tick(MONDAY, 9, 20, 50.0, key="NSE:INFY"))
        assert aggregator.in_progress("NSE:RELIANCE").close == 100.0
        assert aggregator.in_progress("NSE:INFY").close == 50.0


class TestVolume:
    def test_volume_is_a_difference_of_cumulative_totals(
        self, aggregator: CandleAggregator
    ) -> None:
        """Feeds send the day's running total, not the size of the last print."""
        aggregator.on_tick(tick(MONDAY, 9, 16, 100.0, cumulative_volume=1_000))
        aggregator.on_tick(tick(MONDAY, 9, 25, 101.0, cumulative_volume=3_000))
        first = aggregator.on_tick(tick(MONDAY, 9, 31, 102.0, cumulative_volume=3_500))
        assert first is not None
        # First bar of the session: everything traded so far belongs to it.
        assert first.volume == 3_000
        assert not first.volume_estimated

        second = aggregator.on_tick(tick(MONDAY, 9, 46, 103.0, cumulative_volume=5_000))
        assert second is not None
        # Not 5,000: the second bar only owns what traded inside it.
        assert second.volume == 3_500 - 3_000

    def test_a_cold_start_flags_volume_as_estimated(self, aggregator: CandleAggregator) -> None:
        # Attaching at 11:00 means the cumulative baseline is unknown; reporting
        # the morning's entire turnover as one bar would be badly wrong.
        aggregator.attach_mid_session(KEY)
        aggregator.on_tick(tick(MONDAY, 11, 5, 100.0, cumulative_volume=9_000_000))
        emitted = aggregator.on_tick(tick(MONDAY, 11, 20, 101.0, cumulative_volume=9_010_000))
        assert emitted is not None
        assert emitted.volume_estimated
        assert emitted.volume == 0  # rebased on attach, so nothing is claimed

    def test_volume_falls_back_to_summed_prints_when_no_cumulative_is_sent(
        self, calendar: TradingCalendar
    ) -> None:
        aggregator = CandleAggregator(calendar, Timeframe.M15)
        for minute in (16, 20, 25):
            aggregator.on_tick(
                Tick(
                    instrument_key=KEY,
                    ts=dt.datetime(2025, 8, 4, 9, minute, tzinfo=IST),
                    last_price=100.0,
                    last_quantity=10,
                )
            )
        pending = aggregator.in_progress(KEY)
        assert pending is not None
        assert pending.volume == 30


class TestSessionBoundaries:
    def test_a_pre_open_tick_is_rejected(self, aggregator: CandleAggregator) -> None:
        assert aggregator.on_tick(tick(MONDAY, 9, 5, 100.0)) is None
        assert aggregator.stats.rejected[RejectionReason.OUTSIDE_SESSION] == 1

    def test_a_closing_session_tick_is_rejected(self, aggregator: CandleAggregator) -> None:
        # 15:47 is a real print, but it belongs to the closing session. Rounding
        # it into the 15:15 bar would contaminate the continuous tape.
        assert aggregator.on_tick(tick(MONDAY, 15, 47, 100.0)) is None
        assert aggregator.stats.rejected[RejectionReason.OUTSIDE_SESSION] == 1

    def test_a_tick_on_a_holiday_is_rejected(self, aggregator: CandleAggregator) -> None:
        assert aggregator.on_tick(tick(HOLIDAY, 11, 0, 100.0)) is None
        assert aggregator.stats.rejected[RejectionReason.NON_TRADING_DAY] == 1

    def test_no_bar_spans_the_overnight_gap(self, aggregator: CandleAggregator) -> None:
        """The load-bearing session-boundary test.

        The last bar of Monday and the first bar of Tuesday must be two separate
        bars, seventeen and a half hours apart — not one bar rolled across the
        gap.
        """
        aggregator.on_tick(tick(MONDAY, 15, 20, 100.0, cumulative_volume=5_000))
        emitted = aggregator.on_tick(tick(TUESDAY, 9, 16, 120.0, cumulative_volume=800))
        assert emitted is not None
        assert emitted.ts == dt.datetime(2025, 8, 4, 15, 15, tzinfo=IST)
        assert emitted.close == 100.0

        pending = aggregator.in_progress(KEY)
        assert pending is not None
        assert pending.ts == dt.datetime(2025, 8, 5, 9, 15, tzinfo=IST)
        assert pending.open == 120.0
        # The 20% overnight move is a gap event between two bars, never inside one.
        assert emitted.high < 120.0

    def test_cumulative_volume_is_rebased_at_the_session_open(
        self, aggregator: CandleAggregator
    ) -> None:
        # Monday's total is far larger than Tuesday's first reading. Without a
        # reset the difference would go negative and clamp to zero.
        aggregator.on_tick(tick(MONDAY, 15, 20, 100.0, cumulative_volume=9_000_000))
        aggregator.on_tick(tick(TUESDAY, 9, 16, 120.0, cumulative_volume=1_000))
        emitted = aggregator.on_tick(tick(TUESDAY, 9, 31, 121.0, cumulative_volume=4_000))
        assert emitted is not None
        assert emitted.volume == 1_000  # not a clamped zero, not nine million


class TestRaggedBars:
    def test_the_final_hourly_bar_is_flagged_partial(self, calendar: TradingCalendar) -> None:
        aggregator = CandleAggregator(calendar, Timeframe.H1)
        aggregator.on_tick(tick(MONDAY, 15, 20, 100.0))
        candles = aggregator.flush_all()
        assert len(candles) == 1
        assert candles[0].ts == dt.datetime(2025, 8, 4, 15, 15, tzinfo=IST)
        assert candles[0].partial

    def test_a_mid_session_hourly_bar_is_not_partial(self, calendar: TradingCalendar) -> None:
        aggregator = CandleAggregator(calendar, Timeframe.H1)
        aggregator.on_tick(tick(MONDAY, 11, 0, 100.0))
        candles = aggregator.flush_all()
        assert not candles[0].partial


class TestOutOfOrder:
    def test_a_tick_for_an_already_closed_bar_is_dropped(
        self, aggregator: CandleAggregator
    ) -> None:
        aggregator.on_tick(tick(MONDAY, 9, 20, 100.0))
        aggregator.on_tick(tick(MONDAY, 9, 35, 101.0))  # closes the 09:15 bar
        assert aggregator.on_tick(tick(MONDAY, 9, 25, 999.0)) is None
        assert aggregator.stats.rejected[RejectionReason.LATE_TICK] == 1
        # The closed bar is not retroactively corrupted.
        assert aggregator.in_progress(KEY).high == 101.0

    def test_a_reordered_tick_within_the_same_bar_does_not_move_the_close(
        self, aggregator: CandleAggregator
    ) -> None:
        aggregator.on_tick(tick(MONDAY, 9, 20, 100.0, second=30))
        aggregator.on_tick(tick(MONDAY, 9, 20, 90.0, second=10))
        pending = aggregator.in_progress(KEY)
        assert pending is not None
        assert pending.close == 100.0
        assert aggregator.stats.rejected[RejectionReason.LATE_TICK] == 1


class TestStats:
    def test_counts_are_tracked(self, aggregator: CandleAggregator) -> None:
        aggregator.on_tick(tick(MONDAY, 9, 20, 100.0))
        aggregator.on_tick(tick(MONDAY, 9, 5, 100.0))
        aggregator.flush_all()
        assert aggregator.stats.accepted == 1
        assert aggregator.stats.total_rejected == 1
        assert aggregator.stats.candles_emitted == 1
