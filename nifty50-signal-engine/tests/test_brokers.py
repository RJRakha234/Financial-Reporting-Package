"""Broker adapter contract, and the structural read-only guarantee."""

from __future__ import annotations

import datetime as dt

import pytest

from nifty50.config import Config
from nifty50.data.brokers import (
    ConnectionState,
    ReadOnlyViolationError,
    StreamCallbacks,
    StreamMode,
    assert_read_only,
    build_adapter,
)
from nifty50.data.brokers.base import BrokerAdapter
from nifty50.data.brokers.replay import ReplayAdapter, ReplayFaults
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import IST, Exchange, Instrument, InstrumentKind, Tick, Timeframe
from nifty50.trading_calendar import TradingCalendar

MONDAY = dt.date(2025, 8, 4)
FRIDAY = dt.date(2025, 8, 8)


@pytest.fixture
def seeded_replay(config: Config, calendar: TradingCalendar) -> ReplayAdapter:
    root = config.path(config.broker.replay.root)
    for symbol, price in (("RELIANCE", 1400.0), ("INFY", 1600.0)):
        directory = root / "NSE" / symbol
        directory.mkdir(parents=True, exist_ok=True)
        for timeframe in (Timeframe.M1, Timeframe.M15, Timeframe.D1):
            frame = generate_session_bars(
                calendar, MONDAY, FRIDAY, timeframe, start_price=price, seed=hash(symbol) % 100
            )
            frame.to_parquet(directory / f"{timeframe.value}.parquet")
    adapter = ReplayAdapter(config, root=root)
    adapter.authenticate()
    return adapter


class TestReadOnlyGuarantee:
    """PART 8: this engine sits outside SEBI's algo perimeter by having no
    execution surface at all. That is enforced here, not just documented."""

    def test_a_class_with_an_order_method_is_rejected(self) -> None:
        class Rogue:
            def place_order(self) -> None: ...

        with pytest.raises(ReadOnlyViolationError, match="place_order"):
            assert_read_only(Rogue)

    @pytest.mark.parametrize(
        "method",
        ["place_order", "modify_order", "cancel_order", "exit_position", "square_off"],
    )
    def test_execution_verbs_are_all_caught(self, method: str) -> None:
        rogue = type("Rogue", (), {method: lambda self: None})
        with pytest.raises(ReadOnlyViolationError):
            assert_read_only(rogue)

    def test_a_broker_subclass_cannot_define_one(self) -> None:
        # __init_subclass__ fires at class-definition time, so this fails on
        # import rather than at runtime in the middle of a session.
        with pytest.raises(ReadOnlyViolationError):

            class RogueAdapter(BrokerAdapter):  # type: ignore[misc]
                def place_order(self) -> None: ...

    def test_the_shipped_adapters_are_clean(self) -> None:
        from nifty50.data.brokers.kite import KiteAdapter

        assert_read_only(ReplayAdapter)
        assert_read_only(KiteAdapter)

    def test_read_only_methods_are_not_false_positives(self) -> None:
        class Fine:
            def historical_candles(self) -> None: ...
            def quote(self) -> None: ...
            def reorder_buffer(self) -> None: ...

        assert_read_only(Fine)


class TestRegistry:
    def test_build_adapter_honours_config(self, config: Config) -> None:
        adapter = build_adapter(config)
        assert isinstance(adapter, ReplayAdapter)
        assert adapter.name == "replay"

    def test_unknown_adapter_is_rejected(self, config: Config) -> None:
        with pytest.raises(ValueError, match="unknown broker adapter"):
            build_adapter(config, "definitely-not-a-broker")


class TestReplayAdapterContract:
    def test_authenticate_and_token_status(self, seeded_replay: ReplayAdapter) -> None:
        status = seeded_replay.token_status()
        assert status.valid
        assert status.expires_at is not None

    def test_injected_auth_failure(self, config: Config) -> None:
        adapter = ReplayAdapter(config, faults=ReplayFaults(fail_authentication=True))
        assert not adapter.authenticate().valid

    def test_instrument_discovery_and_resolution(self, seeded_replay: ReplayAdapter) -> None:
        instruments = seeded_replay.list_instruments(Exchange.NSE)
        assert {i.symbol for i in instruments} == {"RELIANCE", "INFY"}
        resolved = seeded_replay.resolve("RELIANCE", Exchange.NSE)
        assert resolved.key == "NSE:RELIANCE"
        with pytest.raises(KeyError):
            seeded_replay.resolve("NOSUCH", Exchange.NSE)

    def test_historical_candles_are_tz_aware_and_range_filtered(
        self, seeded_replay: ReplayAdapter
    ) -> None:
        instrument = seeded_replay.resolve("RELIANCE", Exchange.NSE)
        frame = seeded_replay.historical_candles(
            instrument,
            Timeframe.M15,
            dt.datetime(2025, 8, 5, 9, 15, tzinfo=IST),
            dt.datetime(2025, 8, 5, 15, 15, tzinfo=IST),
        )
        assert len(frame) == 25
        assert frame.index.tz is not None
        assert {ts.date() for ts in frame.index} == {dt.date(2025, 8, 5)}

    def test_quote_returns_a_full_tick_with_depth(self, seeded_replay: ReplayAdapter) -> None:
        instrument = seeded_replay.resolve("RELIANCE", Exchange.NSE)
        quotes = seeded_replay.quote([instrument])
        tick = quotes["NSE:RELIANCE"]
        assert isinstance(tick, Tick)
        assert tick.best_bid is not None
        assert tick.best_ask is not None
        assert tick.spread is not None
        assert tick.spread > 0

    def test_subscription_requires_an_open_stream(self, seeded_replay: ReplayAdapter) -> None:
        instrument = seeded_replay.resolve("RELIANCE", Exchange.NSE)
        with pytest.raises(RuntimeError, match="stream is not open"):
            seeded_replay.subscribe([instrument], StreamMode.FULL)

    def test_subscription_cap_is_enforced(self, seeded_replay: ReplayAdapter) -> None:
        seeded_replay.open_stream(StreamCallbacks())
        too_many = [
            Instrument(f"SYM{i}", Exchange.NSE, InstrumentKind.EQUITY, broker_token=i)
            for i in range(seeded_replay.max_stream_instruments + 1)
        ]
        with pytest.raises(ValueError, match="exceeds the cap"):
            seeded_replay.subscribe(too_many, StreamMode.LTP)

    def test_pump_delivers_ticks_to_the_callback(self, seeded_replay: ReplayAdapter) -> None:
        received: list[Tick] = []
        seeded_replay.open_stream(StreamCallbacks(on_ticks=received.extend))
        seeded_replay.subscribe([seeded_replay.resolve("RELIANCE", Exchange.NSE)], StreamMode.FULL)
        delivered = seeded_replay.pump(MONDAY)
        assert delivered == 375  # one minute bar per minute of the session
        assert len(received) == 375
        assert all(t.instrument_key == "NSE:RELIANCE" for t in received)

    def test_injected_drop_closes_the_stream_mid_pump(
        self, config: Config, calendar: TradingCalendar
    ) -> None:
        root = config.path(config.broker.replay.root)
        directory = root / "NSE" / "RELIANCE"
        directory.mkdir(parents=True, exist_ok=True)
        generate_session_bars(calendar, MONDAY, MONDAY, Timeframe.M1, seed=1).to_parquet(
            directory / "1m.parquet"
        )
        adapter = ReplayAdapter(config, root=root, faults=ReplayFaults(drop_after_ticks=100))
        adapter.authenticate()
        closes: list[tuple[int | None, str | None]] = []
        adapter.open_stream(
            StreamCallbacks(on_close=lambda code, reason: closes.append((code, reason)))
        )
        adapter.subscribe([adapter.resolve("RELIANCE", Exchange.NSE)], StreamMode.LTP)
        delivered = adapter.pump(MONDAY)
        assert delivered == 100
        assert closes == [(1006, "injected drop")]
        assert adapter.connection_state is ConnectionState.DISCONNECTED
