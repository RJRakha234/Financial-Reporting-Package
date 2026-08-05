"""Integrity layer: duplicates, ordering, alignment, gaps, OHLC, reconciliation."""

from __future__ import annotations

import pandas as pd
import pytest

from cse.data.integrity import (
    find_gaps,
    malformed_ohlc_mask,
    normalize,
    reconcile_candle,
)
from cse.data.schema import IntervalError, interval_to_ms
from tests.conftest import FIFTEEN_MIN_MS, make_candles

TOL = 1e-9


def test_clean_frame_passes_unchanged() -> None:
    frame = make_candles(50)
    clean, report = normalize(frame, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.ok
    assert report.rows_in == report.rows_out == 50
    assert report.gaps == []
    pd.testing.assert_frame_equal(clean, frame)


def test_duplicates_keep_last_occurrence() -> None:
    """A re-fetched bar supersedes the earlier copy: later wins."""
    frame = make_candles(10)
    superseding = frame.iloc[[5]].copy()
    superseding.loc[:, "close"] = 999.0
    superseding.loc[:, "high"] = 999.0
    duped = pd.concat([frame, superseding], ignore_index=True)

    clean, report = normalize(duped, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.duplicates_dropped == 1
    assert report.rows_out == 10
    assert clean.loc[clean["open_time"] == frame["open_time"].iloc[5], "close"].item() == 999.0


def test_out_of_order_rows_are_sorted() -> None:
    frame = make_candles(20)
    shuffled = frame.iloc[::-1].reset_index(drop=True)

    clean, report = normalize(shuffled, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.reordered is True
    assert clean["open_time"].is_monotonic_increasing
    pd.testing.assert_frame_equal(clean, frame)


def test_misaligned_bars_are_dropped() -> None:
    """A bar not on the interval grid would corrupt every gap calculation."""
    frame = make_candles(10)
    frame.loc[3, "open_time"] = int(frame["open_time"].iloc[3]) + 1_000

    clean, report = normalize(frame, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.misaligned_dropped == 1
    assert report.rows_out == 9
    assert ((clean["open_time"] % FIFTEEN_MIN_MS) == 0).all()


def test_gap_detection_reports_exact_missing_range() -> None:
    frame = make_candles(10)
    with_hole = frame.drop(index=[4, 5, 6]).reset_index(drop=True)

    clean, report = normalize(with_hole, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.missing_bars == 3
    assert gap.start_open_time == int(frame["open_time"].iloc[4])
    assert gap.end_open_time == int(frame["open_time"].iloc[6])
    assert report.missing_bars == 3
    # Gaps are a completeness problem, not a correctness one.
    assert report.ok
    assert len(clean) == 7


def test_multiple_gaps_are_reported_separately() -> None:
    frame = make_candles(20)
    with_holes = frame.drop(index=[3, 4, 10]).reset_index(drop=True)

    gaps = find_gaps(with_holes, FIFTEEN_MIN_MS)

    assert [g.missing_bars for g in gaps] == [2, 1]


@pytest.mark.parametrize(
    ("column", "value", "label"),
    [
        ("high", 1.0, "high below body"),
        ("low", 1e9, "low above body"),
        ("close", -5.0, "negative price"),
    ],
)
def test_malformed_ohlc_rows_are_dropped(column: str, value: float, label: str) -> None:
    frame = make_candles(10)
    frame.loc[4, column] = value

    clean, report = normalize(frame, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.malformed_ohlc == 1, label
    assert report.rows_out == 9
    assert len(clean) == 9
    assert not report.ok


def test_ohlc_tolerance_absorbs_float_noise() -> None:
    """Exchange decimal strings round-trip imperfectly; that must not be an error."""
    frame = make_candles(5)
    frame.loc[2, "high"] = float(frame["close"].iloc[2]) - 1e-12

    mask = malformed_ohlc_mask(frame, 1e-9)

    assert not mask.any()


def test_nan_rows_are_dropped() -> None:
    frame = make_candles(6)
    frame.loc[2, "volume"] = float("nan")

    clean, report = normalize(frame, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.nan_rows_dropped == 1
    assert len(clean) == 5


def test_empty_frame_is_handled() -> None:
    clean, report = normalize(make_candles(0), FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert clean.empty
    assert report.rows_in == 0
    assert report.gaps == []


def test_normalize_coerces_string_numbers() -> None:
    """REST delivers numbers as strings; downstream must still get float64."""
    frame = make_candles(5)
    as_strings = frame.astype(str)

    clean, report = normalize(as_strings, FIFTEEN_MIN_MS, ohlc_tolerance=TOL)

    assert report.ok
    assert clean["close"].dtype == "float64"
    assert clean["open_time"].dtype == "int64"


# ---- reconciliation ---------------------------------------------------------


def _candle(open_time: int, **overrides: float) -> dict[str, float | int]:
    base: dict[str, float | int] = {
        "open_time": open_time,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }
    base.update(overrides)
    return base


def test_reconcile_matching_candles() -> None:
    result = reconcile_candle(
        _candle(1000), _candle(1000), price_tolerance=1e-6, volume_tolerance=1e-4
    )

    assert result.matched
    assert result.reason == ""


def test_reconcile_flags_price_mismatch() -> None:
    """A dropped WebSocket frame shows up as a wrong high on the live bar."""
    live = _candle(1000, high=101.0)
    rest = _candle(1000, high=105.0)

    result = reconcile_candle(live, rest, price_tolerance=1e-6, volume_tolerance=1e-4)

    assert not result.matched
    assert result.reason == "price_mismatch:high"
    assert result.price_deltas["high"] == pytest.approx(4.0 / 105.0)


def test_reconcile_flags_volume_mismatch() -> None:
    result = reconcile_candle(
        _candle(1000, volume=10.0),
        _candle(1000, volume=12.0),
        price_tolerance=1e-6,
        volume_tolerance=1e-4,
    )

    assert not result.matched
    assert result.reason == "volume_mismatch"


def test_reconcile_rejects_mismatched_open_times() -> None:
    result = reconcile_candle(
        _candle(1000), _candle(2000), price_tolerance=1e-6, volume_tolerance=1e-4
    )

    assert not result.matched
    assert result.reason == "open_time_mismatch"


# ---- interval arithmetic ----------------------------------------------------


@pytest.mark.parametrize(
    ("interval", "expected_ms"),
    [("1m", 60_000), ("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000)],
)
def test_interval_to_ms(interval: str, expected_ms: int) -> None:
    assert interval_to_ms(interval) == expected_ms


def test_calendar_month_interval_is_rejected() -> None:
    """'1M' has no fixed width; accepting it would silently break gap detection."""
    with pytest.raises(IntervalError):
        interval_to_ms("1M")


@pytest.mark.parametrize("bad", ["", "m", "0m", "15x", "-5m"])
def test_bad_intervals_rejected(bad: str) -> None:
    with pytest.raises(IntervalError):
        interval_to_ms(bad)
