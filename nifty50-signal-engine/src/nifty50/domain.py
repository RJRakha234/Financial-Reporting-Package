"""Core domain types shared by every layer.

Two invariants are enforced here rather than by convention:

1. Every timestamp that crosses a module boundary is tz-aware in IST.
2. A :class:`Timeframe` knows its own duration, so no module has to hardcode
   "15m == 900 seconds" anywhere.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Column contract for every OHLCV frame in the system.
OHLCV_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
BAR_INDEX_NAME: Final[str] = "ts"


class NaiveDatetimeError(ValueError):
    """Raised when a naive datetime reaches a boundary that requires tz-awareness."""


def ensure_ist(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as a tz-aware IST datetime.

    Naive datetimes are rejected rather than silently localised: a naive value
    almost always means someone lost the timezone upstream, and guessing IST
    would bury the bug instead of surfacing it.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(f"naive datetime is not allowed: {value!r}")
    return value.astimezone(IST)


def now_ist() -> dt.datetime:
    """Current wall-clock time in IST. The only sanctioned source of 'now'."""
    return dt.datetime.now(tz=IST)


class Timeframe(StrEnum):
    """Supported bar sizes.

    ``D1`` is a session bar, not a 24h bar: its duration is defined by the
    trading calendar, so :attr:`duration` is undefined for it on purpose.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"

    @property
    def is_intraday(self) -> bool:
        return self is not Timeframe.D1

    @property
    def duration(self) -> dt.timedelta:
        """Bar duration for intraday timeframes."""
        if self is Timeframe.D1:
            raise ValueError(
                "Timeframe.D1 has no fixed duration; a session bar is defined by the "
                "trading calendar, not by a timedelta."
            )
        return _INTRADAY_DURATIONS[self]

    @property
    def minutes(self) -> int:
        return int(self.duration.total_seconds() // 60)


_INTRADAY_DURATIONS: Final[dict[Timeframe, dt.timedelta]] = {
    Timeframe.M1: dt.timedelta(minutes=1),
    Timeframe.M5: dt.timedelta(minutes=5),
    Timeframe.M30: dt.timedelta(minutes=30),
    Timeframe.M15: dt.timedelta(minutes=15),
    Timeframe.H1: dt.timedelta(hours=1),
}


class SessionPhase(StrEnum):
    """Where a timestamp sits relative to the NSE trading day."""

    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    CONTINUOUS = "continuous"
    POST_CLOSE = "post_close"

    @property
    def is_tradeable(self) -> bool:
        """Only the continuous session produces bars we will signal on."""
        return self is SessionPhase.CONTINUOUS


class InstrumentKind(StrEnum):
    EQUITY = "equity"
    INDEX = "index"
    FUTURE = "future"
    OPTION = "option"


class Exchange(StrEnum):
    NSE = "NSE"
    NFO = "NFO"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable or observable symbol, decoupled from any broker's token scheme."""

    symbol: str
    exchange: Exchange
    kind: InstrumentKind
    broker_token: int | None = None
    name: str | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    isin: str | None = None

    @property
    def key(self) -> str:
        """Stable identity used for storage paths and cross-broker mapping."""
        return f"{self.exchange.value}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: float
    quantity: int
    orders: int


@dataclass(frozen=True, slots=True)
class Tick:
    """One market-data update. ``ts`` is the exchange timestamp when available."""

    instrument_key: str
    ts: dt.datetime
    last_price: float
    last_quantity: int | None = None
    volume_traded_today: int | None = None
    average_traded_price: float | None = None
    total_buy_quantity: int | None = None
    total_sell_quantity: int | None = None
    open_interest: int | None = None
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()
    received_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_ist(self.ts))
        if self.received_at is not None:
            object.__setattr__(self, "received_at", ensure_ist(self.received_at))

    @property
    def lag(self) -> dt.timedelta | None:
        """Exchange-to-local latency, the number the dashboard health panel shows."""
        if self.received_at is None:
            return None
        return self.received_at - self.ts

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price - self.bids[0].price


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV bar, stamped with the START of its interval.

    Start-stamping is a deliberate choice and it is load-bearing for look-ahead
    safety: a bar labelled 10:15 on a 15m series covers 10:15:00 to 10:29:59 and is
    only complete at 10:30. Anything consuming a bar must also check
    :attr:`closed` before treating it as history.
    """

    instrument_key: str
    timeframe: Timeframe
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int | None = None
    closed: bool = True
    # True for the ragged end-of-session stub bar (e.g. 15:15-15:30 on an
    # hourly series). Emitted and flagged, never dropped or stretched.
    partial: bool = False
    # True when volume was inferred without a known cumulative baseline — the
    # usual cause is the engine starting mid-session. Such bars are safe for
    # price features and must not be used for volume z-scores or OBV.
    volume_estimated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_ist(self.ts))
        if self.high < self.low:
            raise ValueError(f"high < low in candle {self.instrument_key} @ {self.ts}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"open outside [low, high] in candle {self.instrument_key} @ {self.ts}"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"close outside [low, high] in candle {self.instrument_key} @ {self.ts}"
            )
        if self.volume < 0:
            raise ValueError(f"negative volume in candle {self.instrument_key} @ {self.ts}")
