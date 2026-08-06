"""Parquet bar store: round trips, upserts, partitioning and resume."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from nifty50.data.store import BarStore
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import IST, Exchange, Timeframe
from nifty50.trading_calendar import TradingCalendar

WEEK_START = dt.date(2025, 8, 4)
WEEK_END = dt.date(2025, 8, 8)


@pytest.fixture
def week(calendar: TradingCalendar) -> pd.DataFrame:
    return generate_session_bars(
        calendar, WEEK_START, WEEK_END, Timeframe.M15, start_price=1400.0, seed=3
    )


class TestRoundTrip:
    def test_write_then_read_preserves_values_and_timezone(
        self, store: BarStore, week: pd.DataFrame
    ) -> None:
        written = store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        assert written == len(week)

        read_back = store.read(Exchange.NSE, "RELIANCE", Timeframe.M15)
        assert len(read_back) == len(week)
        assert str(read_back.index.tz) == str(IST)
        pd.testing.assert_series_equal(read_back["close"], week["close"], check_freq=False)
        assert read_back.index[0] == week.index[0]

    def test_reading_an_unknown_symbol_returns_an_empty_tz_aware_frame(
        self, store: BarStore
    ) -> None:
        frame = store.read(Exchange.NSE, "NOSUCH", Timeframe.M15)
        assert frame.empty
        assert frame.index.tz is not None

    def test_range_filter(self, store: BarStore, week: pd.DataFrame) -> None:
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        start = dt.datetime(2025, 8, 6, 9, 15, tzinfo=IST)
        end = dt.datetime(2025, 8, 6, 15, 15, tzinfo=IST)
        subset = store.read(Exchange.NSE, "RELIANCE", Timeframe.M15, start=start, end=end)
        assert len(subset) == 25
        assert {ts.date() for ts in subset.index} == {dt.date(2025, 8, 6)}


class TestUpsert:
    def test_a_later_write_wins_on_a_timestamp_collision(
        self, store: BarStore, week: pd.DataFrame
    ) -> None:
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        corrected = week.iloc[:1].copy()
        corrected["close"] = 9999.0
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, corrected)

        read_back = store.read(Exchange.NSE, "RELIANCE", Timeframe.M15)
        # The authoritative REST candle replaces the locally-assembled one, and
        # the bar count does not grow.
        assert len(read_back) == len(week)
        assert read_back["close"].iloc[0] == pytest.approx(9999.0)

    def test_duplicate_timestamps_within_one_write_are_collapsed(
        self, store: BarStore, week: pd.DataFrame
    ) -> None:
        doubled = pd.concat([week, week])
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, doubled)
        assert len(store.read(Exchange.NSE, "RELIANCE", Timeframe.M15)) == len(week)


class TestPartitioning:
    def test_partitions_are_monthly_not_daily(
        self, store: BarStore, calendar: TradingCalendar
    ) -> None:
        # Two months of daily bars must land in exactly two partition files, not
        # ~40. See the module docstring on why per-date partitioning is wrong here.
        frame = generate_session_bars(
            calendar, dt.date(2025, 7, 1), dt.date(2025, 8, 31), Timeframe.D1, seed=1
        )
        store.write(Exchange.NSE, "RELIANCE", Timeframe.D1, frame)
        parts = list(store.partition_dir(Exchange.NSE, "RELIANCE", Timeframe.D1).glob("ym=*"))
        assert sorted(p.name for p in parts) == ["ym=202507", "ym=202508"]

    def test_symbols_with_awkward_characters_are_path_safe(
        self, store: BarStore, week: pd.DataFrame
    ) -> None:
        # M&M is a real Nifty 50 constituent and '&' is hostile in a path.
        store.write(Exchange.NSE, "M&M", Timeframe.M15, week)
        assert len(store.read(Exchange.NSE, "M&M", Timeframe.M15)) == len(week)
        assert "M_M" in store.symbols(Exchange.NSE)

    def test_timeframes_are_isolated(self, store: BarStore, week: pd.DataFrame) -> None:
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        assert store.read(Exchange.NSE, "RELIANCE", Timeframe.H1).empty


class TestCoverage:
    def test_coverage_reports_the_stored_span(self, store: BarStore, week: pd.DataFrame) -> None:
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        coverage = store.coverage(Exchange.NSE, "RELIANCE", Timeframe.M15)
        assert coverage.bar_count == len(week)
        assert coverage.first_ts == week.index[0]
        assert coverage.last_ts == week.index[-1]

    def test_empty_coverage(self, store: BarStore) -> None:
        coverage = store.coverage(Exchange.NSE, "NOSUCH", Timeframe.M15)
        assert coverage.is_empty
        assert coverage.last_ts is None

    def test_last_bar_ts_drives_resume(self, store: BarStore, week: pd.DataFrame) -> None:
        store.write(Exchange.NSE, "RELIANCE", Timeframe.M15, week)
        assert store.last_bar_ts(Exchange.NSE, "RELIANCE", Timeframe.M15) == week.index[-1]

    def test_coverage_spans_multiple_partitions(
        self, store: BarStore, calendar: TradingCalendar
    ) -> None:
        frame = generate_session_bars(
            calendar, dt.date(2025, 6, 2), dt.date(2025, 8, 29), Timeframe.D1, seed=5
        )
        store.write(Exchange.NSE, "RELIANCE", Timeframe.D1, frame)
        coverage = store.coverage(Exchange.NSE, "RELIANCE", Timeframe.D1)
        assert coverage.bar_count == len(frame)
        assert coverage.first_ts == frame.index[0]
        assert coverage.last_ts == frame.index[-1]


class TestValidation:
    def test_naive_index_is_rejected(self, store: BarStore) -> None:
        frame = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
            index=pd.DatetimeIndex([dt.datetime(2025, 8, 4, 9, 15)], name="ts"),
        )
        with pytest.raises(ValueError, match="tz-aware"):
            store.write(Exchange.NSE, "X", Timeframe.M15, frame)

    def test_missing_columns_are_rejected(self, store: BarStore) -> None:
        frame = pd.DataFrame(
            {"close": [1.0]},
            index=pd.DatetimeIndex([dt.datetime(2025, 8, 4, 9, 15, tzinfo=IST)], name="ts"),
        )
        with pytest.raises(ValueError, match="missing columns"):
            store.write(Exchange.NSE, "X", Timeframe.M15, frame)

    def test_writing_an_empty_frame_is_a_no_op(self, store: BarStore) -> None:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz=IST, name="ts")
        assert store.write(Exchange.NSE, "X", Timeframe.M15, empty) == 0
