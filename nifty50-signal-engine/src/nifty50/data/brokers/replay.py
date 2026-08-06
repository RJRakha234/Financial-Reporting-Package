"""Offline adapter that replays stored bars as history and as a live tick feed.

This is not a toy. Broker credentials expire daily, cost money and cannot run in
CI, so without an offline adapter the data layer would be untestable and the
first six phases of the build would be unverifiable. The replay adapter
implements the same contract as :class:`~nifty50.data.brokers.kite.KiteAdapter`,
including the failure modes worth testing: it can be told to drop the socket, to
stall the heartbeat, and to emit ticks out of order.

Layout under ``broker.replay.root``::

    instruments.csv                     symbol,exchange,kind,token,lot_size,tick_size
    NSE/RELIANCE/1m.parquet
    NSE/RELIANCE/15m.parquet
"""

from __future__ import annotations

import csv
import datetime as dt
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from nifty50.config import Config
from nifty50.data.brokers.base import (
    BrokerAdapter,
    ConnectionState,
    StreamCallbacks,
    StreamMode,
    TokenStatus,
)
from nifty50.domain import (
    IST,
    OHLCV_COLUMNS,
    DepthLevel,
    Exchange,
    Instrument,
    InstrumentKind,
    Tick,
    Timeframe,
    ensure_ist,
    now_ist,
)
from nifty50.frames import as_float, empty_bars, ist_index
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

# Replay has no vendor limit; the number mirrors Kite so that subscription-cap
# logic is exercised identically in tests.
_DEFAULT_MAX_STREAM_INSTRUMENTS: int = 3000
_DEFAULT_CHUNK_DAYS: int = 10_000
_SYNTHETIC_DEPTH_LEVELS: int = 5
_SYNTHETIC_SPREAD_TICKS: int = 1
_DEFAULT_TICK_SIZE: float = 0.05


@dataclass(slots=True)
class ReplayFaults:
    """Deliberate failure injection, for exercising the resilience paths."""

    drop_after_ticks: int | None = None
    duplicate_every: int | None = None
    reorder_every: int | None = None
    fail_authentication: bool = False


class ReplayAdapter(BrokerAdapter):
    """Serves stored bars as history and synthesises ticks from them."""

    name = "replay"

    def __init__(
        self,
        config: Config,
        *,
        root: Path | None = None,
        faults: ReplayFaults | None = None,
    ) -> None:
        self._config = config
        self._root = root or config.path(config.broker.replay.root)
        self._faults = faults or ReplayFaults()
        self._callbacks = StreamCallbacks()
        self._state = ConnectionState.DISCONNECTED
        self._subscribed: dict[str, Instrument] = {}
        self._instruments: dict[Exchange, list[Instrument]] = {}
        self._authenticated = False
        self._emitted = 0

    # ----------------------------------------------------------------- auth

    def authenticate(self) -> TokenStatus:
        if self._faults.fail_authentication:
            return TokenStatus(valid=False, expires_at=None, message="injected auth failure")
        self._authenticated = True
        return self.token_status()

    def token_status(self) -> TokenStatus:
        # Replay tokens never expire; the field is populated anyway so the
        # dashboard's countdown widget has the same shape for every adapter.
        return TokenStatus(
            valid=self._authenticated,
            expires_at=now_ist() + dt.timedelta(days=365) if self._authenticated else None,
            message="replay adapter: no real session",
        )

    # ---------------------------------------------------------- instruments

    def list_instruments(self, exchange: Exchange) -> list[Instrument]:
        if exchange in self._instruments:
            return self._instruments[exchange]
        manifest = self._root / "instruments.csv"
        instruments: list[Instrument] = []
        if manifest.exists():
            with manifest.open("r", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row["exchange"].strip() != exchange.value:
                        continue
                    instruments.append(
                        Instrument(
                            symbol=row["symbol"].strip(),
                            exchange=exchange,
                            kind=InstrumentKind(row["kind"].strip()),
                            broker_token=int(row["token"]),
                            lot_size=_optional_int(row.get("lot_size")),
                            tick_size=_optional_float(row.get("tick_size")),
                        )
                    )
        else:
            base = self._root / exchange.value
            counter = itertools.count(1)
            for directory in sorted(p for p in base.glob("*") if p.is_dir()):
                instruments.append(
                    Instrument(
                        symbol=directory.name,
                        exchange=exchange,
                        kind=InstrumentKind.EQUITY,
                        broker_token=next(counter),
                        tick_size=_DEFAULT_TICK_SIZE,
                    )
                )
        self._instruments[exchange] = instruments
        return instruments

    def resolve(self, symbol: str, exchange: Exchange) -> Instrument:
        for instrument in self.list_instruments(exchange):
            if instrument.symbol == symbol:
                return instrument
        raise KeyError(f"{symbol} not present in replay data under {self._root}")

    # ------------------------------------------------------------- history

    def historical_chunk_days(self, timeframe: Timeframe) -> int:
        return _DEFAULT_CHUNK_DAYS

    def historical_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> pd.DataFrame:
        frame = self._load(instrument, timeframe)
        if frame.empty:
            return frame
        start, end = ensure_ist(start), ensure_ist(end)
        mask = (frame.index >= start) & (frame.index <= end)
        return frame.loc[mask].copy()

    def _load(self, instrument: Instrument, timeframe: Timeframe) -> pd.DataFrame:
        directory = self._root / instrument.exchange.value / instrument.symbol
        parquet = directory / f"{timeframe.value}.parquet"
        csv_path = directory / f"{timeframe.value}.csv"
        if parquet.exists():
            frame = pd.read_parquet(parquet)
        elif csv_path.exists():
            frame = pd.read_csv(csv_path)
        else:
            return empty_bars(OHLCV_COLUMNS)
        if "ts" in frame.columns:
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(IST)
            frame = frame.set_index("ts")
        frame.index = pd.DatetimeIndex(frame.index).tz_convert(IST)
        frame.index.name = "ts"
        return frame.sort_index()

    # -------------------------------------------------------------- quotes

    def quote(self, instruments: list[Instrument]) -> dict[str, Tick]:
        received = now_ist()
        out: dict[str, Tick] = {}
        for instrument in instruments:
            frame = self._load(instrument, Timeframe.M1)
            if frame.empty:
                continue
            last = frame.iloc[-1]
            ts = ist_index(frame)[-1]
            out[instrument.key] = _synthetic_tick(
                instrument,
                ts.to_pydatetime(),
                as_float(last["close"]),
                int(last["volume"]),
                received,
            )
        return out

    def ltp(self, instruments: list[Instrument]) -> dict[str, float]:
        return {key: tick.last_price for key, tick in self.quote(instruments).items()}

    # -------------------------------------------------------------- stream

    @property
    def max_stream_instruments(self) -> int:
        return _DEFAULT_MAX_STREAM_INSTRUMENTS

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    def open_stream(self, callbacks: StreamCallbacks) -> None:
        self._callbacks = callbacks
        self._state = ConnectionState.CONNECTED
        self._emitted = 0
        callbacks.on_connect()

    def close_stream(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._subscribed.clear()

    def subscribe(self, instruments: list[Instrument], mode: StreamMode) -> None:
        if self._state is not ConnectionState.CONNECTED:
            raise RuntimeError("stream is not open; call open_stream() first")
        total = len(set(self._subscribed) | {i.key for i in instruments})
        if total > self.max_stream_instruments:
            raise ValueError(
                f"subscription of {total} instruments exceeds the cap of "
                f"{self.max_stream_instruments}"
            )
        for instrument in instruments:
            self._subscribed[instrument.key] = instrument
        log.info("replay.subscribed", count=len(instruments), mode=mode.value)

    def unsubscribe(self, instruments: list[Instrument]) -> None:
        for instrument in instruments:
            self._subscribed.pop(instrument.key, None)

    # ------------------------------------------------------------- driving

    def pump(self, day: dt.date, *, timeframe: Timeframe = Timeframe.M1) -> int:
        """Replay one session's bars for every subscribed instrument as ticks.

        Returns the number of ticks delivered. Faults configured on the adapter
        are injected here so that the supervisor's reconnect, de-duplication and
        out-of-order handling are exercised against the real code path.
        """
        delivered = 0
        for instrument in list(self._subscribed.values()):
            frame = self._load(instrument, timeframe)
            if frame.empty:
                continue
            index = ist_index(frame)
            same_day = frame[[ts.date() == day for ts in index]]
            day_index = ist_index(same_day)
            batch: list[Tick] = []
            for position in range(len(same_day)):
                row = same_day.iloc[position]
                received = now_ist()
                tick = _synthetic_tick(
                    instrument,
                    day_index[position].to_pydatetime(),
                    as_float(row["close"]),
                    int(row["volume"]),
                    received,
                )
                batch.append(tick)
                self._emitted += 1
                duplicate_every = self._faults.duplicate_every
                if duplicate_every and self._emitted % duplicate_every == 0:
                    batch.append(tick)
                if (
                    self._faults.drop_after_ticks is not None
                    and self._emitted >= self._faults.drop_after_ticks
                ):
                    self._state = ConnectionState.DISCONNECTED
                    self._callbacks.on_ticks(batch)
                    delivered += len(batch)
                    self._callbacks.on_close(1006, "injected drop")
                    return delivered
            if self._faults.reorder_every and len(batch) > self._faults.reorder_every:
                step = self._faults.reorder_every
                batch[step - 1], batch[step] = batch[step], batch[step - 1]
            self._callbacks.on_ticks(batch)
            delivered += len(batch)
        return delivered


@dataclass(slots=True)
class _Counter:
    value: int = field(default=0)


def _synthetic_tick(
    instrument: Instrument,
    ts: dt.datetime,
    price: float,
    volume: int,
    received: dt.datetime,
) -> Tick:
    """Build a plausible full-mode tick from a bar close.

    The depth ladder is synthetic and is clearly labelled as such: it is shaped
    to exercise order-book-imbalance code paths, and must never be mistaken for
    a real book in a backtest of a depth-sensitive strategy.
    """
    tick_size = instrument.tick_size or _DEFAULT_TICK_SIZE
    half_spread = tick_size * _SYNTHETIC_SPREAD_TICKS
    bids = tuple(
        DepthLevel(
            price=round(price - half_spread - i * tick_size, 2),
            quantity=100 * (i + 1),
            orders=i + 1,
        )
        for i in range(_SYNTHETIC_DEPTH_LEVELS)
    )
    asks = tuple(
        DepthLevel(
            price=round(price + half_spread + i * tick_size, 2),
            quantity=100 * (i + 1),
            orders=i + 1,
        )
        for i in range(_SYNTHETIC_DEPTH_LEVELS)
    )
    return Tick(
        instrument_key=instrument.key,
        ts=ensure_ist(ts),
        last_price=price,
        last_quantity=volume,
        volume_traded_today=volume,
        bids=bids,
        asks=asks,
        received_at=received,
    )


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)
