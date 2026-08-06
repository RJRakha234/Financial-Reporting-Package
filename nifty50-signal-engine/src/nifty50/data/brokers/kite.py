"""Zerodha Kite Connect adapter (market data only).

Chosen as the default vendor for stability and documentation quality, not for
price. Three Kite-specific facts shape this module:

* Access tokens die every morning (~06:00 IST). There is no refresh token — the
  OAuth redirect must be walked daily. The engine therefore treats token expiry
  as an operational event with a countdown, not an exception to be caught.
* ``historical_data`` returns *unadjusted* bars whose adjustment policy is not
  contractual. We take them raw and adjust ourselves.
* Ticks arrive on the SDK's own thread. Nothing in this module touches the
  event loop; the supervisor owns that handoff.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path
from typing import Any, Final

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
    DepthLevel,
    Exchange,
    Instrument,
    InstrumentKind,
    Tick,
    Timeframe,
    ensure_ist,
    now_ist,
)
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

# Kite's own timeframe vocabulary differs from ours.
_KITE_INTERVALS: Final[dict[Timeframe, str]] = {
    Timeframe.M1: "minute",
    Timeframe.M5: "5minute",
    Timeframe.M15: "15minute",
    Timeframe.H1: "60minute",
    Timeframe.D1: "day",
}

_KITE_SEGMENT_TO_KIND: Final[dict[str, InstrumentKind]] = {
    "EQ": InstrumentKind.EQUITY,
    "INDICES": InstrumentKind.INDEX,
    "FUT": InstrumentKind.FUTURE,
    "CE": InstrumentKind.OPTION,
    "PE": InstrumentKind.OPTION,
}


class KiteAuthError(RuntimeError):
    """Raised when no usable market-data session can be established."""


class KiteAdapter(BrokerAdapter):
    """Read-only Kite Connect adapter."""

    name = "kite"

    def __init__(self, config: Config, *, env_path: Path | None = None) -> None:
        self._config = config
        self._settings = config.broker.kite
        self._env_path = env_path or (config.project_root / ".env")
        self._kite: Any = None
        self._ticker: Any = None
        self._callbacks = StreamCallbacks()
        self._state = ConnectionState.DISCONNECTED
        self._lock = threading.Lock()
        self._subscribed: set[int] = set()
        self._instrument_cache: dict[Exchange, list[Instrument]] = {}
        self._token_by_key: dict[str, Instrument] = {}

    # ----------------------------------------------------------------- auth

    def _client(self) -> Any:
        if self._kite is None:
            try:
                from kiteconnect import KiteConnect
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise KiteAuthError(
                    "kiteconnect is not installed. Install the optional extra: "
                    "pip install '.[kite]'"
                ) from exc
            api_key = self._env(self._settings.api_key_env)
            self._kite = KiteConnect(api_key=api_key)
        return self._kite

    @staticmethod
    def _env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise KiteAuthError(f"environment variable {name} is not set (see .env.example)")
        return value

    def authenticate(self) -> TokenStatus:
        """Reuse today's access token, or mint one from a fresh request token.

        Kite has no unattended re-auth: the daily login is an interactive OAuth
        redirect. When no usable token exists we raise with the exact login URL
        rather than blocking on a prompt, so the morning start-up is a one-line
        copy-paste and an automated run fails fast instead of hanging.
        """
        client = self._client()
        existing = os.environ.get(self._settings.access_token_env, "").strip()
        if existing:
            client.set_access_token(existing)
            try:
                client.profile()
            except Exception as exc:
                log.warning("kite.access_token.rejected", error=str(exc))
            else:
                log.info("kite.auth.reused_token")
                return self.token_status()

        request_token = os.environ.get(self._settings.request_token_env, "").strip()
        if not request_token:
            raise KiteAuthError(
                "No valid Kite access token. Kite tokens expire daily and cannot be "
                "refreshed unattended.\n"
                f"  1. Open: {client.login_url()}\n"
                "  2. Log in; copy the `request_token` from the redirect URL\n"
                f"  3. Set {self._settings.request_token_env} in .env and re-run"
            )

        session = client.generate_session(
            request_token, api_secret=self._env(self._settings.api_secret_env)
        )
        access_token = str(session["access_token"])
        client.set_access_token(access_token)
        os.environ[self._settings.access_token_env] = access_token
        self._persist_env(self._settings.access_token_env, access_token)
        # A request token is single-use; leaving it around guarantees a confusing
        # "token already used" failure on the next start.
        self._persist_env(self._settings.request_token_env, "")
        os.environ.pop(self._settings.request_token_env, None)
        log.info("kite.auth.new_session")
        return self.token_status()

    def token_status(self) -> TokenStatus:
        token = os.environ.get(self._settings.access_token_env, "").strip()
        if not token:
            return TokenStatus(valid=False, expires_at=None, message="no access token")
        return TokenStatus(
            valid=True,
            expires_at=self._next_expiry(now_ist()),
            message="kite tokens expire daily; re-auth each morning",
        )

    def _next_expiry(self, now: dt.datetime) -> dt.datetime:
        expiry_time = self._settings.token_expiry_local_time
        candidate = dt.datetime.combine(now.date(), expiry_time, tzinfo=IST)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    def _persist_env(self, key: str, value: str) -> None:
        """Upsert ``key`` in the .env file, creating it if needed."""
        lines: list[str] = []
        if self._env_path.exists():
            lines = self._env_path.read_text(encoding="utf-8").splitlines()
        prefix = f"{key}="
        replaced = False
        for position, line in enumerate(lines):
            if line.startswith(prefix):
                lines[position] = f"{prefix}{value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{prefix}{value}")
        self._env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------------------------------------------------------- instruments

    def list_instruments(self, exchange: Exchange) -> list[Instrument]:
        if exchange in self._instrument_cache:
            return self._instrument_cache[exchange]
        raw: list[dict[str, Any]] = self._client().instruments(exchange.value)
        instruments = [self._to_instrument(row, exchange) for row in raw]
        self._instrument_cache[exchange] = instruments
        for instrument in instruments:
            self._token_by_key[instrument.key] = instrument
        log.info("kite.instruments.loaded", exchange=exchange.value, count=len(instruments))
        return instruments

    @staticmethod
    def _to_instrument(row: dict[str, Any], exchange: Exchange) -> Instrument:
        segment = str(row.get("segment", ""))
        instrument_type = str(row.get("instrument_type", ""))
        kind = _KITE_SEGMENT_TO_KIND.get(instrument_type) or (
            InstrumentKind.INDEX if segment.endswith("INDICES") else InstrumentKind.EQUITY
        )
        return Instrument(
            symbol=str(row["tradingsymbol"]),
            exchange=exchange,
            kind=kind,
            broker_token=int(row["instrument_token"]),
            name=str(row.get("name") or "") or None,
            lot_size=int(row["lot_size"]) if row.get("lot_size") else None,
            tick_size=float(row["tick_size"]) if row.get("tick_size") else None,
        )

    def resolve(self, symbol: str, exchange: Exchange) -> Instrument:
        key = f"{exchange.value}:{symbol}"
        cached = self._token_by_key.get(key)
        if cached is not None:
            return cached
        for instrument in self.list_instruments(exchange):
            if instrument.symbol == symbol:
                return instrument
        raise KeyError(f"{symbol} not found in the {exchange.value} instrument master")

    # ------------------------------------------------------------- history

    def historical_chunk_days(self, timeframe: Timeframe) -> int:
        return self._settings.historical_chunk_days[timeframe]

    @property
    def historical_requests_per_second(self) -> float:
        return float(self._settings.rest_rate_limit_per_second.get("historical", 1))

    def historical_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> pd.DataFrame:
        if instrument.broker_token is None:
            raise ValueError(f"{instrument.key} has no Kite instrument token")
        start, end = ensure_ist(start), ensure_ist(end)
        span_days = (end.date() - start.date()).days
        cap = self.historical_chunk_days(timeframe)
        if span_days > cap:
            raise ValueError(
                f"requested {span_days}d of {timeframe.value} data but the Kite cap is {cap}d; "
                "the backfiller is responsible for chunking"
            )
        rows: list[dict[str, Any]] = self._client().historical_data(
            instrument_token=instrument.broker_token,
            from_date=start,
            to_date=end,
            interval=_KITE_INTERVALS[timeframe],
            continuous=False,
            oi=instrument.kind in (InstrumentKind.FUTURE, InstrumentKind.OPTION),
        )
        return _candles_to_frame(rows)

    # -------------------------------------------------------------- quotes

    def quote(self, instruments: list[Instrument]) -> dict[str, Tick]:
        keys = [f"{i.exchange.value}:{i.symbol}" for i in instruments]
        payload: dict[str, dict[str, Any]] = self._client().quote(keys)
        received = now_ist()
        out: dict[str, Tick] = {}
        for key, data in payload.items():
            out[key] = _quote_to_tick(key, data, received)
        return out

    def ltp(self, instruments: list[Instrument]) -> dict[str, float]:
        keys = [f"{i.exchange.value}:{i.symbol}" for i in instruments]
        payload: dict[str, dict[str, Any]] = self._client().ltp(keys)
        return {key: float(data["last_price"]) for key, data in payload.items()}

    # -------------------------------------------------------------- stream

    @property
    def max_stream_instruments(self) -> int:
        return self._settings.max_ws_instruments

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    def open_stream(self, callbacks: StreamCallbacks) -> None:
        try:
            from kiteconnect import KiteTicker
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise KiteAuthError("kiteconnect is not installed; pip install '.[kite]'") from exc

        self._callbacks = callbacks
        ticker = KiteTicker(
            self._env(self._settings.api_key_env),
            self._env(self._settings.access_token_env),
        )
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        self._ticker = ticker
        self._state = ConnectionState.CONNECTING
        # threaded=True: the SDK owns its own thread. The supervisor bridges to
        # asyncio; blocking the event loop on a vendor socket would stall
        # everything else the engine has to do during the session.
        ticker.connect(threaded=True, disable_ssl_verification=False)

    def close_stream(self) -> None:
        if self._ticker is not None:
            try:
                self._ticker.close()
            finally:
                self._ticker = None
        self._state = ConnectionState.DISCONNECTED
        self._subscribed.clear()

    def subscribe(self, instruments: list[Instrument], mode: StreamMode) -> None:
        if self._ticker is None:
            raise RuntimeError("stream is not open; call open_stream() first")
        tokens = [i.broker_token for i in instruments if i.broker_token is not None]
        with self._lock:
            total = len(self._subscribed | set(tokens))
            if total > self.max_stream_instruments:
                raise ValueError(
                    f"subscription of {total} instruments exceeds the Kite websocket cap of "
                    f"{self.max_stream_instruments}"
                )
            self._ticker.subscribe(tokens)
            self._ticker.set_mode(mode.value, tokens)
            self._subscribed.update(tokens)
        log.info("kite.subscribed", count=len(tokens), mode=mode.value)

    def unsubscribe(self, instruments: list[Instrument]) -> None:
        if self._ticker is None:
            return
        tokens = [i.broker_token for i in instruments if i.broker_token is not None]
        with self._lock:
            self._ticker.unsubscribe(tokens)
            self._subscribed.difference_update(tokens)

    # ------------------------------------------------------- SDK callbacks

    def _on_ticks(self, _ws: Any, ticks: list[dict[str, Any]]) -> None:
        received = now_ist()
        parsed: list[Tick] = []
        for raw in ticks:
            instrument = self._token_by_key_lookup(int(raw["instrument_token"]))
            if instrument is None:
                continue
            parsed.append(_tick_to_domain(instrument.key, raw, received))
        if parsed:
            self._callbacks.on_ticks(parsed)

    def _token_by_key_lookup(self, token: int) -> Instrument | None:
        for instrument in self._token_by_key.values():
            if instrument.broker_token == token:
                return instrument
        return None

    def _on_connect(self, _ws: Any, _response: Any) -> None:
        self._state = ConnectionState.CONNECTED
        self._callbacks.on_connect()

    def _on_close(self, _ws: Any, code: int | None, reason: str | None) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._callbacks.on_close(code, reason)

    def _on_error(self, _ws: Any, code: int | None, reason: str | None) -> None:
        self._callbacks.on_error(RuntimeError(f"kite ticker error {code}: {reason}"))


def _candles_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = ["open", "high", "low", "close", "volume"]
    if not rows:
        empty = pd.DataFrame(columns=columns)
        empty.index = pd.DatetimeIndex([], tz=IST, name="ts")
        return empty
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["date"], utc=True).dt.tz_convert(IST)
    frame = frame.set_index("ts").sort_index()
    if "oi" in frame.columns:
        columns = [*columns, "oi"]
    return frame[columns]


def _quote_to_tick(key: str, data: dict[str, Any], received: dt.datetime) -> Tick:
    depth = data.get("depth") or {}
    timestamp = data.get("timestamp") or data.get("last_trade_time")
    ts = _parse_exchange_time(timestamp, received)
    return Tick(
        instrument_key=key,
        ts=ts,
        last_price=float(data["last_price"]),
        last_quantity=_optional_int(data.get("last_quantity")),
        volume_traded_today=_optional_int(data.get("volume")),
        average_traded_price=_optional_float(data.get("average_price")),
        total_buy_quantity=_optional_int(data.get("buy_quantity")),
        total_sell_quantity=_optional_int(data.get("sell_quantity")),
        open_interest=_optional_int(data.get("oi")),
        bids=_depth_levels(depth.get("buy")),
        asks=_depth_levels(depth.get("sell")),
        received_at=received,
    )


def _tick_to_domain(key: str, raw: dict[str, Any], received: dt.datetime) -> Tick:
    depth = raw.get("depth") or {}
    ts = _parse_exchange_time(raw.get("exchange_timestamp") or raw.get("last_trade_time"), received)
    return Tick(
        instrument_key=key,
        ts=ts,
        last_price=float(raw["last_price"]),
        last_quantity=_optional_int(raw.get("last_traded_quantity")),
        volume_traded_today=_optional_int(raw.get("volume_traded")),
        average_traded_price=_optional_float(raw.get("average_traded_price")),
        total_buy_quantity=_optional_int(raw.get("total_buy_quantity")),
        total_sell_quantity=_optional_int(raw.get("total_sell_quantity")),
        open_interest=_optional_int(raw.get("oi")),
        bids=_depth_levels(depth.get("buy")),
        asks=_depth_levels(depth.get("sell")),
        received_at=received,
    )


def _parse_exchange_time(value: Any, fallback: dt.datetime) -> dt.datetime:
    """Kite emits naive IST datetimes; localise rather than reject them.

    This is the one sanctioned place where a naive datetime is localised, because
    the vendor contract documents the timezone. Everywhere else naive datetimes
    are rejected outright.
    """
    if value is None:
        return fallback
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)
    return fallback


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _depth_levels(levels: Any) -> tuple[DepthLevel, ...]:
    if not levels:
        return ()
    return tuple(
        DepthLevel(
            price=float(level["price"]),
            quantity=int(level["quantity"]),
            orders=int(level.get("orders", 0)),
        )
        for level in levels
    )
