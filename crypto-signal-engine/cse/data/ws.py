"""Binance WebSocket streaming with reconnect, heartbeat, and REST fallback.

Consumes the combined stream endpoint for ``kline_<interval>``, ``aggTrade``,
``depth20@100ms`` and ``ticker``. Three failure modes are handled explicitly
because all three happen in normal operation:

* **Silent socket.** TCP stays up while frames stop arriving. Protocol-level
  ping/pong catches most of it; a staleness check on the last *application*
  message catches the rest.
* **Hard disconnect.** Reconnect with exponential backoff and jitter. Every
  disconnect is logged with its cause.
* **Extended outage.** If the socket stays down past the configured threshold,
  a REST poller takes over so the engine keeps producing closed candles (at
  lower resolution and with no order-flow data) rather than going blind.

Binance also closes any raw connection after 24 hours; the manager recycles
proactively before that, which turns an unexpected drop into a scheduled one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cse.config import WebsocketConfig
from cse.data.rest import BinanceRestClient
from cse.data.schema import candle_from_ws, interval_to_ms
from cse.logging import get_logger

_log = get_logger(__name__)


class EventType(StrEnum):
    KLINE = "kline"
    AGG_TRADE = "aggTrade"
    DEPTH = "depth"
    TICKER = "ticker"


@dataclass(frozen=True)
class MarketEvent:
    """A normalised event from any stream.

    ``payload`` shape depends on ``type``:

    * KLINE     -> canonical candle dict plus ``is_closed``
    * AGG_TRADE -> ``{price, quantity, is_buyer_maker, trade_time}``
    * DEPTH     -> ``{bids: [[price, qty], ...], asks: [...]}``
    * TICKER    -> raw 24h rolling statistics
    """

    type: EventType
    symbol: str
    timeframe: str | None
    event_time_ms: int
    received_ms: int
    payload: dict[str, Any]

    @property
    def lag_ms(self) -> int:
        """Time between the exchange stamping the event and us receiving it."""
        return self.received_ms - self.event_time_ms


@dataclass
class StreamHealth:
    """Live connection health, surfaced on the dashboard's system-health panel."""

    connected: bool = False
    fallback_active: bool = False
    connected_since_ms: int | None = None
    last_message_ms: int | None = None
    last_lag_ms: int | None = None
    reconnects: int = 0
    errors: int = 0
    messages: int = 0
    last_error: str = ""

    def age_ms(self, now_ms: int | None = None) -> int | None:
        if self.last_message_ms is None:
            return None
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return now - self.last_message_ms

    def as_dict(self, now_ms: int | None = None) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "fallback_active": self.fallback_active,
            "last_message_age_ms": self.age_ms(now_ms),
            "last_lag_ms": self.last_lag_ms,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "messages": self.messages,
            "last_error": self.last_error,
        }


# A connect function yields an async iterator of raw text frames. Injected so
# the reconnect/backoff logic is testable without a network.
ConnectFn = Callable[[str], Awaitable["WebSocketLike"]]


class WebSocketLike:
    """Minimal protocol the manager needs from a socket implementation."""

    async def __aenter__(self) -> WebSocketLike:  # pragma: no cover - protocol
        raise NotImplementedError

    async def __aexit__(self, *exc: object) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def __aiter__(self) -> AsyncIterator[str]:  # pragma: no cover - protocol
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - protocol
        raise NotImplementedError


def build_stream_names(
    symbols: list[str],
    timeframes: list[str],
    *,
    depth_levels: int,
    depth_update_ms: int,
    include_flow: bool = True,
) -> list[str]:
    """Binance combined-stream names for the configured symbols/timeframes."""
    names: list[str] = []
    for symbol in symbols:
        lower = symbol.lower()
        for timeframe in timeframes:
            names.append(f"{lower}@kline_{timeframe}")
        if include_flow:
            names.append(f"{lower}@aggTrade")
            names.append(f"{lower}@depth{depth_levels}@{depth_update_ms}ms")
            names.append(f"{lower}@ticker")
    return names


class MarketStream:
    """Manages the combined WebSocket connection and emits :class:`MarketEvent`."""

    def __init__(
        self,
        config: WebsocketConfig,
        symbols: list[str],
        timeframes: list[str],
        *,
        intrabar: bool = False,
        rest_client: BinanceRestClient | None = None,
        connect_fn: ConnectFn | None = None,
        queue_maxsize: int = 10_000,
        rng: random.Random | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._symbols = symbols
        self._timeframes = timeframes
        self._intrabar = intrabar
        self._rest = rest_client
        self._connect_fn = connect_fn or _default_connect
        self._rng = rng or random.Random()
        self._time_fn = time_fn
        self.events: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_maxsize)
        self.health = StreamHealth()
        self._stop = asyncio.Event()
        self._down_since: float | None = None
        self._fallback_task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        streams = build_stream_names(
            self._symbols,
            self._timeframes,
            depth_levels=self._config.depth_levels,
            depth_update_ms=self._config.depth_update_ms,
        )
        return f"{self._config.base_url}?streams={'/'.join(streams)}"

    def stop(self) -> None:
        self._stop.set()

    def _now_ms(self) -> int:
        return int(self._time_fn() * 1000)

    def _jittered(self, delay: float) -> float:
        jitter = self._config.reconnect_jitter
        if jitter <= 0.0:
            return delay
        return max(0.0, delay * (1.0 + self._rng.uniform(-jitter, jitter)))

    async def run(self) -> None:
        """Connect-and-consume loop; returns when :meth:`stop` is called."""
        delay = self._config.reconnect_initial_seconds
        while not self._stop.is_set():
            try:
                await self._consume_once()
                delay = self._config.reconnect_initial_seconds  # clean cycle: reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.errors += 1
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                _log.warning(
                    "ws.disconnected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    reconnects=self.health.reconnects,
                    sleep_seconds=round(delay, 3),
                )
            finally:
                self.health.connected = False

            if self._stop.is_set():
                break

            if self._down_since is None:
                self._down_since = self._time_fn()
            await self._maybe_start_fallback()

            self.health.reconnects += 1
            await asyncio.sleep(self._jittered(delay))
            delay = min(
                delay * self._config.reconnect_multiplier,
                self._config.reconnect_max_seconds,
            )

        await self._stop_fallback()

    async def _consume_once(self) -> None:
        """One connection lifetime: connect, consume until stale/closed/expired."""
        url = self.url
        _log.info("ws.connecting", stream_count=url.count("/") + 1)
        socket = await self._connect_fn(url)
        opened_at = self._time_fn()
        self.health.connected = True
        self.health.connected_since_ms = self._now_ms()
        self.health.last_message_ms = self._now_ms()
        self._down_since = None
        await self._stop_fallback()
        _log.info("ws.connected")

        try:
            async for raw in socket:
                self._handle_frame(raw)

                if self._stop.is_set():
                    break
                if self._time_fn() - opened_at > self._config.max_connection_seconds:
                    _log.info("ws.recycling", reason="max_connection_seconds")
                    break
                if self._is_stale():
                    raise TimeoutError(
                        f"no application frame for more than "
                        f"{self._config.ping_timeout_seconds + self._config.ping_interval_seconds}s"
                    )
        finally:
            with contextlib.suppress(Exception):
                await socket.close()

    def _is_stale(self) -> bool:
        """True if application frames stopped arriving.

        Distinct from protocol ping/pong: Binance keeps the socket alive with
        pongs even for a subscription that has gone quiet, so silence at the
        application layer needs its own detector.
        """
        if self.health.last_message_ms is None:
            return False
        limit_ms = (self._config.ping_interval_seconds + self._config.ping_timeout_seconds) * 1000
        age = self._now_ms() - self.health.last_message_ms
        return age > limit_ms

    def _handle_frame(self, raw: str) -> None:
        received = self._now_ms()
        self.health.last_message_ms = received
        self.health.messages += 1
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.health.errors += 1
            _log.warning("ws.bad_json", sample=raw[:200])
            return

        # Combined streams wrap the payload as {"stream": ..., "data": ...}.
        data = message.get("data", message)
        if not isinstance(data, dict):
            return
        event = self._to_event(data, received)
        if event is None:
            return
        self.health.last_lag_ms = event.lag_ms
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            self.health.errors += 1
            _log.error(
                "ws.queue_full",
                note="consumer is slower than the stream; dropping event",
                event_type=event.type.value,
            )

    def _to_event(self, data: dict[str, Any], received_ms: int) -> MarketEvent | None:
        event_type = data.get("e")
        symbol = str(data.get("s", "")).upper()
        event_time = int(data.get("E", received_ms))

        if event_type == "kline":
            kline = data["k"]
            is_closed = bool(kline.get("x", False))
            if not is_closed and not self._intrabar:
                # Spec: never emit a signal from an unclosed candle. Dropping it
                # here means no downstream consumer can accidentally use one.
                return None
            candle = candle_from_ws(kline)
            candle["is_closed"] = is_closed
            return MarketEvent(
                type=EventType.KLINE,
                symbol=symbol,
                timeframe=str(kline["i"]),
                event_time_ms=event_time,
                received_ms=received_ms,
                payload=candle,
            )

        if event_type == "aggTrade":
            return MarketEvent(
                type=EventType.AGG_TRADE,
                symbol=symbol,
                timeframe=None,
                event_time_ms=event_time,
                received_ms=received_ms,
                payload={
                    "price": float(data["p"]),
                    "quantity": float(data["q"]),
                    # Binance's "m" is true when the BUYER was the maker, i.e.
                    # the aggressor was a SELLER. Getting this backwards inverts
                    # the order-flow imbalance feature.
                    "is_buyer_maker": bool(data["m"]),
                    "trade_time": int(data["T"]),
                },
            )

        if event_type == "24hrTicker":
            return MarketEvent(
                type=EventType.TICKER,
                symbol=symbol,
                timeframe=None,
                event_time_ms=event_time,
                received_ms=received_ms,
                payload=dict(data),
            )

        # depth20@100ms is a partial-book stream: no "e" field, has bids/asks.
        if "bids" in data and "asks" in data:
            return MarketEvent(
                type=EventType.DEPTH,
                symbol=symbol or self._symbol_from_depth(data),
                timeframe=None,
                event_time_ms=event_time,
                received_ms=received_ms,
                payload={
                    "bids": [[float(p), float(q)] for p, q in data["bids"]],
                    "asks": [[float(p), float(q)] for p, q in data["asks"]],
                    "last_update_id": data.get("lastUpdateId"),
                },
            )
        return None

    def _symbol_from_depth(self, data: dict[str, Any]) -> str:
        """Partial-depth frames omit the symbol; recover it from the stream name."""
        stream = str(data.get("_stream", ""))
        return stream.split("@", 1)[0].upper()

    # ---- REST fallback ------------------------------------------------------

    async def _maybe_start_fallback(self) -> None:
        if self._rest is None or self._down_since is None:
            return
        if self._fallback_task is not None and not self._fallback_task.done():
            return
        down_for = self._time_fn() - self._down_since
        if down_for < self._config.rest_fallback_after_seconds:
            return
        _log.warning(
            "ws.fallback_start",
            down_seconds=round(down_for, 1),
            poll_seconds=self._config.rest_fallback_poll_seconds,
            note="serving closed candles via REST; order-flow features unavailable",
        )
        self.health.fallback_active = True
        self._fallback_task = asyncio.create_task(self._rest_fallback_loop())

    async def _stop_fallback(self) -> None:
        if self._fallback_task is None:
            return
        self.health.fallback_active = False
        self._fallback_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._fallback_task
        self._fallback_task = None
        _log.info("ws.fallback_stop")

    async def _rest_fallback_loop(self) -> None:
        """Poll the most recent closed candle per symbol/timeframe while down."""
        assert self._rest is not None
        seen: dict[tuple[str, str], int] = {}
        while not self._stop.is_set():
            for symbol in self._symbols:
                for timeframe in self._timeframes:
                    try:
                        # limit=2 so the newest (possibly forming) bar can be
                        # discarded and the one before it is known-closed.
                        page = await self._rest.klines(symbol, timeframe, limit=2)
                    except Exception as exc:
                        self.health.errors += 1
                        _log.warning("ws.fallback_poll_failed", symbol=symbol, error=str(exc))
                        continue
                    if len(page) < 2:
                        continue
                    closed = page.iloc[-2]
                    open_time = int(closed["open_time"])
                    key = (symbol, timeframe)
                    if seen.get(key) == open_time:
                        continue
                    seen[key] = open_time
                    payload = {c: closed[c] for c in page.columns}
                    payload["is_closed"] = True
                    payload["source"] = "rest_fallback"
                    received = self._now_ms()
                    event = MarketEvent(
                        type=EventType.KLINE,
                        symbol=symbol,
                        timeframe=timeframe,
                        event_time_ms=int(closed["close_time"]),
                        received_ms=received,
                        payload=payload,
                    )
                    with contextlib.suppress(asyncio.QueueFull):
                        self.events.put_nowait(event)
            await asyncio.sleep(self._config.rest_fallback_poll_seconds)


async def _default_connect(url: str) -> WebSocketLike:  # pragma: no cover - needs network
    """Real connector, imported lazily so tests never require the library."""
    import websockets

    socket = await websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=10,
        max_queue=2**12,
    )
    return socket  # type: ignore[return-value]


def expected_bar_close_ms(open_time_ms: int, timeframe: str) -> int:
    """When a bar with this ``open_time`` should be final."""
    return open_time_ms + interval_to_ms(timeframe)
