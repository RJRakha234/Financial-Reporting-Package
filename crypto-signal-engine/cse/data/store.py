"""Local candle storage: Hive-partitioned Parquet, readable directly by DuckDB.

Layout::

    <root>/candles/symbol=BTCUSDT/timeframe=15m/date=2024-01-01/candles.parquet

Partitioning by UTC date keeps individual files small enough to rewrite cheaply
(a 15m day is 96 rows) while keeping the directory count manageable over a
three-year backfill. The layout is standard Hive partitioning, so a query like::

    SELECT * FROM read_parquet('data_store/candles/**/*.parquet', hive_partitioning=1)
    WHERE symbol='BTCUSDT' AND timeframe='15m'

works from DuckDB with no bespoke reader.

Writes are atomic per partition (write to a temp file, then ``Path.replace``),
so an interrupted backfill leaves either the old partition or the new one, never
a half-written Parquet file.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from cse.config import StorageConfig
from cse.data.integrity import IntegrityReport, normalize
from cse.data.schema import (
    CANDLE_COLUMNS,
    coerce_dtypes,
    date_partition_series,
    empty_candles,
)
from cse.logging import get_logger

_log = get_logger(__name__)

CANDLES_DIRNAME = "candles"


class CandleStore:
    """Read/write access to the local candle archive."""

    def __init__(self, config: StorageConfig, *, ohlc_tolerance: float = 1e-9) -> None:
        self._config = config
        self._root = Path(config.root).expanduser()
        self._ohlc_tolerance = ohlc_tolerance
        self._candles_root = self._root / CANDLES_DIRNAME

    @property
    def root(self) -> Path:
        return self._root

    def partition_dir(self, symbol: str, timeframe: str, date: str) -> Path:
        return self._candles_root / f"symbol={symbol}" / f"timeframe={timeframe}" / f"date={date}"

    def series_dir(self, symbol: str, timeframe: str) -> Path:
        return self._candles_root / f"symbol={symbol}" / f"timeframe={timeframe}"

    # ---- writing ------------------------------------------------------------

    def write(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> int:
        """Upsert candles, one partition per UTC date. Returns rows written.

        Existing rows in a touched partition are merged with the incoming rows
        and deduplicated on ``open_time`` keeping the *new* row, because a
        re-fetched bar is by definition the more settled one.
        """
        if frame.empty:
            return 0
        incoming = coerce_dtypes(frame)
        incoming = incoming.assign(_date=date_partition_series(incoming["open_time"]))

        written = 0
        for date_value, group in incoming.groupby("_date", sort=True):
            date_str = str(date_value)
            partition = self.partition_dir(symbol, timeframe, date_str)
            new_rows = group.drop(columns=["_date"])

            existing = self._read_partition(partition)
            if not existing.empty:
                merged = pd.concat([existing, new_rows], ignore_index=True)
                merged = merged.drop_duplicates(subset=["open_time"], keep="last")
            else:
                merged = new_rows
            merged = merged.sort_values("open_time", kind="mergesort").reset_index(drop=True)

            self._write_partition_atomic(partition, merged)
            written += len(new_rows)

        _log.info(
            "store.write",
            symbol=symbol,
            timeframe=timeframe,
            rows=written,
            partitions=int(incoming["_date"].nunique()),
        )
        return written

    def _write_partition_atomic(self, partition: Path, frame: pd.DataFrame) -> None:
        partition.mkdir(parents=True, exist_ok=True)
        target = partition / "candles.parquet"
        temp = partition / "candles.parquet.tmp"
        table = pa.Table.from_pandas(frame.loc[:, list(CANDLE_COLUMNS)], preserve_index=False)
        pq.write_table(table, temp, compression=self._config.compression)
        # Path.replace is an atomic rename on the same filesystem, so a crash
        # leaves either the old partition or the new one, never a torn file.
        temp.replace(target)

    # ---- reading ------------------------------------------------------------

    def _read_partition(self, partition: Path) -> pd.DataFrame:
        target = partition / "candles.parquet"
        if not target.is_file():
            return empty_candles()
        return coerce_dtypes(pd.read_parquet(target))

    def read(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        """Read a candle range, sorted and deduplicated. Bounds are inclusive."""
        series = self.series_dir(symbol, timeframe)
        if not series.is_dir():
            return empty_candles()

        files = sorted(series.glob("date=*/candles.parquet"))
        if not files:
            return empty_candles()

        # Prune whole partitions by their date key before touching the disk.
        keep = [f for f in files if self._partition_in_range(f, start_time, end_time)]
        if not keep:
            return empty_candles()

        frames = [pd.read_parquet(f) for f in keep]
        combined = coerce_dtypes(pd.concat(frames, ignore_index=True))
        if start_time is not None:
            combined = combined.loc[combined["open_time"] >= int(start_time)]
        if end_time is not None:
            combined = combined.loc[combined["open_time"] <= int(end_time)]
        combined = (
            combined.drop_duplicates(subset=["open_time"], keep="last")
            .sort_values("open_time", kind="mergesort")
            .reset_index(drop=True)
        )
        return combined

    def _partition_in_range(
        self, file_path: Path, start_time: int | None, end_time: int | None
    ) -> bool:
        date_str = file_path.parent.name.removeprefix("date=")
        try:
            day_start = pd.Timestamp(date_str, tz="UTC")
        except ValueError:
            _log.warning("store.bad_partition_name", path=str(file_path))
            return True  # read it rather than silently skip real data
        day_start_ms = int(day_start.value // 1_000_000)
        day_end_ms = day_start_ms + 86_400_000 - 1
        if start_time is not None and day_end_ms < int(start_time):
            return False
        return not (end_time is not None and day_start_ms > int(end_time))

    # ---- resume support -----------------------------------------------------

    def last_open_time(self, symbol: str, timeframe: str) -> int | None:
        """``open_time`` of the newest stored bar, or None if nothing is stored.

        Reads only the most recent partition file, so resume stays O(1) in the
        size of the archive rather than O(history).
        """
        series = self.series_dir(symbol, timeframe)
        if not series.is_dir():
            return None
        partitions = sorted(p for p in series.glob("date=*") if (p / "candles.parquet").is_file())
        if not partitions:
            return None
        newest = self._read_partition(partitions[-1])
        if newest.empty:
            return None
        return int(newest["open_time"].max())

    def first_open_time(self, symbol: str, timeframe: str) -> int | None:
        series = self.series_dir(symbol, timeframe)
        if not series.is_dir():
            return None
        partitions = sorted(p for p in series.glob("date=*") if (p / "candles.parquet").is_file())
        if not partitions:
            return None
        oldest = self._read_partition(partitions[0])
        if oldest.empty:
            return None
        return int(oldest["open_time"].min())

    def row_count(self, symbol: str, timeframe: str) -> int:
        series = self.series_dir(symbol, timeframe)
        if not series.is_dir():
            return 0
        total = 0
        for file_path in series.glob("date=*/candles.parquet"):
            total += pq.ParquetFile(file_path).metadata.num_rows
        return total

    # ---- maintenance --------------------------------------------------------

    def verify(
        self,
        symbol: str,
        timeframe: str,
        interval_ms: int,
        *,
        start_time: int | None = None,
    ) -> IntegrityReport:
        """Integrity-check a stored series.

        Defaults to the whole series. Pass ``start_time`` to check only a
        suffix, which is what the resume path wants: re-reading a three-year 1m
        archive (1,095 Parquet files per series) on every startup makes restart
        cost scale with history, even though the only bars that can have changed
        are the ones just written.

        Callers checking a suffix should start one bar *before* the first new
        bar, so a gap at the seam is still detected.
        """
        frame = self.read(symbol, timeframe, start_time=start_time)
        _, report = normalize(frame, interval_ms, ohlc_tolerance=self._ohlc_tolerance)
        _log.info(
            "store.verify",
            symbol=symbol,
            timeframe=timeframe,
            scope="suffix" if start_time is not None else "full",
            **report.as_dict(),
        )
        return report

    def rewrite_clean(self, symbol: str, timeframe: str, interval_ms: int) -> IntegrityReport:
        """Normalise everything stored for a series and write it back.

        Used after an interrupted or overlapping backfill to collapse duplicates
        and drop any malformed bars that made it to disk.
        """
        frame = self.read(symbol, timeframe)
        clean, report = normalize(frame, interval_ms, ohlc_tolerance=self._ohlc_tolerance)
        if not report.ok:
            series = self.series_dir(symbol, timeframe)
            if series.is_dir():
                shutil.rmtree(series)
            self.write(symbol, timeframe, clean)
            _log.warning(
                "store.rewrite_clean", symbol=symbol, timeframe=timeframe, **report.as_dict()
            )
        return report

    def stats(self, symbol: str, timeframe: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "rows": self.row_count(symbol, timeframe),
            "first_open_time": self.first_open_time(symbol, timeframe),
            "last_open_time": self.last_open_time(symbol, timeframe),
        }
