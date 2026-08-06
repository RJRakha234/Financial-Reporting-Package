"""Historical backfill: chunked, rate-limited and resumable.

Resume semantics deserve a note. The backfiller restarts from the *start of the
last stored day* rather than from the last stored bar. Vendors revise the tail
of a session after the close — late prints, auction trades and corrections all
land after the fact — so re-fetching the final day costs one request and avoids
freezing a provisional bar into the store forever.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from nifty50.config import Config
from nifty50.data.brokers.base import BrokerAdapter, HistoricalRequest
from nifty50.data.store import BarStore
from nifty50.domain import Instrument, Timeframe, ensure_ist, now_ist
from nifty50.frames import ist_index
from nifty50.logging_setup import get_logger
from nifty50.trading_calendar.calendar import TradingCalendar

log = get_logger(__name__)


@dataclass(slots=True)
class BackfillResult:
    instrument_key: str
    timeframe: Timeframe
    bars_written: int = 0
    requests_made: int = 0
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


class _RateLimiter:
    """Minimum-interval limiter. Broker REST quotas are per-second and strict."""

    def __init__(self, max_per_second: float, *, sleeper: object = None) -> None:
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._last_call = 0.0
        self._sleep = sleeper if callable(sleeper) else time.sleep

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        wait = self._min_interval - elapsed
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()


class Backfiller:
    """Pulls history from a broker into the :class:`BarStore`."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        store: BarStore,
        calendar: TradingCalendar,
        config: Config,
        *,
        sleeper: object = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._calendar = calendar
        self._config = config
        self._limiter = _RateLimiter(adapter.historical_requests_per_second, sleeper=sleeper)

    # --------------------------------------------------------------- planning

    def default_start(self, end: dt.date | None = None) -> dt.date:
        """Earliest date the configured history window reaches back to."""
        anchor = end or now_ist().date()
        return anchor - dt.timedelta(days=365 * self._config.data.history_years)

    def plan(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: dt.date,
        end: dt.date,
    ) -> list[HistoricalRequest]:
        """Split ``[start, end]`` into vendor-legal chunks, skipping closed days."""
        cap = self._adapter.historical_chunk_days(timeframe)
        requests: list[HistoricalRequest] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + dt.timedelta(days=cap - 1), end)
            days = self._calendar.trading_days(cursor, chunk_end)
            if days:
                schedule_first = self._calendar.schedule(days[0])
                schedule_last = self._calendar.schedule(days[-1])
                requests.append(
                    HistoricalRequest(
                        instrument=instrument,
                        timeframe=timeframe,
                        start=schedule_first.continuous.start,
                        end=schedule_last.continuous.end,
                    )
                )
            cursor = chunk_end + dt.timedelta(days=1)
        return requests

    def resume_point(
        self, instrument: Instrument, timeframe: Timeframe, default_start: dt.date
    ) -> dt.date:
        last = self._store.last_bar_ts(instrument.exchange, instrument.symbol, timeframe)
        if last is None:
            return default_start
        # Re-fetch the whole final stored day; see the module docstring.
        return ensure_ist(last).date()

    # -------------------------------------------------------------- execution

    def backfill(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        resume: bool = True,
    ) -> BackfillResult:
        result = BackfillResult(instrument_key=instrument.key, timeframe=timeframe)
        end_date = end or now_ist().date()
        begin = start or self.default_start(end_date)
        if resume:
            begin = max(begin, self.resume_point(instrument, timeframe, begin))
        if begin > end_date:
            result.skipped = True
            result.skip_reason = "store is already current"
            return result

        for request in self.plan(instrument, timeframe, begin, end_date):
            self._limiter.acquire()
            try:
                frame = self._adapter.historical_candles(
                    request.instrument, request.timeframe, request.start, request.end
                )
            except Exception as exc:
                message = f"{request.start.date()}..{request.end.date()}: {exc}"
                result.errors.append(message)
                log.warning(
                    "backfill.chunk_failed",
                    instrument=instrument.key,
                    timeframe=timeframe.value,
                    error=str(exc),
                )
                continue
            result.requests_made += 1
            if frame.empty:
                continue
            written = self._store.write(instrument.exchange, instrument.symbol, timeframe, frame)
            result.bars_written += written
            first = ensure_ist(frame.index.min().to_pydatetime())
            last = ensure_ist(frame.index.max().to_pydatetime())
            result.first_ts = min(result.first_ts or first, first)
            result.last_ts = max(result.last_ts or last, last)

        log.info(
            "backfill.done",
            instrument=instrument.key,
            timeframe=timeframe.value,
            bars=result.bars_written,
            requests=result.requests_made,
            errors=len(result.errors),
        )
        return result

    def backfill_many(
        self,
        instruments: Sequence[Instrument],
        timeframes: Sequence[Timeframe],
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        resume: bool = True,
    ) -> list[BackfillResult]:
        results: list[BackfillResult] = []
        for instrument in instruments:
            for timeframe in timeframes:
                results.append(
                    self.backfill(instrument, timeframe, start=start, end=end, resume=resume)
                )
        return results


def daily_from_intraday(frame: pd.DataFrame, calendar: TradingCalendar) -> pd.DataFrame:
    """Roll intraday bars up to session bars.

    Used to cross-check a vendor's daily series against its own intraday series;
    a systematic disagreement usually means the vendor's daily bar includes
    pre-open or closing-session prints that the intraday series excludes.
    """
    if frame.empty:
        return frame
    grouped = frame.groupby(ist_index(frame).date)
    daily = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    index = pd.DatetimeIndex(
        [calendar.schedule(day).continuous.start for day in daily.index], name="ts"
    )
    daily.index = index
    return daily
