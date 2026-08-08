"""Drive the dashboard from stored bars, without a broker connection.

:class:`~nifty50.data.stream.StreamSupervisor` is built for a vendor that
pushes ticks at it. The replay adapter pushes nothing until something calls
:meth:`~nifty50.data.brokers.replay.ReplayAdapter.pump`, and the supervisor
never does — so pointing the supervisor at replay produces a dashboard that
connects and then sits empty forever. Worse, the supervisor idles outside the
continuous session, so on a weekend it would not even connect. This module is
the missing driver: it plays the part the websocket plays in production.

It goes through the adapter's own tick synthesis and the real
:class:`~nifty50.data.aggregator.CandleAggregator`, so the path exercised here
is the path that runs live — bar boundaries, session rolls, partial stub bars
and all. What it does *not* do is pretend to be live: health reports
``REST_FALLBACK`` and the session phase of the bar being replayed, never
``LIVE``. A replay that reports itself as live is how you end up trusting a
weekend rehearsal as a market test.

Ticks are fed at a controlled pace so bars form visibly rather than a whole
year appearing in one frame. That pacing is cosmetic and applies to nothing
but the display.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from nifty50.dashboard.state import DashboardState
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.base import StreamCallbacks, StreamMode
from nifty50.data.brokers.replay import ReplayAdapter
from nifty50.data.stream import EngineState, StreamHealth
from nifty50.domain import IST, Instrument, SessionPhase, Tick, Timeframe, now_ist
from nifty50.frames import ist_index
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

# Finest first: the source series must be at least as fine as the bars being
# built from it. You cannot make a 15-minute bar out of 30-minute closes, and
# a driver that tried would produce a chart that looks plausible and is wrong.
_BY_RESOLUTION: tuple[Timeframe, ...] = (
    Timeframe.M1,
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
)

# Bars are stamped in the past; a replayed session must not look stale on the
# health panel for the wrong reason.
_WALL_CLOCK_TICK: bool = True


class ReplayDataError(ValueError):
    """The replay root has nothing that can drive the requested timeframes."""


@dataclass(slots=True)
class ReplayDriver:
    """Feeds stored bars through the aggregators into the dashboard."""

    adapter: ReplayAdapter
    aggregators: Sequence[CandleAggregator]
    state: DashboardState
    instruments: Sequence[Instrument]
    seconds_per_tick: float = 0.05
    max_sessions: int | None = 30
    health: StreamHealth = field(default_factory=StreamHealth)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _pending: list[Tick] = field(default_factory=list)

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- planning

    def source_timeframe(self) -> Timeframe:
        """The finest stored series available, which must not be coarser than
        the finest bar being built from it.

        Finest, not merely sufficient. One tick per output bar makes every
        candle a doji -- open, high, low and close all equal to the source
        bar's close -- which looks like a rendering bug and is actually a
        sampling one. More source bars per output bar means the high and low
        are sampled more often and the shape gets closer to the truth.
        """
        wanted = [a.timeframe for a in self.aggregators if a.timeframe.is_intraday]
        if not wanted:
            raise ReplayDataError("replay needs at least one intraday timeframe to build")
        finest_wanted = min(wanted, key=_BY_RESOLUTION.index)

        available = self._available_timeframes()
        if not available:
            raise ValueError(
                "no stored bars found for "
                + ", ".join(i.symbol for i in self.instruments)
                + ". Build a replay root first:\n"
                "  python -m nifty50.scripts.build_replay --source <download-dir>"
            )
        cap = _BY_RESOLUTION.index(finest_wanted)
        usable = [tf for tf in available if _BY_RESOLUTION.index(tf) <= cap]
        if not usable:
            stored = ", ".join(tf.value for tf in available)
            raise ReplayDataError(
                f"cannot build {finest_wanted.value} bars from stored {stored} data -- "
                f"the source must be at least as fine as the bars requested. "
                f"Either download {finest_wanted.value} or finer, or ask the dashboard "
                f"for --timeframes {available[0].value}."
            )
        return min(usable, key=_BY_RESOLUTION.index)

    def fidelity_notice(self, source: Timeframe) -> str:
        """What replayed bar shapes cannot tell you, stated plainly.

        Live, a 15-minute bar is folded from thousands of ticks and its high
        and low are real. Replayed from stored 5-minute closes it is folded
        from three prices, so the range is systematically understated -- and
        the range is exactly what the cost-hurdle panel tests. Understating it
        makes "below costs" appear more often than it should, which is the
        safe direction to be wrong in, but only if you know about it.
        """
        finest_built = min(
            (a.timeframe for a in self.aggregators if a.timeframe.is_intraday),
            key=_BY_RESOLUTION.index,
        )
        per_bar = finest_built.minutes // source.minutes
        return (
            f"Replay: bars rebuilt from stored {source.value} closes "
            f"({per_bar} per {finest_built.value} bar). Highs, lows and the bar "
            f"range are approximate and biased low -- the cost-hurdle panel is "
            f"correspondingly pessimistic. Live ticks do not have this problem."
        )

    def _available_timeframes(self) -> list[Timeframe]:
        """Bar sizes present for every subscribed instrument, finest first."""
        common: set[Timeframe] | None = None
        for instrument in self.instruments:
            present = {
                tf
                for tf in _BY_RESOLUTION
                if not self.adapter.historical_candles(
                    instrument, tf, _EPOCH, _FAR_FUTURE
                ).empty
            }
            common = present if common is None else (common & present)
        return sorted(common or set(), key=_BY_RESOLUTION.index)

    def sessions(self, timeframe: Timeframe) -> list[dt.date]:
        """Session dates present in the stored bars, oldest first."""
        days: set[dt.date] = set()
        for instrument in self.instruments:
            frame = self.adapter.historical_candles(instrument, timeframe, _EPOCH, _FAR_FUTURE)
            if frame.empty:
                continue
            days.update(ts.date() for ts in ist_index(frame))
        ordered = sorted(days)
        if self.max_sessions is not None:
            ordered = ordered[-self.max_sessions :]
        return ordered

    # -------------------------------------------------------------- running

    async def run(self) -> None:
        source = self.source_timeframe()
        days = self.sessions(source)
        log.info(
            "replay.plan",
            source_timeframe=source.value,
            sessions=len(days),
            instruments=len(self.instruments),
        )

        self.adapter.authenticate()
        self.adapter.open_stream(StreamCallbacks(on_ticks=self._collect))
        self.adapter.subscribe(list(self.instruments), StreamMode.FULL)
        self.health.subscribed_instruments = len(self.instruments)
        self.health.token_expires_at = self.adapter.token_status().expires_at
        # Never LIVE. This is stored data being replayed, and the panel says so.
        self.health.state = EngineState.REST_FALLBACK
        self.state.set_notice(self.fidelity_notice(source))
        self.state.set_health(self.health)

        try:
            for day in days:
                if self._stop.is_set():
                    break
                await self._replay_day(day, source)
            self._flush()
        finally:
            self.adapter.close_stream()
            self.health.state = EngineState.IDLE_MARKET_CLOSED
            self.health.session_phase = SessionPhase.CLOSED
            self.state.set_health(self.health)
            log.info("replay.finished", ticks=self.health.ticks_received)

    async def _replay_day(self, day: dt.date, source: Timeframe) -> None:
        self._pending.clear()
        # pump() delivers the whole session in one callback; collecting first
        # and pacing afterwards keeps the adapter's real tick synthesis while
        # still letting bars form on screen one at a time.
        self.adapter.pump(day, timeframe=source)
        ticks = sorted(self._pending, key=lambda tick: tick.ts)
        self.health.session_phase = SessionPhase.CONTINUOUS

        for tick in ticks:
            if self._stop.is_set():
                return
            self._on_tick(tick)
            if self.seconds_per_tick > 0:
                await asyncio.sleep(self.seconds_per_tick)

        # End of session: flush the stub bars, exactly as the supervisor does
        # when the continuous phase ends.
        self._flush()
        self.health.session_phase = SessionPhase.CLOSED
        self.state.set_health(self.health)

    def _on_tick(self, tick: Tick) -> None:
        self.health.ticks_received += 1
        self.health.last_tick_at = now_ist() if _WALL_CLOCK_TICK else tick.ts
        for aggregator in self.aggregators:
            closed = aggregator.on_tick(tick)
            if closed is not None:
                self.health.candles_emitted += 1
                self.state.on_candle(closed)
            forming = aggregator.in_progress(tick.instrument_key)
            if forming is not None:
                self.state.on_candle(forming)
        self.state.set_health(self.health)

    def _flush(self) -> None:
        for aggregator in self.aggregators:
            for candle in aggregator.flush_all():
                self.health.candles_emitted += 1
                self.state.on_candle(candle)

    def _collect(self, ticks: list[Tick]) -> None:
        self._pending.extend(ticks)


_EPOCH = dt.datetime(1996, 1, 1, tzinfo=IST)
_FAR_FUTURE = dt.datetime(2099, 1, 1, tzinfo=IST)
