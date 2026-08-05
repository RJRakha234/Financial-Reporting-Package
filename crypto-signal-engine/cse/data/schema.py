"""Canonical candle schema and interval arithmetic.

Every candle in this system — whether it came from REST, WebSocket, the local
store, or the synthetic generator — is normalised into the frame defined here.
Downstream layers may assume these columns, these dtypes, and this ordering.
"""

from __future__ import annotations

import re
from typing import Any, Final

import pandas as pd

# Binance kline array positions (GET /api/v3/klines and the "k" object of the
# kline WebSocket payload carry the same fields under different names).
CANDLE_COLUMNS: Final[tuple[str, ...]] = (
    "open_time",  # int64, epoch ms, INCLUSIVE start of the bar
    "open",
    "high",
    "low",
    "close",
    "volume",  # base asset volume
    "close_time",  # int64, epoch ms, inclusive end (open_time + interval - 1)
    "quote_volume",  # quote asset volume
    "trades",  # int64, number of trades
    "taker_buy_base",  # base volume bought by the aggressing (taker) side
    "taker_buy_quote",
)

FLOAT_COLUMNS: Final[tuple[str, ...]] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base",
    "taker_buy_quote",
)

INT_COLUMNS: Final[tuple[str, ...]] = ("open_time", "close_time", "trades")

CANDLE_DTYPES: Final[dict[str, str]] = {
    **dict.fromkeys(FLOAT_COLUMNS, "float64"),
    **dict.fromkeys(INT_COLUMNS, "int64"),
}

_INTERVAL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)([smhdwM])$")

# Binance interval suffix -> milliseconds. 'M' (month) is intentionally absent:
# months are not a fixed duration, and every gap/alignment routine in this
# package assumes a constant bar width. Reject it loudly rather than guess.
_UNIT_MS: Final[dict[str, int]] = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}


class IntervalError(ValueError):
    """Raised for an interval string this package cannot treat as fixed-width."""


def interval_to_ms(interval: str) -> int:
    """Convert a Binance interval string ('15m', '4h') to milliseconds.

    Raises IntervalError for calendar-relative intervals ('1M'), which have no
    constant width and would silently corrupt gap detection.
    """
    match = _INTERVAL_RE.match(interval)
    if match is None:
        raise IntervalError(f"unparseable interval: {interval!r}")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise IntervalError(f"interval must be positive: {interval!r}")
    if unit not in _UNIT_MS:
        raise IntervalError(
            f"interval {interval!r} is not fixed-width (calendar months are not supported)"
        )
    return amount * _UNIT_MS[unit]


def floor_to_interval(timestamp_ms: int, interval_ms: int) -> int:
    """Snap an epoch-ms timestamp down to the open_time of its bar.

    Binance bars are aligned to the Unix epoch, so this is plain floor division
    for every interval we support (all of which divide evenly into a day).
    """
    return (timestamp_ms // interval_ms) * interval_ms


def empty_candles() -> pd.DataFrame:
    """An empty frame with the canonical columns and dtypes."""
    frame = pd.DataFrame({c: pd.Series(dtype=CANDLE_DTYPES[c]) for c in CANDLE_COLUMNS})
    return frame


def candles_from_rest(rows: list[list[object]]) -> pd.DataFrame:
    """Build a canonical frame from the raw /api/v3/klines array response.

    Binance returns numbers as JSON strings; positions 0-10 map to the canonical
    columns in order, and position 11 is a documented "ignore" field.
    """
    if not rows:
        return empty_candles()
    frame = pd.DataFrame(
        [row[: len(CANDLE_COLUMNS)] for row in rows],
        columns=list(CANDLE_COLUMNS),
    )
    return coerce_dtypes(frame)


def candle_from_ws(kline: dict[str, Any]) -> dict[str, Any]:
    """Map the ``k`` object of a kline WebSocket payload to canonical fields.

    Field names are Binance's documented single-letter keys. The caller is
    responsible for checking ``kline["x"]`` (is-closed) before treating the
    result as a final bar.
    """
    return {
        "open_time": int(kline["t"]),
        "open": float(kline["o"]),
        "high": float(kline["h"]),
        "low": float(kline["l"]),
        "close": float(kline["c"]),
        "volume": float(kline["v"]),
        "close_time": int(kline["T"]),
        "quote_volume": float(kline["q"]),
        "trades": int(kline["n"]),
        "taker_buy_base": float(kline["V"]),
        "taker_buy_quote": float(kline["Q"]),
    }


def coerce_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast a candle frame to the canonical dtypes, preserving column order.

    Missing columns are an error rather than a silent NaN fill: a frame without
    ``taker_buy_base`` would quietly disable the order-flow features.
    """
    missing = [c for c in CANDLE_COLUMNS if c not in frame.columns]
    if missing:
        raise KeyError(f"candle frame missing required columns: {missing}")
    out = frame.loc[:, list(CANDLE_COLUMNS)].copy()
    for column in FLOAT_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    for column in INT_COLUMNS:
        # Ints arrive as JSON strings from REST; go via float to tolerate "123.0".
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("int64")
    return out


def open_time_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """UTC DatetimeIndex built from ``open_time``, for charting and resampling."""
    return pd.DatetimeIndex(pd.to_datetime(frame["open_time"], unit="ms", utc=True))


def date_partition(open_time_ms: int) -> str:
    """UTC date string used as the storage partition key for one bar."""
    return str(pd.Timestamp(int(open_time_ms), unit="ms", tz="UTC").strftime("%Y-%m-%d"))


def date_partition_series(open_time_ms: pd.Series[int]) -> pd.Series[str]:
    """Vectorised :func:`date_partition` for a whole ``open_time`` column."""
    return pd.to_datetime(open_time_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
