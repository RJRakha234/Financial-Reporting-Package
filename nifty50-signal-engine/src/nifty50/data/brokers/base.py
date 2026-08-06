"""The broker abstraction.

Nothing downstream of this module imports a broker SDK. That is what makes the
vendor swappable, but it is also the seam where the project's central
constraint is enforced: :class:`BrokerAdapter` has no order-placement surface,
and :func:`assert_read_only` refuses to accept a subclass that grows one.

This is a structural guarantee rather than a note in the README. Adding
execution to this engine requires deleting a check that exists specifically to
make you stop and read PART 8 of the spec first.
"""

from __future__ import annotations

import datetime as dt
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import pandas as pd

from nifty50.domain import Exchange, Instrument, Tick, Timeframe

# Any public attribute matching one of these is an execution surface. The engine
# refuses to load an adapter that exposes one.
_FORBIDDEN_METHOD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^place_",
        r"^modify_",
        r"^cancel_",
        r"^exit_",
        r"^square_off",
        r".*_order$",
        r"^orders?$",
        r"^place$",
        r"^gtt",
        r"^basket",
    )
)


class ReadOnlyViolationError(RuntimeError):
    """Raised when a broker adapter exposes an order-placement surface.

    See PART 8 (Regulatory). This engine is alert-only by design; that posture
    is what keeps it outside SEBI's algo-registration perimeter. Do not silence
    this error — if execution is genuinely wanted, the compliance implications
    have to be worked through first.
    """


class StreamMode(StrEnum):
    """Subscription depth. Higher modes cost bandwidth and count against caps."""

    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"  # includes market depth


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Auth state. Indian broker tokens expire daily, so this is a live concern."""

    valid: bool
    expires_at: dt.datetime | None
    message: str = ""

    def seconds_remaining(self, now: dt.datetime) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - now).total_seconds()


@dataclass(slots=True)
class StreamCallbacks:
    """Hooks the supervisor installs on an adapter's stream.

    Deliberately plain callables: broker SDKs deliver ticks on their own threads,
    and the supervisor is responsible for getting them onto the event loop.
    """

    on_ticks: Callable[[list[Tick]], None] = lambda ticks: None
    on_connect: Callable[[], None] = lambda: None
    on_close: Callable[[int | None, str | None], None] = lambda code, reason: None
    on_error: Callable[[BaseException], None] = lambda exc: None


@dataclass(frozen=True, slots=True)
class HistoricalRequest:
    """One chunk of a historical backfill, already sized to the vendor's cap."""

    instrument: Instrument
    timeframe: Timeframe
    start: dt.datetime
    end: dt.datetime


class BrokerAdapter(ABC):
    """Read-only market-data access to one broker.

    Implementations return *raw*, unadjusted bars. Corporate-action adjustment
    happens on read in :mod:`nifty50.corporate_actions`, not here, so that a
    vendor's adjustment policy (or lack of one) never becomes an invisible
    dependency of the backtest.
    """

    name: str = "abstract"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        assert_read_only(cls)

    # ----------------------------------------------------------------- auth

    @abstractmethod
    def authenticate(self) -> TokenStatus:
        """Establish or refresh a market-data session."""

    @abstractmethod
    def token_status(self) -> TokenStatus:
        """Current token validity, without performing a round trip if avoidable."""

    # ---------------------------------------------------------- instruments

    @abstractmethod
    def list_instruments(self, exchange: Exchange) -> list[Instrument]:
        """The vendor's full instrument master for an exchange."""

    @abstractmethod
    def resolve(self, symbol: str, exchange: Exchange) -> Instrument:
        """Map a trading symbol to an :class:`Instrument` with a broker token."""

    # ------------------------------------------------------------- history

    @abstractmethod
    def historical_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> pd.DataFrame:
        """Raw OHLCV bars for ``[start, end]``, indexed by tz-aware IST bar start.

        Callers must respect :meth:`historical_chunk_days`; adapters should raise
        rather than silently truncate an over-long span.
        """

    @abstractmethod
    def historical_chunk_days(self, timeframe: Timeframe) -> int:
        """Vendor cap on the span of a single historical request, in days."""

    @property
    def historical_requests_per_second(self) -> float:
        """Vendor REST quota for historical requests. ``0`` means unlimited.

        Declared by the adapter rather than inferred from the config by callers,
        so a new vendor's quota arrives with its adapter instead of as a special
        case in the backfiller.
        """
        return 0.0

    # -------------------------------------------------------------- quotes

    @abstractmethod
    def quote(self, instruments: list[Instrument]) -> dict[str, Tick]:
        """Full snapshot quotes keyed by :attr:`Instrument.key`."""

    @abstractmethod
    def ltp(self, instruments: list[Instrument]) -> dict[str, float]:
        """Last traded price keyed by :attr:`Instrument.key`."""

    # -------------------------------------------------------------- stream

    @abstractmethod
    def open_stream(self, callbacks: StreamCallbacks) -> None:
        """Open the market-data websocket and install ``callbacks``."""

    @abstractmethod
    def close_stream(self) -> None:
        """Close the websocket without tearing down the REST session."""

    @abstractmethod
    def subscribe(self, instruments: list[Instrument], mode: StreamMode) -> None:
        """Subscribe to live updates. Must respect :attr:`max_stream_instruments`."""

    @abstractmethod
    def unsubscribe(self, instruments: list[Instrument]) -> None: ...

    @property
    @abstractmethod
    def max_stream_instruments(self) -> int:
        """Vendor cap on instruments per websocket connection."""

    @property
    @abstractmethod
    def connection_state(self) -> ConnectionState: ...


def assert_read_only(candidate: type) -> None:
    """Reject any adapter that exposes an order-placement surface."""
    offenders = sorted(
        attribute
        for attribute in dir(candidate)
        if not attribute.startswith("_")
        and any(pattern.match(attribute) for pattern in _FORBIDDEN_METHOD_PATTERNS)
    )
    if offenders:
        raise ReadOnlyViolationError(
            f"{candidate.__name__} exposes order-related attributes {offenders}. "
            "This engine is alert-only by design (see PART 8 / README 'Regulatory "
            "posture'). Adding execution changes the project's regulatory character "
            "and must not be done by deleting this check."
        )


@dataclass(slots=True)
class InstrumentRegistry:
    """Symbol → :class:`Instrument` cache, so tokens are resolved once per run."""

    _by_key: dict[str, Instrument] = field(default_factory=dict)
    _by_token: dict[int, Instrument] = field(default_factory=dict)

    def add(self, instrument: Instrument) -> None:
        self._by_key[instrument.key] = instrument
        if instrument.broker_token is not None:
            self._by_token[instrument.broker_token] = instrument

    def add_all(self, instruments: list[Instrument]) -> None:
        for instrument in instruments:
            self.add(instrument)

    def by_key(self, key: str) -> Instrument | None:
        return self._by_key.get(key)

    def by_token(self, token: int) -> Instrument | None:
        return self._by_token.get(token)

    def __len__(self) -> int:
        return len(self._by_key)

    def keys(self) -> list[str]:
        return list(self._by_key)
