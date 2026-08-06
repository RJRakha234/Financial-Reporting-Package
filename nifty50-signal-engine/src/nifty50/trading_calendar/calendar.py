"""The NSE session model.

This module exists because Indian equities are not a 24/7 tape. Everything
downstream — bar aggregation, gap detection, indicator windows, backtest event
loops — asks the calendar "is this a real bar time?" rather than assuming that
adding 15 minutes to a timestamp lands somewhere meaningful.

Three ideas carry the weight:

``bar_starts(date, timeframe)``
    The exact set of bar-start timestamps the exchange should produce that day.
    Gap detection is then set difference against reality, not heuristics.

Ragged final bars
    The continuous session is 375 minutes. That divides evenly by 1, 5 and 15
    but not by 60, so the last hourly bar of every NSE day is a 15-minute stub.
    It is emitted and flagged ``partial`` rather than dropped or silently
    stretched, because dropping it loses the closing auction run-up and
    stretching it fabricates 45 minutes of price action.

Muhurat sessions
    A one-hour evening session that can land on a Saturday. It produces real
    bars, so it must be in the calendar; it is nothing like a normal day, so it
    is flagged ``tradeable=False`` and excluded from indicator windows by
    default.
"""

from __future__ import annotations

import csv
import datetime as dt
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from nifty50.config import Config
from nifty50.domain import IST, SessionPhase, Timeframe, ensure_ist

_WEEKEND_DAYS: Final[frozenset[int]] = frozenset({5, 6})  # Saturday, Sunday
_COMMENT_PREFIX: Final[str] = "#"


class NotATradingDayError(ValueError):
    """Raised when a schedule is requested for a day the exchange is shut."""


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A half-open time window ``[start, end)``, tz-aware in IST."""

    start: dt.datetime
    end: dt.datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_ist(self.start))
        object.__setattr__(self, "end", ensure_ist(self.end))
        if self.end <= self.start:
            raise ValueError(f"session window end {self.end} not after start {self.start}")

    def contains(self, ts: dt.datetime) -> bool:
        return self.start <= ensure_ist(ts) < self.end

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class SpecialSession:
    """A non-standard session (muhurat, special live session)."""

    date: dt.date
    label: str
    continuous_start: dt.time
    continuous_end: dt.time
    pre_open_start: dt.time | None
    pre_open_end: dt.time | None
    post_close_start: dt.time | None
    post_close_end: dt.time | None
    tradeable: bool
    source: str


@dataclass(frozen=True, slots=True)
class DaySchedule:
    """Everything about one exchange day."""

    date: dt.date
    continuous: SessionWindow
    pre_open: SessionWindow | None
    post_close: SessionWindow | None
    is_special: bool
    label: str | None
    tradeable: bool

    @property
    def open(self) -> dt.datetime:
        return self.continuous.start

    @property
    def close(self) -> dt.datetime:
        return self.continuous.end


@dataclass(frozen=True, slots=True)
class HolidayRecord:
    date: dt.date
    name: str
    confidence: str
    source: str


class TradingCalendar:
    """Holiday-aware NSE session calendar.

    Construct via :meth:`from_config`; the constructor is kept explicit so tests
    can build calendars from literal data without touching the filesystem.
    """

    def __init__(
        self,
        *,
        holidays: dict[dt.date, HolidayRecord],
        special_sessions: dict[dt.date, SpecialSession],
        pre_open: tuple[dt.time, dt.time],
        continuous: tuple[dt.time, dt.time],
        post_close: tuple[dt.time, dt.time],
        anchor_intraday_bars_to_session_open: bool = True,
    ) -> None:
        self._holidays = holidays
        self._special = special_sessions
        self._pre_open = pre_open
        self._continuous = continuous
        self._post_close = post_close
        self._anchor_to_open = anchor_intraday_bars_to_session_open
        # Sorted list backing previous/next lookups without scanning day by day.
        self._sorted_holidays: list[dt.date] = sorted(holidays)

    # ------------------------------------------------------------------ build

    @classmethod
    def from_config(cls, config: Config) -> TradingCalendar:
        cal = config.calendar
        return cls(
            holidays=load_holidays(config.path(cal.holidays_file)),
            special_sessions=load_special_sessions(config.path(cal.special_sessions_file)),
            pre_open=(cal.sessions.pre_open.start, cal.sessions.pre_open.end),
            continuous=(cal.sessions.continuous.start, cal.sessions.continuous.end),
            post_close=(cal.sessions.post_close.start, cal.sessions.post_close.end),
            anchor_intraday_bars_to_session_open=cal.anchor_intraday_bars_to_session_open,
        )

    # ------------------------------------------------------------- predicates

    def is_weekend(self, day: dt.date) -> bool:
        return day.weekday() in _WEEKEND_DAYS

    def is_holiday(self, day: dt.date) -> bool:
        return day in self._holidays

    def holiday(self, day: dt.date) -> HolidayRecord | None:
        return self._holidays.get(day)

    def special_session(self, day: dt.date) -> SpecialSession | None:
        return self._special.get(day)

    def is_trading_day(self, day: dt.date) -> bool:
        """True when the standard continuous session runs on ``day``.

        A muhurat evening on Diwali is *not* a trading day by this definition:
        the regular market is shut. Use :meth:`has_any_session` when you care
        about "were there any bars at all".
        """
        return not self.is_weekend(day) and not self.is_holiday(day)

    def has_any_session(self, day: dt.date) -> bool:
        return self.is_trading_day(day) or day in self._special

    # -------------------------------------------------------------- schedules

    def schedule(self, day: dt.date) -> DaySchedule:
        """Return the session windows for ``day``.

        Raises :class:`NotATradingDayError` when the exchange is shut, so that
        "never compute a signal on a non-trading day" is enforced by the type of
        failure rather than by everyone remembering to check first.
        """
        special = self._special.get(day)
        if special is not None:
            return self._special_schedule(special)
        if not self.is_trading_day(day):
            reason = "weekend" if self.is_weekend(day) else "holiday"
            holiday = self._holidays.get(day)
            detail = f" ({holiday.name})" if holiday else ""
            raise NotATradingDayError(f"{day.isoformat()} is a {reason}{detail}")
        return DaySchedule(
            date=day,
            continuous=self._window(day, *self._continuous),
            pre_open=self._window(day, *self._pre_open),
            post_close=self._window(day, *self._post_close),
            is_special=False,
            label=None,
            tradeable=True,
        )

    def _special_schedule(self, special: SpecialSession) -> DaySchedule:
        pre_open = None
        if special.pre_open_start is not None and special.pre_open_end is not None:
            pre_open = self._window(special.date, special.pre_open_start, special.pre_open_end)
        post_close = None
        if special.post_close_start is not None and special.post_close_end is not None:
            post_close = self._window(
                special.date, special.post_close_start, special.post_close_end
            )
        return DaySchedule(
            date=special.date,
            continuous=self._window(special.date, special.continuous_start, special.continuous_end),
            pre_open=pre_open,
            post_close=post_close,
            is_special=True,
            label=special.label,
            tradeable=special.tradeable,
        )

    @staticmethod
    def _window(day: dt.date, start: dt.time, end: dt.time) -> SessionWindow:
        return SessionWindow(
            start=dt.datetime.combine(day, start, tzinfo=IST),
            end=dt.datetime.combine(day, end, tzinfo=IST),
        )

    # ------------------------------------------------------------ phase / now

    def phase(self, ts: dt.datetime) -> SessionPhase:
        """Which session phase ``ts`` falls in. The engine's market-state source."""
        ts = ensure_ist(ts)
        if not self.has_any_session(ts.date()):
            return SessionPhase.CLOSED
        schedule = self.schedule(ts.date())
        if schedule.pre_open is not None and schedule.pre_open.contains(ts):
            return SessionPhase.PRE_OPEN
        if schedule.continuous.contains(ts):
            return SessionPhase.CONTINUOUS
        if schedule.post_close is not None and schedule.post_close.contains(ts):
            return SessionPhase.POST_CLOSE
        return SessionPhase.CLOSED

    def session_date(self, ts: dt.datetime) -> dt.date | None:
        """The exchange day a timestamp belongs to, or ``None`` outside sessions.

        NSE sessions never cross midnight, so this is just the date — but going
        through the calendar keeps callers from assuming that and breaking if a
        late special session is ever added.
        """
        ts = ensure_ist(ts)
        day = ts.date()
        if not self.has_any_session(day):
            return None
        schedule = self.schedule(day)
        earliest = schedule.pre_open.start if schedule.pre_open else schedule.continuous.start
        latest = schedule.post_close.end if schedule.post_close else schedule.continuous.end
        return day if earliest <= ts < latest else None

    # ---------------------------------------------------------- day arithmetic

    def trading_days(
        self, start: dt.date, end: dt.date, *, include_special: bool = False
    ) -> list[dt.date]:
        """Trading days in ``[start, end]`` inclusive."""
        if end < start:
            return []
        days: list[dt.date] = []
        day = start
        one = dt.timedelta(days=1)
        while day <= end:
            if self.is_trading_day(day) or (include_special and day in self._special):
                days.append(day)
            day += one
        return days

    def previous_trading_day(self, day: dt.date, n: int = 1) -> dt.date:
        if n < 1:
            raise ValueError("n must be >= 1")
        cursor = day
        remaining = n
        one = dt.timedelta(days=1)
        while remaining > 0:
            cursor -= one
            if self.is_trading_day(cursor):
                remaining -= 1
        return cursor

    def next_trading_day(self, day: dt.date, n: int = 1) -> dt.date:
        if n < 1:
            raise ValueError("n must be >= 1")
        cursor = day
        remaining = n
        one = dt.timedelta(days=1)
        while remaining > 0:
            cursor += one
            if self.is_trading_day(cursor):
                remaining -= 1
        return cursor

    def sessions_between(self, start: dt.date, end: dt.date) -> int:
        """Count of trading days in ``(start, end]`` — the T+N settlement clock."""
        if end <= start:
            return 0
        return len(self.trading_days(start + dt.timedelta(days=1), end))

    def holidays_in(self, start: dt.date, end: dt.date) -> list[HolidayRecord]:
        left = bisect_left(self._sorted_holidays, start)
        right = bisect_right(self._sorted_holidays, end)
        return [self._holidays[d] for d in self._sorted_holidays[left:right]]

    # ------------------------------------------------------------- bar grid

    def bar_starts(self, day: dt.date, timeframe: Timeframe) -> list[dt.datetime]:
        """Bar-start timestamps the exchange should produce on ``day``.

        This is the ground truth for gap detection. A daily bar is stamped at the
        continuous-session open, not at midnight: bars are start-stamped
        throughout the engine, and a daily bar stamped 00:00 would look available
        nine hours before the session it summarises had even begun.
        """
        schedule = self.schedule(day)
        if timeframe is Timeframe.D1:
            return [schedule.continuous.start]
        step = timeframe.duration
        anchor = schedule.continuous.start if self._anchor_to_open else _midnight(day)
        starts: list[dt.datetime] = []
        cursor = anchor
        while cursor < schedule.continuous.end:
            if cursor >= schedule.continuous.start:
                starts.append(cursor)
            cursor += step
        return starts

    def bar_end(self, bar_start: dt.datetime, timeframe: Timeframe) -> dt.datetime:
        """When a bar completes. Clamped to the session close for stub bars."""
        bar_start = ensure_ist(bar_start)
        schedule = self.schedule(bar_start.date())
        if timeframe is Timeframe.D1:
            return schedule.continuous.end
        return min(bar_start + timeframe.duration, schedule.continuous.end)

    def is_partial_bar(self, bar_start: dt.datetime, timeframe: Timeframe) -> bool:
        """True for the ragged stub at the end of the session (1h: 15:15-15:30)."""
        if timeframe is Timeframe.D1:
            return False
        bar_start = ensure_ist(bar_start)
        schedule = self.schedule(bar_start.date())
        return bar_start + timeframe.duration > schedule.continuous.end

    def floor_to_bar(self, ts: dt.datetime, timeframe: Timeframe) -> dt.datetime:
        """The start of the bar containing ``ts``.

        Raises :class:`NotATradingDayError` outside the continuous session: a
        tick at 15:47 belongs to no bar, and quietly rounding it into the 15:15
        bar would fold closing-session prints into the regular tape.
        """
        ts = ensure_ist(ts)
        schedule = self.schedule(ts.date())
        if not schedule.continuous.contains(ts):
            raise NotATradingDayError(
                f"{ts.isoformat()} is outside the continuous session for {ts.date().isoformat()}"
            )
        if timeframe is Timeframe.D1:
            return schedule.continuous.start
        elapsed = ts - schedule.continuous.start
        step = timeframe.duration
        buckets = int(elapsed // step)
        return schedule.continuous.start + buckets * step

    def is_session_open_bar(self, bar_start: dt.datetime, timeframe: Timeframe) -> bool:
        """True when this is the first bar of its session.

        The overnight-gap feature and every "do not roll a window across the
        session boundary" guard hang off this predicate.
        """
        bar_start = ensure_ist(bar_start)
        return bar_start == self.schedule(bar_start.date()).continuous.start

    def bars_per_session(self, day: dt.date, timeframe: Timeframe) -> int:
        return len(self.bar_starts(day, timeframe))

    def expected_bar_starts(
        self, start: dt.date, end: dt.date, timeframe: Timeframe, *, include_special: bool = False
    ) -> list[dt.datetime]:
        """Full expected bar grid across a date range."""
        starts: list[dt.datetime] = []
        for day in self.trading_days(start, end, include_special=include_special):
            starts.extend(self.bar_starts(day, timeframe))
        return starts


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0, 0), tzinfo=IST)


def _rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV, skipping ``#`` comment lines that precede the header."""
    with path.open("r", encoding="utf-8") as handle:
        lines = [ln for ln in handle if not ln.lstrip().startswith(_COMMENT_PREFIX)]
    return list(csv.DictReader(lines))


def load_holidays(path: Path) -> dict[dt.date, HolidayRecord]:
    records: dict[dt.date, HolidayRecord] = {}
    for row in _rows(path):
        day = dt.date.fromisoformat(row["date"].strip())
        records[day] = HolidayRecord(
            date=day,
            name=row["name"].strip(),
            confidence=row["confidence"].strip(),
            source=row["source"].strip(),
        )
    return records


def load_special_sessions(path: Path) -> dict[dt.date, SpecialSession]:
    sessions: dict[dt.date, SpecialSession] = {}
    for row in _rows(path):
        day = dt.date.fromisoformat(row["date"].strip())
        sessions[day] = SpecialSession(
            date=day,
            label=row["label"].strip(),
            continuous_start=_time(row["continuous_start"]),
            continuous_end=_time(row["continuous_end"]),
            pre_open_start=_optional_time(row["pre_open_start"]),
            pre_open_end=_optional_time(row["pre_open_end"]),
            post_close_start=_optional_time(row["post_close_start"]),
            post_close_end=_optional_time(row["post_close_end"]),
            tradeable=row["tradeable"].strip().lower() == "true",
            source=row["source"].strip(),
        )
    return sessions


def _time(value: str) -> dt.time:
    return dt.time.fromisoformat(value.strip())


def _optional_time(value: str) -> dt.time | None:
    stripped = value.strip()
    return dt.time.fromisoformat(stripped) if stripped else None


@lru_cache(maxsize=1)
def get_calendar() -> TradingCalendar:
    """Process-wide calendar built from the active configuration."""
    from nifty50.config import get_config

    return TradingCalendar.from_config(get_config())
