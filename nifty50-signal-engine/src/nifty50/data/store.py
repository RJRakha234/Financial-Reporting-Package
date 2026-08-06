"""Parquet bar store, partitioned ``exchange/symbol/timeframe/year-month``.

Storage layout is a deliberate departure from the spec's "partition by date".
Fifty symbols x five timeframes x seven years of *daily* partitions is roughly
440,000 files averaging a few kilobytes each — a shape that makes every scan
metadata-bound, blows out inode budgets and turns a full-history read into tens
of thousands of open() calls. Month partitions give ~21,000 files of a few
hundred KB, which is where parquet's row-group statistics actually start
earning their keep, and the partition key is still coarse enough that a
single-day repair rewrites one small file.

The store holds **raw, unadjusted** bars. Corporate-action factors are applied
on read by :mod:`nifty50.corporate_actions`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pandas as pd

from nifty50.config import Config
from nifty50.domain import BAR_INDEX_NAME, IST, OHLCV_COLUMNS, Exchange, Timeframe, ensure_ist
from nifty50.frames import bar_index, empty_bars
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

_PART_FILENAME: Final[str] = "bars.parquet"
_SAFE_SEGMENT: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._+-]")
_OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("oi",)


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the store holds for one (symbol, timeframe)."""

    first_ts: dt.datetime | None
    last_ts: dt.datetime | None
    bar_count: int

    @property
    def is_empty(self) -> bool:
        return self.bar_count == 0


class BarStore:
    """Append-and-upsert parquet store for OHLCV bars."""

    def __init__(self, root: Path, *, compression: str = "zstd") -> None:
        self._root = root
        self._compression = compression
        self._root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: Config) -> BarStore:
        return cls(
            config.path(config.data.store.root),
            compression=config.data.store.compression,
        )

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------- layout

    def partition_dir(self, exchange: Exchange, symbol: str, timeframe: Timeframe) -> Path:
        return (
            self._root
            / f"exchange={safe_symbol(exchange.value)}"
            / f"symbol={safe_symbol(symbol)}"
            / f"timeframe={timeframe.value}"
        )

    def partition_path(
        self, exchange: Exchange, symbol: str, timeframe: Timeframe, period: str
    ) -> Path:
        return self.partition_dir(exchange, symbol, timeframe) / f"ym={period}" / _PART_FILENAME

    # -------------------------------------------------------------- write

    def write(
        self,
        exchange: Exchange,
        symbol: str,
        timeframe: Timeframe,
        frame: pd.DataFrame,
    ) -> int:
        """Upsert ``frame`` into the store. Returns the number of bars written.

        Later data wins on a timestamp collision: a REST candle fetched after the
        close is authoritative over the one the engine assembled from ticks. The
        mismatch is reported separately by
        :func:`nifty50.data.integrity.reconcile_candles` — overwriting silently
        would hide exactly the discrepancy we want to see.
        """
        if frame.empty:
            return 0
        prepared = _normalise(frame)
        written = 0
        for period, chunk in prepared.groupby(bar_index(prepared).strftime("%Y%m")):
            path = self.partition_path(exchange, symbol, timeframe, str(period))
            merged = chunk
            if path.exists():
                existing = pd.read_parquet(path)
                existing = _restore_index(existing)
                merged = pd.concat([existing, chunk])
                merged = merged[~merged.index.duplicated(keep="last")]
            merged = merged.sort_index()
            _atomic_write_parquet(merged, path, self._compression)
            written += len(chunk)
        log.debug(
            "store.write",
            exchange=exchange.value,
            symbol=symbol,
            timeframe=timeframe.value,
            bars=written,
        )
        return written

    # --------------------------------------------------------------- read

    def read(
        self,
        exchange: Exchange,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> pd.DataFrame:
        """Raw bars for ``[start, end]``, sorted and de-duplicated."""
        directory = self.partition_dir(exchange, symbol, timeframe)
        if not directory.exists():
            return _empty_frame()
        parts = sorted(directory.glob(f"ym=*/{_PART_FILENAME}"))
        if start is not None or end is not None:
            parts = [p for p in parts if _period_overlaps(p, start, end)]
        if not parts:
            return _empty_frame()
        frames = [_restore_index(pd.read_parquet(path)) for path in parts]
        combined = pd.concat(frames).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        if start is not None:
            combined = combined[combined.index >= ensure_ist(start)]
        if end is not None:
            combined = combined[combined.index <= ensure_ist(end)]
        return combined

    def coverage(self, exchange: Exchange, symbol: str, timeframe: Timeframe) -> Coverage:
        """First/last stored bar. This is what makes restart-and-resume cheap."""
        directory = self.partition_dir(exchange, symbol, timeframe)
        if not directory.exists():
            return Coverage(first_ts=None, last_ts=None, bar_count=0)
        parts = sorted(directory.glob(f"ym=*/{_PART_FILENAME}"))
        if not parts:
            return Coverage(first_ts=None, last_ts=None, bar_count=0)
        first_frame = _restore_index(pd.read_parquet(parts[0]))
        last_frame = _restore_index(pd.read_parquet(parts[-1]))
        total = sum(len(_restore_index(pd.read_parquet(path))) for path in parts)
        if first_frame.empty or last_frame.empty:
            return Coverage(first_ts=None, last_ts=None, bar_count=total)
        return Coverage(
            first_ts=first_frame.index.min().to_pydatetime(),
            last_ts=last_frame.index.max().to_pydatetime(),
            bar_count=total,
        )

    def last_bar_ts(
        self, exchange: Exchange, symbol: str, timeframe: Timeframe
    ) -> dt.datetime | None:
        return self.coverage(exchange, symbol, timeframe).last_ts

    def symbols(self, exchange: Exchange) -> list[str]:
        base = self._root / f"exchange={safe_symbol(exchange.value)}"
        if not base.exists():
            return []
        return sorted(p.name.split("=", 1)[1] for p in base.glob("symbol=*") if p.is_dir())

    def timeframes(self, exchange: Exchange, symbol: str) -> list[Timeframe]:
        base = (
            self._root / f"exchange={safe_symbol(exchange.value)}" / f"symbol={safe_symbol(symbol)}"
        )
        if not base.exists():
            return []
        found = []
        for path in base.glob("timeframe=*"):
            value = path.name.split("=", 1)[1]
            try:
                found.append(Timeframe(value))
            except ValueError:  # pragma: no cover - stray directory
                continue
        return sorted(found, key=lambda tf: tf.value)


def safe_symbol(value: str) -> str:
    """Make a symbol safe for a path segment.

    NSE symbols contain ``&`` (M&M) and ``-`` (``-BE`` series suffixes); a raw
    join would produce shell-hostile paths and, on some filesystems, silently
    different directories for the same symbol.
    """
    return _SAFE_SEGMENT.sub("_", value)


def _empty_frame() -> pd.DataFrame:
    return empty_bars(OHLCV_COLUMNS)


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce an incoming frame to the storage contract."""
    index = bar_index(frame)
    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"bar frame is missing columns {missing}")
    keep = [*OHLCV_COLUMNS, *(c for c in _OPTIONAL_COLUMNS if c in frame.columns)]
    out = frame[keep].copy()
    out.index = index.tz_convert(IST)
    out.index.name = BAR_INDEX_NAME
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    for column in ("open", "high", "low", "close"):
        out[column] = out[column].astype("float64")
    out["volume"] = out["volume"].astype("int64")
    if "oi" in out.columns:
        out["oi"] = out["oi"].astype("int64")
    return out


def _restore_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the tz-aware index after a parquet round trip."""
    if BAR_INDEX_NAME in frame.columns:
        frame = frame.set_index(BAR_INDEX_NAME)
    restored = pd.DatetimeIndex(frame.index)
    frame.index = restored.tz_localize(IST) if restored.tz is None else restored.tz_convert(IST)
    frame.index.name = BAR_INDEX_NAME
    return frame


def _atomic_write_parquet(frame: pd.DataFrame, path: Path, compression: str) -> None:
    """Write via a temp file and rename, so a crash cannot leave a torn partition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(handle)
    tmp_path = Path(tmp_name)
    try:
        # `compression` is config-driven text; pandas types it as a Literal.
        frame.to_parquet(tmp_path, compression=compression, index=True)  # type: ignore[call-overload]
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _period_overlaps(path: Path, start: dt.datetime | None, end: dt.datetime | None) -> bool:
    """Cheap partition pruning from the ``ym=YYYYMM`` directory name."""
    period = path.parent.name.split("=", 1)[-1]
    if len(period) != 6 or not period.isdigit():
        return True
    year, month = int(period[:4]), int(period[4:])
    if start is not None and (year, month) < (start.year, start.month):
        return False
    return not (end is not None and (year, month) > (end.year, end.month))
