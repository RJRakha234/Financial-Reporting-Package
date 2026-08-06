"""Live-stream supervisor: connect, watch, degrade, recover, idle.

The supervisor owns the engine's market-state machine. Its most important
behaviour is the least glamorous one: **when the market is shut it does
nothing, loudly**. An engine that alarms every night at 15:31 because the tick
feed went quiet is an engine whose alerts get ignored during the day.

Degradation ladder:

1. ``LIVE`` — websocket connected, ticks arriving inside the heartbeat window.
2. ``STALE`` — connected but silent past ``heartbeat_timeout_seconds``.
3. ``REST_FALLBACK`` — socket down longer than ``rest_fallback_after_seconds``;
   the engine polls REST quotes so bars keep forming, degraded but continuous.
4. ``HALTED`` — too many consecutive reconnects. Stops trying and says so
   rather than hammering the vendor into a rate-limit ban.

The decision logic is deliberately factored into small pure functions so it can
be tested without a socket, a clock or a broker.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from nifty50.config import Config
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.base import BrokerAdapter, StreamCallbacks, StreamMode
from nifty50.domain import Candle, Instrument, SessionPhase, Tick, now_ist
from nifty50.logging_setup import get_logger
from nifty50.trading_calendar.calendar import TradingCalendar

log = get_logger(__name__)


class EngineState(StrEnum):
    IDLE_MARKET_CLOSED = "idle_market_closed"
    PRE_OPEN = "pre_open"
    CONNECTING = "connecting"
    LIVE = "live"
    STALE = "stale"
    REST_FALLBACK = "rest_fallback"
    HALTED = "halted"


@dataclass(slots=True)
class StreamHealth:
    """The system-health panel's data source."""

    state: EngineState = EngineState.IDLE_MARKET_CLOSED
    session_phase: SessionPhase = SessionPhase.CLOSED
    connected_since: dt.datetime | None = None
    last_tick_at: dt.datetime | None = None
    last_tick_lag_ms: float | None = None
    ticks_received: int = 0
    candles_emitted: int = 0
    reconnect_count: int = 0
    consecutive_failures: int = 0
    error_count: int = 0
    last_error: str = ""
    token_expires_at: dt.datetime | None = None
    subscribed_instruments: int = 0

    def token_seconds_remaining(self, now: dt.datetime | None = None) -> float | None:
        if self.token_expires_at is None:
            return None
        return (self.token_expires_at - (now or now_ist())).total_seconds()

    def data_age_seconds(self, now: dt.datetime | None = None) -> float | None:
        if self.last_tick_at is None:
            return None
        return ((now or now_ist()) - self.last_tick_at).total_seconds()


# ------------------------------------------------------------- pure decisions


def next_backoff(
    attempt: int,
    *,
    initial: float,
    multiplier: float,
    maximum: float,
    jitter_fraction: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with proportional jitter.

    Jitter matters more than usual here: fifty symbols behind one API key means a
    vendor-side blip disconnects every consumer at once, and unjittered backoff
    would reconnect them all in the same millisecond.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    base = min(initial * (multiplier**attempt), maximum)
    if jitter_fraction <= 0:
        return base
    generator = rng or random
    spread = base * jitter_fraction
    return max(0.0, base + generator.uniform(-spread, spread))


def is_heartbeat_stale(
    last_tick_at: dt.datetime | None, now: dt.datetime, timeout_seconds: float
) -> bool:
    """Whether the feed has gone quiet for longer than the heartbeat window."""
    if last_tick_at is None:
        return False
    return (now - last_tick_at).total_seconds() > timeout_seconds


def should_fall_back_to_rest(
    disconnected_since: dt.datetime | None, now: dt.datetime, threshold_seconds: float
) -> bool:
    if disconnected_since is None:
        return False
    return (now - disconnected_since).total_seconds() >= threshold_seconds


def desired_state_for_phase(phase: SessionPhase) -> EngineState:
    """What the engine should be doing given only the session phase."""
    if phase is SessionPhase.CONTINUOUS:
        return EngineState.LIVE
    if phase is SessionPhase.PRE_OPEN:
        return EngineState.PRE_OPEN
    return EngineState.IDLE_MARKET_CLOSED


# ------------------------------------------------------------------ supervisor


@dataclass(slots=True)
class _Subscription:
    instruments: list[Instrument]
    mode: StreamMode


class StreamSupervisor:
    """Drives one broker stream through a trading day."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        calendar: TradingCalendar,
        config: Config,
        *,
        aggregators: Sequence[CandleAggregator] = (),
        on_candle: Callable[[Candle], None] | None = None,
        on_tick: Callable[[Tick], None] | None = None,
        clock: Callable[[], dt.datetime] = now_ist,
    ) -> None:
        self._adapter = adapter
        self._calendar = calendar
        self._config = config
        self._settings = config.stream
        self._aggregators = list(aggregators)
        self._on_candle = on_candle
        self._on_tick = on_tick
        self._now = clock
        self.health = StreamHealth()
        self._subscription: _Subscription | None = None
        self._queue: asyncio.Queue[list[Tick]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._disconnected_since: dt.datetime | None = None
        self._connected = False
        self._flushed_for: dt.date | None = None

    # ------------------------------------------------------------ lifecycle

    def subscribe(self, instruments: Sequence[Instrument], mode: StreamMode) -> None:
        """Record what to subscribe to; applied on each (re)connect."""
        instruments = list(instruments)
        cap = self._adapter.max_stream_instruments
        if len(instruments) > cap:
            raise ValueError(
                f"{len(instruments)} instruments exceeds the vendor websocket cap of {cap}; "
                "split across connections or reduce the universe"
            )
        self._subscription = _Subscription(instruments=instruments, mode=mode)
        self.health.subscribed_instruments = len(instruments)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, *, max_iterations: int | None = None) -> None:
        """Main loop: step until stopped, then tear down.

        ``max_iterations`` bounds the loop for tests and for embedding the
        supervisor in another event loop that owns its own shutdown.
        """
        iterations = 0
        while not self._stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            await self.step()
        await self.shutdown()

    async def step(self) -> None:
        """Advance the state machine by one iteration.

        Public and re-entrant so the loop can be driven externally — by a test,
        or by a dashboard process that owns the event loop. Never raises: the
        supervisor must survive anything a vendor SDK throws at it, because the
        alternative is an engine that dies silently at 10:30.
        """
        self._loop = asyncio.get_running_loop()
        try:
            await self._advance()
        except Exception as exc:
            self._record_error(exc)
            await asyncio.sleep(self._settings.reconnect_backoff_initial_seconds)

    async def _advance(self) -> None:
        now = self._now()
        phase = self._calendar.phase(now)
        self.health.session_phase = phase

        if phase is not SessionPhase.CONTINUOUS:
            await self._handle_closed(now, phase)
            return

        self._flushed_for = None
        if not self._connected:
            await self._connect(now)
            return

        await self._drain_queue()
        self._update_staleness(now)

    async def _handle_closed(self, now: dt.datetime, phase: SessionPhase) -> None:
        """Idle cleanly outside the continuous session."""
        if self._connected:
            # Close out the day: flush partial bars so the last bar of the session
            # is written, then drop the socket.
            self._flush_aggregators(now)
            self._adapter.close_stream()
            self._connected = False
        self.health.state = desired_state_for_phase(phase)
        self.health.connected_since = None
        await asyncio.sleep(self._settings.idle_poll_seconds_when_closed)

    async def _connect(self, now: dt.datetime) -> None:
        budget = self._settings.max_consecutive_reconnects_before_halt
        if self.health.consecutive_failures >= budget:
            self.health.state = EngineState.HALTED
            log.error(
                "stream.halted",
                failures=self.health.consecutive_failures,
                hint="reconnect budget exhausted; check credentials and vendor status",
            )
            await asyncio.sleep(self._settings.idle_poll_seconds_when_closed)
            return

        self.health.state = EngineState.CONNECTING
        try:
            self._adapter.open_stream(self._callbacks())
            if self._subscription is not None:
                self._adapter.subscribe(self._subscription.instruments, self._subscription.mode)
                # A mid-session attach has no cumulative-volume baseline.
                for aggregator in self._aggregators:
                    for instrument in self._subscription.instruments:
                        aggregator.attach_mid_session(instrument.key)
        except Exception as exc:
            self._record_error(exc)
            self.health.consecutive_failures += 1
            delay = next_backoff(
                self.health.consecutive_failures - 1,
                initial=self._settings.reconnect_backoff_initial_seconds,
                multiplier=self._settings.reconnect_backoff_multiplier,
                maximum=self._settings.reconnect_backoff_max_seconds,
                jitter_fraction=self._settings.reconnect_jitter_fraction,
            )
            log.warning("stream.reconnect_scheduled", delay_seconds=round(delay, 2))
            await asyncio.sleep(delay)
            return

        self._connected = True
        self._disconnected_since = None
        self.health.consecutive_failures = 0
        self.health.connected_since = now
        self.health.state = EngineState.LIVE
        self.health.token_expires_at = self._adapter.token_status().expires_at

    def _callbacks(self) -> StreamCallbacks:
        return StreamCallbacks(
            on_ticks=self._on_ticks_threadsafe,
            on_connect=lambda: log.info("stream.connected"),
            on_close=self._on_close,
            on_error=self._record_error,
        )

    def _on_ticks_threadsafe(self, ticks: list[Tick]) -> None:
        """Called on the vendor's thread. Hands off to the event loop, nothing more."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, ticks)

    def _on_close(self, code: int | None, reason: str | None) -> None:
        self._connected = False
        self._disconnected_since = self._now()
        self.health.reconnect_count += 1
        log.warning("stream.closed", code=code, reason=reason)

    async def _drain_queue(self) -> None:
        # Yield once so that tick batches handed over from the vendor thread via
        # call_soon_threadsafe have actually landed in the queue before we read
        # it. Without this the drain races the handoff and reports a quiet feed.
        await asyncio.sleep(0)
        while True:
            try:
                ticks = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._handle_ticks(ticks)

    def _handle_ticks(self, ticks: list[Tick]) -> None:
        for tick in ticks:
            self.health.ticks_received += 1
            self.health.last_tick_at = tick.received_at or tick.ts
            lag = tick.lag
            self.health.last_tick_lag_ms = lag.total_seconds() * 1000 if lag else None
            if self._on_tick is not None:
                self._on_tick(tick)
            for aggregator in self._aggregators:
                candle = aggregator.on_tick(tick)
                if candle is not None:
                    self._emit_candle(candle)

    def _emit_candle(self, candle: Candle) -> None:
        self.health.candles_emitted += 1
        if self._on_candle is not None:
            self._on_candle(candle)

    def _flush_aggregators(self, now: dt.datetime) -> None:
        day = now.date()
        if self._flushed_for == day:
            return
        for aggregator in self._aggregators:
            for candle in aggregator.flush_all():
                self._emit_candle(candle)
        self._flushed_for = day

    def _update_staleness(self, now: dt.datetime) -> None:
        if should_fall_back_to_rest(
            self._disconnected_since, now, self._settings.rest_fallback_after_seconds
        ):
            self.health.state = EngineState.REST_FALLBACK
            return
        if is_heartbeat_stale(
            self.health.last_tick_at, now, self._settings.heartbeat_timeout_seconds
        ):
            self.health.state = EngineState.STALE
            return
        self.health.state = EngineState.LIVE

    async def poll_rest_once(self) -> list[Tick]:
        """One REST-fallback poll. Keeps bars forming while the socket is down."""
        if self._subscription is None:
            return []
        quotes = self._adapter.quote(self._subscription.instruments)
        ticks = list(quotes.values())
        self._handle_ticks(ticks)
        return ticks

    def _record_error(self, exc: BaseException) -> None:
        self.health.error_count += 1
        self.health.last_error = str(exc)
        log.error("stream.error", error=str(exc), error_type=type(exc).__name__)

    async def shutdown(self) -> None:
        """Flush open bars and drop the socket. Idempotent."""
        if self._connected:
            self._flush_aggregators(self._now())
            self._adapter.close_stream()
            self._connected = False
        log.info(
            "stream.stopped",
            ticks=self.health.ticks_received,
            candles=self.health.candles_emitted,
            errors=self.health.error_count,
        )


@dataclass(slots=True)
class CandleSink:
    """Collects emitted candles. Useful in tests and as a wiring example."""

    candles: list[Candle] = field(default_factory=list)

    def __call__(self, candle: Candle) -> None:
        self.candles.append(candle)
