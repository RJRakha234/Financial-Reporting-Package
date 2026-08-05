"""Candle integrity: duplicates, ordering, alignment, gaps, OHLC sanity, and
live-vs-REST reconciliation.

Everything downstream assumes a strictly increasing, evenly spaced, gap-free
candle series. That assumption is wrong often enough in practice — exchange
halts, dropped WebSocket frames, a REST page boundary landing mid-bar — that it
has to be checked rather than hoped for.

Nothing here silently discards data without recording it: every repair returns
an :class:`IntegrityReport` that the caller logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cse.data.schema import CANDLE_COLUMNS, coerce_dtypes, empty_candles


@dataclass(frozen=True)
class Gap:
    """A run of missing bars in ``[start_open_time, end_open_time]`` inclusive."""

    start_open_time: int
    end_open_time: int
    missing_bars: int

    def as_dict(self) -> dict[str, int]:
        return {
            "start_open_time": self.start_open_time,
            "end_open_time": self.end_open_time,
            "missing_bars": self.missing_bars,
        }


@dataclass
class IntegrityReport:
    """What ``normalize`` changed, and what remains wrong.

    ``ok`` is deliberately narrow: it means the frame is safe to compute
    features on (monotonic, aligned, no duplicates, no malformed OHLC). Gaps do
    not clear ``ok`` by themselves because a gap is a data-completeness problem
    for the backfiller to repair, not a correctness problem in the rows present.
    """

    rows_in: int = 0
    rows_out: int = 0
    duplicates_dropped: int = 0
    reordered: bool = False
    misaligned_dropped: int = 0
    malformed_ohlc: int = 0
    nan_rows_dropped: int = 0
    gaps: list[Gap] = field(default_factory=list)

    @property
    def missing_bars(self) -> int:
        return sum(g.missing_bars for g in self.gaps)

    @property
    def ok(self) -> bool:
        return (
            self.duplicates_dropped == 0
            and not self.reordered
            and self.misaligned_dropped == 0
            and self.malformed_ohlc == 0
            and self.nan_rows_dropped == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "duplicates_dropped": self.duplicates_dropped,
            "reordered": self.reordered,
            "misaligned_dropped": self.misaligned_dropped,
            "malformed_ohlc": self.malformed_ohlc,
            "nan_rows_dropped": self.nan_rows_dropped,
            "gap_count": len(self.gaps),
            "missing_bars": self.missing_bars,
        }


def malformed_ohlc_mask(frame: pd.DataFrame, tolerance: float) -> pd.Series[bool]:
    """Rows violating the OHLC invariants.

    A well-formed bar satisfies ``low <= min(open, close)``,
    ``high >= max(open, close)`` and ``high >= low``. The tolerance absorbs
    float round-trip noise from the exchange's decimal string encoding.
    """
    if frame.empty:
        return pd.Series(dtype=bool, index=frame.index)
    body_high = frame[["open", "close"]].max(axis=1)
    body_low = frame[["open", "close"]].min(axis=1)
    bad: pd.Series[bool] = (
        (frame["high"] < body_high - tolerance)
        | (frame["low"] > body_low + tolerance)
        | (frame["high"] < frame["low"] - tolerance)
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    return bad


def find_gaps(frame: pd.DataFrame, interval_ms: int) -> list[Gap]:
    """Missing-bar runs between consecutive rows.

    Assumes the frame is already sorted and deduplicated (i.e. post-``normalize``).
    """
    if len(frame) < 2:
        return []
    open_times = frame["open_time"].to_numpy(dtype=np.int64)
    deltas = np.diff(open_times)
    gap_positions = np.flatnonzero(deltas > interval_ms)
    gaps: list[Gap] = []
    for position in gap_positions:
        first_missing = int(open_times[position]) + interval_ms
        last_missing = int(open_times[position + 1]) - interval_ms
        missing = int((last_missing - first_missing) // interval_ms) + 1
        gaps.append(
            Gap(start_open_time=first_missing, end_open_time=last_missing, missing_bars=missing)
        )
    return gaps


def normalize(
    frame: pd.DataFrame,
    interval_ms: int,
    *,
    ohlc_tolerance: float,
    drop_malformed: bool = True,
) -> tuple[pd.DataFrame, IntegrityReport]:
    """Return a canonical, trustworthy candle frame plus a report of the repairs.

    Order of operations matters:

    1. cast dtypes (REST sends numbers as strings),
    2. drop rows with NaN in any required field,
    3. drop bars whose ``open_time`` is not aligned to the interval grid — these
       are almost always a caller mixing timeframes, and keeping them would
       corrupt every gap calculation downstream,
    4. drop duplicate ``open_time`` keeping the LAST occurrence, because when a
       bar arrives twice the later copy is the settled one (a live bar is
       superseded by its REST version),
    5. sort by ``open_time``,
    6. drop malformed OHLC rows,
    7. recompute gaps on what survived.
    """
    report = IntegrityReport(rows_in=len(frame))
    if frame.empty:
        return empty_candles(), report

    working = coerce_dtypes(frame)

    before = len(working)
    working = working.dropna(subset=list(CANDLE_COLUMNS))
    report.nan_rows_dropped = before - len(working)

    aligned_mask = (working["open_time"] % interval_ms) == 0
    report.misaligned_dropped = int((~aligned_mask).sum())
    working = working.loc[aligned_mask]

    duplicate_mask = working["open_time"].duplicated(keep="last")
    report.duplicates_dropped = int(duplicate_mask.sum())
    working = working.loc[~duplicate_mask]

    report.reordered = not working["open_time"].is_monotonic_increasing
    if report.reordered:
        working = working.sort_values("open_time", kind="mergesort")

    malformed = malformed_ohlc_mask(working, ohlc_tolerance)
    report.malformed_ohlc = int(malformed.sum())
    if drop_malformed and report.malformed_ohlc:
        working = working.loc[~malformed]

    working = working.reset_index(drop=True)
    report.rows_out = len(working)
    report.gaps = find_gaps(working, interval_ms)
    return working, report


@dataclass(frozen=True)
class ReconciliationResult:
    """Outcome of comparing a WebSocket-closed bar against its REST counterpart."""

    open_time: int
    matched: bool
    price_deltas: dict[str, float]
    volume_delta: float
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_time": self.open_time,
            "matched": self.matched,
            "price_deltas": self.price_deltas,
            "volume_delta": self.volume_delta,
            "reason": self.reason,
        }


def _relative_delta(live: float, rest: float) -> float:
    """Relative difference, falling back to absolute when the reference is zero."""
    if rest == 0.0:
        return abs(live - rest)
    return abs(live - rest) / abs(rest)


def reconcile_candle(
    live: dict[str, Any],
    rest: dict[str, Any],
    *,
    price_tolerance: float,
    volume_tolerance: float,
) -> ReconciliationResult:
    """Compare a closed live bar with the REST bar for the same ``open_time``.

    A mismatch does not stop the engine — the REST bar is authoritative and gets
    stored — but it is logged, because a persistent mismatch means dropped
    WebSocket frames and therefore untrustworthy intrabar features.
    """
    if int(live["open_time"]) != int(rest["open_time"]):
        return ReconciliationResult(
            open_time=int(live["open_time"]),
            matched=False,
            price_deltas={},
            volume_delta=float("nan"),
            reason="open_time_mismatch",
        )

    price_deltas = {
        field_name: _relative_delta(float(live[field_name]), float(rest[field_name]))
        for field_name in ("open", "high", "low", "close")
    }
    volume_delta = _relative_delta(float(live["volume"]), float(rest["volume"]))

    price_ok = all(delta <= price_tolerance for delta in price_deltas.values())
    volume_ok = volume_delta <= volume_tolerance
    reason = ""
    if not price_ok:
        worst = max(price_deltas, key=lambda k: price_deltas[k])
        reason = f"price_mismatch:{worst}"
    elif not volume_ok:
        reason = "volume_mismatch"

    return ReconciliationResult(
        open_time=int(live["open_time"]),
        matched=price_ok and volume_ok,
        price_deltas=price_deltas,
        volume_delta=volume_delta,
        reason=reason,
    )
