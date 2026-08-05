"""WebSocket manager: stream naming, event parsing, reconnect, health tracking.

A fake socket is injected, so these tests exercise the reconnect/backoff state
machine without a network and without real sleeps.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest

from cse.config import Config
from cse.data.ws import (
    EventType,
    MarketStream,
    WebSocketLike,
    build_stream_names,
)


class FakeSocket(WebSocketLike):
    """Yields a fixed list of frames, then ends (a clean server-side close)."""

    def __init__(self, frames: list[str], *, hang: bool = False) -> None:
        self.frames = frames
        self.hang = hang
        self.closed = False

    async def __aenter__(self) -> FakeSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        for frame in self.frames:
            yield frame
        if self.hang:
            await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


def kline_frame(
    *,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    open_time: int = 1_700_000_000_000,
    is_closed: bool = True,
    close: float = 100.5,
) -> str:
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@kline_{interval}",
            "data": {
                "e": "kline",
                "E": open_time + 1_000,
                "s": symbol,
                "k": {
                    "t": open_time,
                    "T": open_time + 899_999,
                    "s": symbol,
                    "i": interval,
                    "o": "100.0",
                    "c": str(close),
                    "h": "101.0",
                    "l": "99.0",
                    "v": "12.5",
                    "n": 42,
                    "x": is_closed,
                    "q": "1256.25",
                    "V": "7.5",
                    "Q": "753.75",
                },
            },
        }
    )


@pytest.fixture
def fast_ws_config(config: Config) -> Config:
    ws = config.data.websocket.model_copy(
        update={
            "reconnect_initial_seconds": 0.001,
            "reconnect_max_seconds": 0.004,
            "reconnect_jitter": 0.0,
            "rest_fallback_after_seconds": 3600.0,  # off unless a test wants it
        }
    )
    return config.model_copy(update={"data": config.data.model_copy(update={"websocket": ws})})


def make_stream(config: Config, **kwargs: Any) -> MarketStream:
    return MarketStream(
        config.data.websocket,
        ["BTCUSDT"],
        ["15m"],
        rng=random.Random(0),
        **kwargs,
    )


# ---- stream naming ----------------------------------------------------------


def test_stream_names_cover_every_required_stream() -> None:
    names = build_stream_names(
        ["BTCUSDT", "ETHUSDT"], ["1m", "15m"], depth_levels=20, depth_update_ms=100
    )

    assert "btcusdt@kline_1m" in names
    assert "btcusdt@kline_15m" in names
    assert "btcusdt@aggTrade" in names
    assert "btcusdt@depth20@100ms" in names
    assert "btcusdt@ticker" in names
    assert len(names) == 2 * (2 + 3)


def test_stream_url_is_a_combined_stream(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)

    assert stream.url.startswith(fast_ws_config.data.websocket.base_url)
    assert "streams=" in stream.url


# ---- event parsing ----------------------------------------------------------


async def test_closed_kline_becomes_an_event(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)

    stream._handle_frame(kline_frame(is_closed=True))

    event = stream.events.get_nowait()
    assert event.type is EventType.KLINE
    assert event.symbol == "BTCUSDT"
    assert event.timeframe == "15m"
    assert event.payload["is_closed"] is True
    assert event.payload["close"] == pytest.approx(100.5)
    assert event.payload["taker_buy_base"] == pytest.approx(7.5)
    assert event.payload["trades"] == 42


async def test_unclosed_kline_is_dropped_by_default(fast_ws_config: Config) -> None:
    """Spec: never emit a signal from an unclosed candle.

    Dropping it at the source means no downstream consumer can use one by mistake.
    """
    stream = make_stream(fast_ws_config, intrabar=False)

    stream._handle_frame(kline_frame(is_closed=False))

    assert stream.events.empty()


async def test_unclosed_kline_is_kept_in_intrabar_mode(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config, intrabar=True)

    stream._handle_frame(kline_frame(is_closed=False))

    event = stream.events.get_nowait()
    assert event.payload["is_closed"] is False


async def test_agg_trade_aggressor_side_is_not_inverted(fast_ws_config: Config) -> None:
    """Binance's `m` is true when the BUYER was the maker, i.e. a SELL aggressor.

    Getting this backwards silently inverts every order-flow feature.
    """
    stream = make_stream(fast_ws_config)
    frame = json.dumps(
        {
            "data": {
                "e": "aggTrade",
                "E": 1_700_000_000_000,
                "s": "BTCUSDT",
                "p": "100.25",
                "q": "0.5",
                "m": True,
                "T": 1_699_999_999_000,
            }
        }
    )

    stream._handle_frame(frame)

    event = stream.events.get_nowait()
    assert event.type is EventType.AGG_TRADE
    assert event.payload["is_buyer_maker"] is True  # aggressor was the seller
    assert event.payload["price"] == pytest.approx(100.25)
    assert event.payload["quantity"] == pytest.approx(0.5)


async def test_depth_frame_is_parsed(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)
    frame = json.dumps(
        {
            "stream": "btcusdt@depth20@100ms",
            "data": {
                "lastUpdateId": 123,
                "bids": [["100.0", "1.5"], ["99.9", "2.0"]],
                "asks": [["100.1", "1.0"]],
                "_stream": "btcusdt@depth20@100ms",
            },
        }
    )

    stream._handle_frame(frame)

    event = stream.events.get_nowait()
    assert event.type is EventType.DEPTH
    assert event.payload["bids"][0] == [100.0, 1.5]
    assert event.payload["asks"][0] == [100.1, 1.0]
    assert event.symbol == "BTCUSDT"


async def test_ticker_frame_is_parsed(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)
    frame = json.dumps(
        {"data": {"e": "24hrTicker", "E": 1_700_000_000_000, "s": "BTCUSDT", "c": "100.5"}}
    )

    stream._handle_frame(frame)

    assert stream.events.get_nowait().type is EventType.TICKER


async def test_malformed_json_is_counted_not_fatal(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)

    stream._handle_frame("{not json")

    assert stream.health.errors == 1
    assert stream.events.empty()


async def test_unknown_event_types_are_ignored(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config)

    stream._handle_frame(json.dumps({"data": {"e": "somethingNew", "s": "BTCUSDT"}}))

    assert stream.events.empty()
    assert stream.health.errors == 0


async def test_queue_full_is_logged_not_raised(fast_ws_config: Config) -> None:
    """A slow consumer must degrade the stream, not crash the engine."""
    stream = make_stream(fast_ws_config, queue_maxsize=1)

    stream._handle_frame(kline_frame(open_time=1_700_000_000_000))
    stream._handle_frame(kline_frame(open_time=1_700_000_900_000))

    assert stream.events.qsize() == 1
    assert stream.health.errors == 1


async def test_lag_is_measured_from_exchange_timestamp(fast_ws_config: Config) -> None:
    stream = make_stream(fast_ws_config, time_fn=lambda: 1_700_000_010.0)

    stream._handle_frame(kline_frame(open_time=1_700_000_000_000))

    event = stream.events.get_nowait()
    assert event.lag_ms == 1_700_000_010_000 - 1_700_000_001_000
    assert stream.health.last_lag_ms == event.lag_ms


# ---- connection lifecycle ---------------------------------------------------


async def test_health_reflects_a_successful_connection(fast_ws_config: Config) -> None:
    socket = FakeSocket([kline_frame()])

    async def connect(url: str) -> FakeSocket:
        return socket

    stream = make_stream(fast_ws_config, connect_fn=connect)
    await stream._consume_once()

    assert stream.health.messages == 1
    assert socket.closed, "the socket must be closed when the loop exits"


async def test_run_reconnects_after_failures(fast_ws_config: Config) -> None:
    """Every disconnect is survived, counted, and followed by a reconnect."""
    attempts = 0

    async def connect(url: str) -> FakeSocket:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise ConnectionError(f"refused #{attempts}")
        stream.stop()
        return FakeSocket([kline_frame()])

    stream = make_stream(fast_ws_config, connect_fn=connect)
    await asyncio.wait_for(stream.run(), timeout=5.0)

    assert attempts == 4
    assert stream.health.reconnects >= 3
    assert stream.health.errors >= 3
    assert "ConnectionError" in stream.health.last_error
    assert stream.health.connected is False


async def test_backoff_grows_then_resets_after_a_clean_cycle(
    fast_ws_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("cse.data.ws.asyncio.sleep", record_sleep)

    attempts = 0

    async def connect(url: str) -> FakeSocket:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise ConnectionError("refused")
        if attempts >= 5:
            stream.stop()
        return FakeSocket([kline_frame()])

    stream = make_stream(fast_ws_config, connect_fn=connect)
    await asyncio.wait_for(stream.run(), timeout=5.0)

    assert sleeps[0] < sleeps[1] < sleeps[2], "backoff must grow while failing"
    assert max(sleeps) <= fast_ws_config.data.websocket.reconnect_max_seconds
    # A connection that consumed cleanly resets the backoff to its floor.
    assert sleeps[3] == pytest.approx(fast_ws_config.data.websocket.reconnect_initial_seconds)


async def test_stale_connection_is_detected(fast_ws_config: Config) -> None:
    """TCP can stay up while frames stop arriving; staleness needs its own check."""
    now = 1_700_000_000.0
    stream = make_stream(fast_ws_config, time_fn=lambda: now)
    stream.health.last_message_ms = int(now * 1000)

    assert stream._is_stale() is False

    limit_s = (
        fast_ws_config.data.websocket.ping_interval_seconds
        + fast_ws_config.data.websocket.ping_timeout_seconds
    )
    now += limit_s + 1.0

    assert stream._is_stale() is True


async def test_connection_is_recycled_before_the_24h_server_limit(
    fast_ws_config: Config,
) -> None:
    """Binance force-closes at 24h; recycling first turns that into a planned event."""
    ws = fast_ws_config.data.websocket.model_copy(update={"max_connection_seconds": 0.0})
    clock = {"t": 1_700_000_000.0}

    def time_fn() -> float:
        clock["t"] += 1.0
        return clock["t"]

    stream = MarketStream(
        ws,
        ["BTCUSDT"],
        ["15m"],
        connect_fn=lambda _url: _immediate(FakeSocket([kline_frame(), kline_frame()], hang=True)),
        time_fn=time_fn,
        rng=random.Random(0),
    )

    await asyncio.wait_for(stream._consume_once(), timeout=5.0)

    assert stream.health.messages == 1  # stopped after the first frame, did not hang


async def _immediate(socket: FakeSocket) -> FakeSocket:
    return socket
