"""Stream supervisor: backoff, staleness, degradation and idling when shut."""

from __future__ import annotations

import asyncio
import datetime as dt
import random

import pytest

from nifty50.config import Config
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.base import StreamCallbacks, StreamMode
from nifty50.data.brokers.replay import ReplayAdapter
from nifty50.data.stream import (
    EngineState,
    StreamSupervisor,
    desired_state_for_phase,
    is_heartbeat_stale,
    next_backoff,
    should_fall_back_to_rest,
)
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import IST, Exchange, Instrument, InstrumentKind, SessionPhase, Timeframe
from nifty50.trading_calendar import TradingCalendar

MONDAY = dt.date(2025, 8, 4)
SATURDAY = dt.date(2025, 8, 9)


def at(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        kwargs = {"initial": 1.0, "multiplier": 2.0, "maximum": 60.0, "jitter_fraction": 0.0}
        assert next_backoff(0, **kwargs) == pytest.approx(1.0)
        assert next_backoff(1, **kwargs) == pytest.approx(2.0)
        assert next_backoff(3, **kwargs) == pytest.approx(8.0)

    def test_is_capped(self) -> None:
        delay = next_backoff(50, initial=1.0, multiplier=2.0, maximum=60.0, jitter_fraction=0.0)
        assert delay == pytest.approx(60.0)

    def test_jitter_stays_within_bounds_and_is_never_negative(self) -> None:
        # Fifty symbols behind one API key all disconnect together; unjittered
        # backoff would reconnect them in the same millisecond.
        rng = random.Random(0)
        for _ in range(200):
            delay = next_backoff(
                2, initial=1.0, multiplier=2.0, maximum=60.0, jitter_fraction=0.25, rng=rng
            )
            assert 3.0 <= delay <= 5.0

    def test_jitter_actually_varies(self) -> None:
        rng = random.Random(1)
        values = {
            round(
                next_backoff(
                    2, initial=1.0, multiplier=2.0, maximum=60.0, jitter_fraction=0.25, rng=rng
                ),
                6,
            )
            for _ in range(20)
        }
        assert len(values) > 1

    def test_negative_attempt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="attempt must be"):
            next_backoff(-1, initial=1.0, multiplier=2.0, maximum=60.0, jitter_fraction=0.0)


class TestStalenessAndFallback:
    def test_heartbeat_staleness(self) -> None:
        now = at(MONDAY, 11, 0)
        assert not is_heartbeat_stale(now - dt.timedelta(seconds=5), now, 10)
        assert is_heartbeat_stale(now - dt.timedelta(seconds=15), now, 10)

    def test_no_ticks_yet_is_not_stale(self) -> None:
        # Before the first tick there is nothing to be stale about; alarming here
        # would fire every morning at 09:15.
        assert not is_heartbeat_stale(None, at(MONDAY, 9, 15), 10)

    def test_rest_fallback_threshold(self) -> None:
        now = at(MONDAY, 11, 0)
        assert not should_fall_back_to_rest(now - dt.timedelta(seconds=10), now, 30)
        assert should_fall_back_to_rest(now - dt.timedelta(seconds=45), now, 30)
        assert not should_fall_back_to_rest(None, now, 30)

    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            (SessionPhase.CONTINUOUS, EngineState.LIVE),
            (SessionPhase.PRE_OPEN, EngineState.PRE_OPEN),
            (SessionPhase.POST_CLOSE, EngineState.IDLE_MARKET_CLOSED),
            (SessionPhase.CLOSED, EngineState.IDLE_MARKET_CLOSED),
        ],
    )
    def test_desired_state(self, phase: SessionPhase, expected: EngineState) -> None:
        assert desired_state_for_phase(phase) is expected


@pytest.fixture
def adapter(config: Config, calendar: TradingCalendar) -> ReplayAdapter:
    root = config.path(config.broker.replay.root)
    directory = root / "NSE" / "RELIANCE"
    directory.mkdir(parents=True, exist_ok=True)
    generate_session_bars(
        calendar, MONDAY, MONDAY, Timeframe.M1, start_price=1400.0, seed=9
    ).to_parquet(directory / "1m.parquet")
    built = ReplayAdapter(config, root=root)
    built.authenticate()
    return built


@pytest.fixture
def instrument() -> Instrument:
    return Instrument("RELIANCE", Exchange.NSE, InstrumentKind.EQUITY, broker_token=1)


class TestSupervisor:
    def test_subscription_cap_is_enforced_up_front(
        self, adapter: ReplayAdapter, calendar: TradingCalendar, config: Config
    ) -> None:
        supervisor = StreamSupervisor(adapter, calendar, config)
        too_many = [
            Instrument(f"S{i}", Exchange.NSE, InstrumentKind.EQUITY, broker_token=i)
            for i in range(adapter.max_stream_instruments + 1)
        ]
        with pytest.raises(ValueError, match="exceeds the vendor websocket cap"):
            supervisor.subscribe(too_many, StreamMode.FULL)

    def test_it_idles_instead_of_alarming_when_the_market_is_shut(
        self, adapter: ReplayAdapter, calendar: TradingCalendar, config: Config
    ) -> None:
        """An engine that alarms every night gets ignored during the day."""
        quiet = config.stream.model_copy(update={"idle_poll_seconds_when_closed": 0.001})
        supervisor = StreamSupervisor(
            adapter,
            calendar,
            config.model_copy(update={"stream": quiet}),
            clock=lambda: at(SATURDAY, 11, 0),
        )
        asyncio.run(supervisor.run(max_iterations=3))
        assert supervisor.health.state is EngineState.IDLE_MARKET_CLOSED
        assert supervisor.health.error_count == 0
        assert supervisor.health.session_phase is SessionPhase.CLOSED

    def test_pre_open_is_its_own_state(
        self, adapter: ReplayAdapter, calendar: TradingCalendar, config: Config
    ) -> None:
        quiet = config.stream.model_copy(update={"idle_poll_seconds_when_closed": 0.001})
        supervisor = StreamSupervisor(
            adapter,
            calendar,
            config.model_copy(update={"stream": quiet}),
            clock=lambda: at(MONDAY, 9, 5),
        )
        asyncio.run(supervisor.run(max_iterations=2))
        assert supervisor.health.state is EngineState.PRE_OPEN

    def test_it_connects_and_subscribes_during_the_session(
        self,
        adapter: ReplayAdapter,
        calendar: TradingCalendar,
        config: Config,
        instrument: Instrument,
    ) -> None:
        supervisor = StreamSupervisor(adapter, calendar, config, clock=lambda: at(MONDAY, 11, 0))
        supervisor.subscribe([instrument], StreamMode.FULL)
        asyncio.run(supervisor.run(max_iterations=2))
        assert supervisor.health.state in (EngineState.LIVE, EngineState.STALE)
        assert supervisor.health.subscribed_instruments == 1
        assert supervisor.health.token_expires_at is not None

    def test_ticks_flow_through_to_candles(
        self,
        adapter: ReplayAdapter,
        calendar: TradingCalendar,
        config: Config,
        instrument: Instrument,
    ) -> None:
        aggregator = CandleAggregator(calendar, Timeframe.M15)
        supervisor = StreamSupervisor(
            adapter,
            calendar,
            config,
            aggregators=[aggregator],
            clock=lambda: at(MONDAY, 11, 0),
        )
        supervisor.subscribe([instrument], StreamMode.FULL)

        async def scenario() -> None:
            # One step connects and subscribes; then pump a session's worth of
            # ticks through the adapter and let the next step drain them.
            await supervisor.step()
            adapter.pump(MONDAY)
            await supervisor.step()

        asyncio.run(scenario())
        # 375 one-minute bars replayed as ticks -> 24 completed 15m bars, with the
        # 25th still in progress until the session close flushes it.
        assert supervisor.health.ticks_received == 375
        assert supervisor.health.candles_emitted == 24
        assert aggregator.in_progress("NSE:RELIANCE") is not None

    def test_rest_fallback_poll_keeps_bars_forming(
        self,
        adapter: ReplayAdapter,
        calendar: TradingCalendar,
        config: Config,
        instrument: Instrument,
    ) -> None:
        supervisor = StreamSupervisor(adapter, calendar, config, clock=lambda: at(MONDAY, 11, 0))
        supervisor.subscribe([instrument], StreamMode.FULL)
        ticks = asyncio.run(supervisor.poll_rest_once())
        assert len(ticks) == 1
        assert supervisor.health.ticks_received == 1

    def test_a_connect_failure_is_recorded_and_retried_not_raised(
        self, calendar: TradingCalendar, config: Config, instrument: Instrument
    ) -> None:
        class Broken(ReplayAdapter):
            def open_stream(self, callbacks: StreamCallbacks) -> None:
                raise RuntimeError("vendor handshake failed")

        fast = config.stream.model_copy(
            update={
                "reconnect_backoff_initial_seconds": 0.001,
                "reconnect_backoff_max_seconds": 0.002,
            }
        )
        supervisor = StreamSupervisor(
            Broken(config, root=config.path(config.broker.replay.root)),
            calendar,
            config.model_copy(update={"stream": fast}),
            clock=lambda: at(MONDAY, 11, 0),
        )
        supervisor.subscribe([instrument], StreamMode.LTP)
        asyncio.run(supervisor.run(max_iterations=3))
        assert supervisor.health.consecutive_failures == 3
        assert "handshake" in supervisor.health.last_error

    def test_it_halts_after_exhausting_the_reconnect_budget(
        self, calendar: TradingCalendar, config: Config, instrument: Instrument
    ) -> None:
        class Broken(ReplayAdapter):
            def open_stream(self, callbacks: StreamCallbacks) -> None:
                raise RuntimeError("down")

        fast = config.stream.model_copy(
            update={
                "reconnect_backoff_initial_seconds": 0.001,
                "reconnect_backoff_max_seconds": 0.001,
                "max_consecutive_reconnects_before_halt": 2,
                "idle_poll_seconds_when_closed": 0.001,
            }
        )
        supervisor = StreamSupervisor(
            Broken(config, root=config.path(config.broker.replay.root)),
            calendar,
            config.model_copy(update={"stream": fast}),
            clock=lambda: at(MONDAY, 11, 0),
        )
        supervisor.subscribe([instrument], StreamMode.LTP)
        asyncio.run(supervisor.run(max_iterations=4))
        # Stops hammering the vendor rather than retrying into a rate-limit ban.
        assert supervisor.health.state is EngineState.HALTED

    def test_the_session_close_flushes_the_final_bar(
        self,
        adapter: ReplayAdapter,
        calendar: TradingCalendar,
        config: Config,
        instrument: Instrument,
    ) -> None:
        aggregator = CandleAggregator(calendar, Timeframe.M15)
        clock = {"now": at(MONDAY, 11, 0)}
        quiet = config.stream.model_copy(update={"idle_poll_seconds_when_closed": 0.001})
        supervisor = StreamSupervisor(
            adapter,
            calendar,
            config.model_copy(update={"stream": quiet}),
            aggregators=[aggregator],
            clock=lambda: clock["now"],
        )
        supervisor.subscribe([instrument], StreamMode.FULL)

        async def scenario() -> None:
            await supervisor.step()
            adapter.pump(MONDAY)
            await supervisor.step()
            assert aggregator.in_progress("NSE:RELIANCE") is not None
            # Roll the clock past the close: no further tick will ever arrive to
            # push the last bar out, so the supervisor must flush it.
            clock["now"] = at(MONDAY, 15, 45)
            await supervisor.step()

        asyncio.run(scenario())
        assert aggregator.in_progress("NSE:RELIANCE") is None
        assert supervisor.health.candles_emitted == 25
