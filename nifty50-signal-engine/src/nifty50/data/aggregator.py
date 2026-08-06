"""Session-aware aggregation of ticks into candles.

Three things here are specific to Indian broker feeds and are the usual source
of wrong volume in home-built engines:

*Cumulative volume.* Kite (and most NSE feeds) send ``volume_traded`` as the
day's running total, not the size of the last print. Summing ``last_quantity``
across ticks double-counts, because a tick is a snapshot of the book, not a
trade report. Bar volume is therefore a *difference of cumulative totals*,
rebased at the session open.

*Cold starts.* If the engine attaches at 11:00 the cumulative baseline for the
current bar is unknown, and treating it as zero would report five hours of
turnover as one bar's volume. Such bars are emitted with
``volume_estimated=True`` instead of being silently wrong.

*Hard session boundaries.* A tick at 15:47 belongs to the closing session, not
to the 15:15 bar. The aggregator refuses to place ticks outside the continuous
session rather than rounding them into the nearest bar, and the day's last bar
is closed by :meth:`flush_all` at the session close rather than by a tick that
will never arrive.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from nifty50.domain import Candle, Tick, Timeframe, ensure_ist
from nifty50.logging_setup import get_logger
from nifty50.trading_calendar.calendar import NotATradingDayError, TradingCalendar

log = get_logger(__name__)


class RejectionReason(StrEnum):
    OUTSIDE_SESSION = "outside_session"
    NON_TRADING_DAY = "non_trading_day"
    LATE_TICK = "late_tick"


@dataclass(slots=True)
class AggregationStats:
    accepted: int = 0
    rejected: dict[RejectionReason, int] = field(default_factory=dict)
    candles_emitted: int = 0

    def reject(self, reason: RejectionReason) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    @property
    def total_rejected(self) -> int:
        return sum(self.rejected.values())


@dataclass(slots=True)
class _BarState:
    """In-progress bar for one instrument."""

    bar_start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    baseline_cumulative_volume: int
    last_cumulative_volume: int | None
    summed_volume: int
    last_tick_ts: dt.datetime
    volume_estimated: bool
    open_interest: int | None = None

    def volume(self) -> int:
        """Bar volume from cumulative totals, falling back to summed prints."""
        if self.last_cumulative_volume is None:
            return self.summed_volume
        return max(0, self.last_cumulative_volume - self.baseline_cumulative_volume)


CandleCallback = Callable[[Candle], None]


class CandleAggregator:
    """Builds candles for one timeframe across many instruments."""

    def __init__(
        self,
        calendar: TradingCalendar,
        timeframe: Timeframe,
        *,
        on_candle: CandleCallback | None = None,
    ) -> None:
        self._calendar = calendar
        self._timeframe = timeframe
        self._on_candle = on_candle
        self._bars: dict[str, _BarState] = {}
        self._session_date: dict[str, dt.date] = {}
        # Cumulative day volume as of the last closed bar. ``None`` means the
        # baseline is not yet known for this session (cold start mid-session).
        self._baseline: dict[str, int | None] = {}
        self.stats = AggregationStats()

    @property
    def timeframe(self) -> Timeframe:
        return self._timeframe

    def in_progress(self, instrument_key: str) -> Candle | None:
        """The current, unclosed bar. Never treat this as history."""
        state = self._bars.get(instrument_key)
        if state is None:
            return None
        return self._to_candle(instrument_key, state, closed=False)

    def on_tick(self, tick: Tick) -> Candle | None:
        """Fold a tick into its bar. Returns a candle if one closed as a result."""
        ts = ensure_ist(tick.ts)
        key = tick.instrument_key

        if not self._calendar.has_any_session(ts.date()):
            self.stats.reject(RejectionReason.NON_TRADING_DAY)
            return None
        if not self._calendar.phase(ts).is_tradeable:
            # Pre-open and closing-session prints are real and interesting, but
            # they are not part of the continuous tape and must not enter its bars.
            self.stats.reject(RejectionReason.OUTSIDE_SESSION)
            return None
        try:
            bar_start = self._calendar.floor_to_bar(ts, self._timeframe)
        except NotATradingDayError:
            self.stats.reject(RejectionReason.OUTSIDE_SESSION)
            return None

        emitted = self._roll_session(key, bar_start.date())
        state = self._bars.get(key)

        if state is not None:
            if bar_start < state.bar_start:
                self.stats.reject(RejectionReason.LATE_TICK)
                log.debug("aggregator.late_tick", instrument=key, ts=ts.isoformat())
                return emitted
            if bar_start > state.bar_start:
                emitted = self._close_bar(key)
                state = None
            elif ts < state.last_tick_ts:
                # Same bar, but the feed re-ordered two ticks. The OHLC update is
                # still valid — only the "last" price would be wrong, and the
                # difference is a single tick within one bar.
                self.stats.reject(RejectionReason.LATE_TICK)
                return emitted

        if state is None:
            self._bars[key] = self._open_bar(key, bar_start, ts, tick)
            self.stats.accepted += 1
            return emitted

        state.high = max(state.high, tick.last_price)
        state.low = min(state.low, tick.last_price)
        state.close = tick.last_price
        state.last_tick_ts = ts
        if tick.volume_traded_today is not None:
            state.last_cumulative_volume = tick.volume_traded_today
        else:
            state.summed_volume += tick.last_quantity or 0
        if tick.open_interest is not None:
            state.open_interest = tick.open_interest
        self.stats.accepted += 1
        return emitted

    def _roll_session(self, key: str, session_day: dt.date) -> Candle | None:
        """Close any bar left open from a previous session and rebase volume."""
        if self._session_date.get(key) == session_day:
            return None
        emitted: Candle | None = None
        if key in self._bars:
            emitted = self._close_bar(key)
        self._session_date[key] = session_day
        # The baseline for the new session is not yet established. It is *not*
        # set to zero here: whether zero is correct depends on whether the first
        # bar we see is the session's opening bar, which _open_bar decides.
        self._baseline[key] = None
        return emitted

    def _open_bar(self, key: str, bar_start: dt.datetime, ts: dt.datetime, tick: Tick) -> _BarState:
        baseline = self._baseline.get(key)
        estimated = False
        if baseline is None:
            if self._calendar.is_session_open_bar(bar_start, self._timeframe):
                # The exchange's running total restarts at zero every morning, so
                # for the session's first bar the baseline is known exactly.
                baseline = 0
            else:
                # Cold start part-way through a session: rebase on this tick so
                # the bar reports volume traded from here, and flag it estimated
                # rather than claiming the whole morning's turnover.
                baseline = tick.volume_traded_today or 0
                estimated = tick.volume_traded_today is not None
            self._baseline[key] = baseline
        return _BarState(
            bar_start=bar_start,
            open=tick.last_price,
            high=tick.last_price,
            low=tick.last_price,
            close=tick.last_price,
            baseline_cumulative_volume=baseline,
            last_cumulative_volume=tick.volume_traded_today,
            summed_volume=tick.last_quantity or 0,
            last_tick_ts=ts,
            volume_estimated=estimated,
            open_interest=tick.open_interest,
        )

    def _close_bar(self, key: str) -> Candle | None:
        state = self._bars.pop(key, None)
        if state is None:
            return None
        if state.last_cumulative_volume is not None:
            self._baseline[key] = state.last_cumulative_volume
        return self._emit(key, state)

    def attach_mid_session(self, instrument_key: str) -> None:
        """Declare the cumulative-volume baseline unknown for this instrument.

        Call this when subscribing after the session has already begun, so the
        first bar is flagged ``volume_estimated`` instead of reporting the whole
        morning's turnover.
        """
        self._baseline[instrument_key] = None

    def flush(self, instrument_key: str) -> Candle | None:
        """Close the in-progress bar for one instrument."""
        return self._close_bar(instrument_key)

    def flush_all(self) -> list[Candle]:
        """Close every in-progress bar. Called at the session close."""
        candles: list[Candle] = []
        for key in list(self._bars):
            candle = self._close_bar(key)
            if candle is not None:
                candles.append(candle)
        return candles

    def _emit(self, instrument_key: str, state: _BarState) -> Candle:
        candle = self._to_candle(instrument_key, state, closed=True)
        self.stats.candles_emitted += 1
        if self._on_candle is not None:
            self._on_candle(candle)
        return candle

    def _to_candle(self, instrument_key: str, state: _BarState, *, closed: bool) -> Candle:
        return Candle(
            instrument_key=instrument_key,
            timeframe=self._timeframe,
            ts=state.bar_start,
            open=state.open,
            high=state.high,
            low=state.low,
            close=state.close,
            volume=state.volume(),
            open_interest=state.open_interest,
            closed=closed,
            partial=self._calendar.is_partial_bar(state.bar_start, self._timeframe),
            volume_estimated=state.volume_estimated,
        )
