"""Parquet store: partition layout, upsert semantics, range reads, resume."""

from __future__ import annotations

import pandas as pd

from cse.data.store import CandleStore
from tests.conftest import FIFTEEN_MIN_MS, make_candles

SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"
# 2023-11-14T22:13:20Z floored to 15m; spans several UTC days at 96 bars/day.
START_MS = 1_700_000_000_000 // FIFTEEN_MIN_MS * FIFTEEN_MIN_MS


def test_write_read_roundtrip(store: CandleStore) -> None:
    frame = make_candles(300, start_ms=START_MS)

    written = store.write(SYMBOL, TIMEFRAME, frame)
    read_back = store.read(SYMBOL, TIMEFRAME)

    assert written == 300
    pd.testing.assert_frame_equal(read_back, frame)


def test_partitions_are_hive_style_by_utc_date(store: CandleStore) -> None:
    """Layout must stay queryable by DuckDB's hive_partitioning."""
    frame = make_candles(300, start_ms=START_MS)
    store.write(SYMBOL, TIMEFRAME, frame)

    series = store.series_dir(SYMBOL, TIMEFRAME)
    partitions = sorted(p.name for p in series.glob("date=*"))

    assert series.parts[-2:] == (f"symbol={SYMBOL}", f"timeframe={TIMEFRAME}")
    # Starts 22:00 UTC, so 300 bars land as 8 + 96 + 96 + 96 + 4 across 5 days.
    assert len(partitions) == 5
    assert all(p.startswith("date=20") for p in partitions)
    assert all((series / p / "candles.parquet").is_file() for p in partitions)


def test_no_temp_files_survive_a_write(store: CandleStore) -> None:
    """Writes are atomic: a crash must never leave a half-written parquet."""
    store.write(SYMBOL, TIMEFRAME, make_candles(200, start_ms=START_MS))

    leftovers = list(store.root.rglob("*.tmp"))

    assert leftovers == []


def test_rewriting_a_bar_keeps_the_newer_copy(store: CandleStore) -> None:
    """A re-fetched bar is more settled than the one already on disk."""
    frame = make_candles(10, start_ms=START_MS)
    store.write(SYMBOL, TIMEFRAME, frame)

    corrected = frame.iloc[[3]].copy()
    corrected.loc[:, "close"] = 12345.0
    store.write(SYMBOL, TIMEFRAME, corrected)

    read_back = store.read(SYMBOL, TIMEFRAME)

    assert len(read_back) == 10
    assert (
        read_back.loc[read_back["open_time"] == frame["open_time"].iloc[3], "close"].item()
        == 12345.0
    )


def test_appending_later_bars_does_not_duplicate(store: CandleStore) -> None:
    first = make_candles(100, start_ms=START_MS)
    second = make_candles(100, start_ms=START_MS + 100 * FIFTEEN_MIN_MS, start_price=200.0)

    store.write(SYMBOL, TIMEFRAME, first)
    store.write(SYMBOL, TIMEFRAME, second)
    read_back = store.read(SYMBOL, TIMEFRAME)

    assert len(read_back) == 200
    assert read_back["open_time"].is_unique
    assert read_back["open_time"].is_monotonic_increasing


def test_range_read_bounds_are_inclusive(store: CandleStore) -> None:
    frame = make_candles(200, start_ms=START_MS)
    store.write(SYMBOL, TIMEFRAME, frame)

    lo = int(frame["open_time"].iloc[50])
    hi = int(frame["open_time"].iloc[59])
    subset = store.read(SYMBOL, TIMEFRAME, start_time=lo, end_time=hi)

    assert len(subset) == 10
    assert int(subset["open_time"].iloc[0]) == lo
    assert int(subset["open_time"].iloc[-1]) == hi


def test_range_read_across_partition_boundaries(store: CandleStore) -> None:
    """Partition pruning must not drop rows that straddle a UTC midnight."""
    frame = make_candles(300, start_ms=START_MS)
    store.write(SYMBOL, TIMEFRAME, frame)

    lo = int(frame["open_time"].iloc[90])
    hi = int(frame["open_time"].iloc[110])
    subset = store.read(SYMBOL, TIMEFRAME, start_time=lo, end_time=hi)

    assert len(subset) == 21
    assert subset["open_time"].is_monotonic_increasing


def test_resume_pointers_on_empty_store(store: CandleStore) -> None:
    assert store.last_open_time(SYMBOL, TIMEFRAME) is None
    assert store.first_open_time(SYMBOL, TIMEFRAME) is None
    assert store.row_count(SYMBOL, TIMEFRAME) == 0
    assert store.read(SYMBOL, TIMEFRAME).empty


def test_resume_pointers_after_write(store: CandleStore) -> None:
    frame = make_candles(300, start_ms=START_MS)
    store.write(SYMBOL, TIMEFRAME, frame)

    assert store.last_open_time(SYMBOL, TIMEFRAME) == int(frame["open_time"].iloc[-1])
    assert store.first_open_time(SYMBOL, TIMEFRAME) == int(frame["open_time"].iloc[0])
    assert store.row_count(SYMBOL, TIMEFRAME) == 300


def test_series_are_isolated_from_each_other(store: CandleStore) -> None:
    frame = make_candles(50, start_ms=START_MS)
    store.write("BTCUSDT", "15m", frame)
    store.write("ETHUSDT", "15m", frame)
    store.write("BTCUSDT", "1h", frame)

    assert store.row_count("BTCUSDT", "15m") == 50
    assert store.row_count("ETHUSDT", "15m") == 50
    assert store.row_count("BTCUSDT", "1h") == 50
    assert store.read("SOLUSDT", "15m").empty


def test_verify_reports_gaps(store: CandleStore) -> None:
    frame = make_candles(50, start_ms=START_MS).drop(index=[20, 21]).reset_index(drop=True)
    store.write(SYMBOL, TIMEFRAME, frame)

    report = store.verify(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS)

    assert len(report.gaps) == 1
    assert report.gaps[0].missing_bars == 2


def test_rewrite_clean_removes_malformed_rows(store: CandleStore) -> None:
    frame = make_candles(50, start_ms=START_MS)
    frame.loc[10, "high"] = 0.5  # below the body: impossible bar
    store.write(SYMBOL, TIMEFRAME, frame)

    report = store.rewrite_clean(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS)
    after = store.read(SYMBOL, TIMEFRAME)

    assert report.malformed_ohlc == 1
    assert len(after) == 49
    assert store.verify(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS).ok


def test_dtypes_survive_the_parquet_roundtrip(store: CandleStore) -> None:
    """float64/int64 must be stable, or feature maths changes subtly on reload."""
    store.write(SYMBOL, TIMEFRAME, make_candles(20, start_ms=START_MS))

    read_back = store.read(SYMBOL, TIMEFRAME)

    assert read_back["open_time"].dtype == "int64"
    assert read_back["trades"].dtype == "int64"
    assert read_back["close"].dtype == "float64"
    assert read_back["taker_buy_base"].dtype == "float64"


def test_writing_empty_frame_is_a_noop(store: CandleStore) -> None:
    assert store.write(SYMBOL, TIMEFRAME, make_candles(0)) == 0
    assert store.read(SYMBOL, TIMEFRAME).empty
