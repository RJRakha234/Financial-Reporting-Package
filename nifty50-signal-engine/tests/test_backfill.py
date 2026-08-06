"""Backfill: chunk planning against vendor caps, resume, and error handling."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from nifty50.config import Config
from nifty50.data.backfill import Backfiller, daily_from_intraday
from nifty50.data.brokers.replay import ReplayAdapter
from nifty50.data.store import BarStore
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import Exchange, Instrument, InstrumentKind, Timeframe
from nifty50.trading_calendar import TradingCalendar

MONDAY = dt.date(2025, 8, 4)
FRIDAY = dt.date(2025, 8, 8)


class _CappedAdapter(ReplayAdapter):
    """Replay adapter that advertises Kite's per-timeframe request caps."""

    def __init__(self, *args: object, caps: dict[Timeframe, int], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._caps = caps
        self.requests: list[tuple[dt.datetime, dt.datetime]] = []

    def historical_chunk_days(self, timeframe: Timeframe) -> int:
        return self._caps[timeframe]

    def historical_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> pd.DataFrame:
        self.requests.append((start, end))
        return super().historical_candles(instrument, timeframe, start, end)


@pytest.fixture
def seeded(config: Config, calendar: TradingCalendar) -> ReplayAdapter:
    root = config.path(config.broker.replay.root)
    directory = root / "NSE" / "RELIANCE"
    directory.mkdir(parents=True, exist_ok=True)
    for timeframe in (Timeframe.M15, Timeframe.D1):
        generate_session_bars(
            calendar, dt.date(2025, 6, 2), FRIDAY, timeframe, start_price=1400.0, seed=8
        ).to_parquet(directory / f"{timeframe.value}.parquet")
    adapter = ReplayAdapter(config, root=root)
    adapter.authenticate()
    return adapter


@pytest.fixture
def instrument() -> Instrument:
    return Instrument("RELIANCE", Exchange.NSE, InstrumentKind.EQUITY, broker_token=1)


class TestPlanning:
    def test_chunks_never_exceed_the_vendor_cap(
        self, config: Config, calendar: TradingCalendar, instrument: Instrument, store: BarStore
    ) -> None:
        adapter = _CappedAdapter(config, caps={Timeframe.M1: 60})
        planner = Backfiller(adapter, store, calendar, config)
        requests = planner.plan(
            instrument, Timeframe.M1, dt.date(2024, 1, 1), dt.date(2024, 12, 31)
        )
        assert requests
        for request in requests:
            span = (request.end.date() - request.start.date()).days
            assert span <= 60

    def test_chunk_boundaries_land_on_trading_days(
        self, config: Config, calendar: TradingCalendar, instrument: Instrument, store: BarStore
    ) -> None:
        adapter = _CappedAdapter(config, caps={Timeframe.D1: 30})
        planner = Backfiller(adapter, store, calendar, config)
        requests = planner.plan(instrument, Timeframe.D1, dt.date(2025, 8, 2), dt.date(2025, 8, 31))
        for request in requests:
            assert calendar.is_trading_day(request.start.date())
            assert calendar.is_trading_day(request.end.date())
            # Requests are aligned to real session windows, not to midnight.
            assert request.start.time() == dt.time(9, 15)
            assert request.end.time() == dt.time(15, 30)

    def test_a_window_containing_no_trading_days_produces_no_request(
        self, config: Config, calendar: TradingCalendar, instrument: Instrument, store: BarStore
    ) -> None:
        adapter = _CappedAdapter(config, caps={Timeframe.D1: 2})
        planner = Backfiller(adapter, store, calendar, config)
        # A weekend on its own: nothing to ask for.
        assert (
            planner.plan(instrument, Timeframe.D1, dt.date(2025, 8, 9), dt.date(2025, 8, 10)) == []
        )

    def test_default_start_honours_the_configured_history_window(
        self, config: Config, calendar: TradingCalendar, store: BarStore, seeded: ReplayAdapter
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        start = planner.default_start(dt.date(2026, 1, 1))
        assert start.year == 2026 - config.data.history_years


class TestExecution:
    def test_backfill_writes_to_the_store(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        seeded: ReplayAdapter,
        instrument: Instrument,
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        result = planner.backfill(instrument, Timeframe.M15, start=MONDAY, end=FRIDAY)
        assert result.ok
        assert result.bars_written == 5 * 25
        assert store.coverage(Exchange.NSE, "RELIANCE", Timeframe.M15).bar_count == 125

    def test_resume_restarts_from_the_last_stored_day(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        seeded: ReplayAdapter,
        instrument: Instrument,
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=dt.date(2025, 8, 6))
        # Re-fetches Wednesday deliberately: vendors revise the tail of a session
        # after the close, so the last stored day is treated as provisional.
        assert planner.resume_point(instrument, Timeframe.D1, MONDAY) == dt.date(2025, 8, 6)

        second = planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=FRIDAY)
        assert second.first_ts is not None
        assert second.first_ts.date() == dt.date(2025, 8, 6)
        assert store.coverage(Exchange.NSE, "RELIANCE", Timeframe.D1).bar_count == 5

    def test_resume_can_be_disabled(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        seeded: ReplayAdapter,
        instrument: Instrument,
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=dt.date(2025, 8, 6))
        result = planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=FRIDAY, resume=False)
        assert result.first_ts is not None
        assert result.first_ts.date() == MONDAY

    def test_an_up_to_date_store_is_skipped(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        seeded: ReplayAdapter,
        instrument: Instrument,
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=FRIDAY)
        result = planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=dt.date(2025, 8, 7))
        assert result.skipped
        assert "already current" in result.skip_reason

    def test_a_failing_chunk_is_recorded_and_the_rest_continue(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        instrument: Instrument,
    ) -> None:
        class Flaky(ReplayAdapter):
            calls = 0

            def historical_chunk_days(self, timeframe: Timeframe) -> int:
                return 2

            def historical_candles(self, *args: object, **kwargs: object) -> pd.DataFrame:
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("vendor 503")
                return super().historical_candles(*args, **kwargs)  # type: ignore[arg-type]

        adapter = Flaky(config, root=config.path(config.broker.replay.root))
        planner = Backfiller(adapter, store, calendar, config)
        result = planner.backfill(instrument, Timeframe.D1, start=MONDAY, end=FRIDAY)
        assert not result.ok
        assert len(result.errors) == 1
        assert "vendor 503" in result.errors[0]
        # The loop kept going rather than abandoning the whole symbol.
        assert result.requests_made >= 1

    def test_backfill_many_covers_the_cross_product(
        self,
        config: Config,
        calendar: TradingCalendar,
        store: BarStore,
        seeded: ReplayAdapter,
        instrument: Instrument,
    ) -> None:
        planner = Backfiller(seeded, store, calendar, config)
        results = planner.backfill_many(
            [instrument], [Timeframe.M15, Timeframe.D1], start=MONDAY, end=FRIDAY
        )
        assert len(results) == 2
        assert all(r.ok for r in results)


class TestDailyRollup:
    def test_intraday_rolls_up_to_session_bars(self, calendar: TradingCalendar) -> None:
        intraday = generate_session_bars(
            calendar, MONDAY, FRIDAY, Timeframe.M15, start_price=1000.0, seed=6
        )
        daily = daily_from_intraday(intraday, calendar)
        assert len(daily) == 5
        monday = intraday[[ts.date() == MONDAY for ts in intraday.index]]
        assert daily["open"].iloc[0] == pytest.approx(monday["open"].iloc[0])
        assert daily["close"].iloc[0] == pytest.approx(monday["close"].iloc[-1])
        assert daily["high"].iloc[0] == pytest.approx(monday["high"].max())
        assert daily["volume"].iloc[0] == monday["volume"].sum()
        # Session bars are stamped at the open, matching the calendar's grid.
        assert daily.index[0] == calendar.schedule(MONDAY).continuous.start
