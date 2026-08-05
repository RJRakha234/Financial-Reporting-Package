"""Historical backfill: paginate ``/api/v3/klines``, resume, and repair gaps.

Two invariants this module exists to hold:

* **Never store an unclosed bar.** The upper bound of every request is the last
  fully closed bar at call time. A forming bar has a moving close, and one
  stored by accident would poison every feature computed from it — silently,
  and only for the most recent data, which is exactly where you would not
  notice it.
* **Never re-download what is already on disk.** Resume starts from the last
  stored ``open_time`` minus a small configured overlap, so a bar that was
  written while still forming (from an earlier crash) gets corrected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from cse.config import BackfillConfig, IntegrityConfig
from cse.data.integrity import Gap, find_gaps, normalize
from cse.data.rest import BinanceRestClient
from cse.data.schema import floor_to_interval
from cse.data.store import CandleStore
from cse.logging import get_logger

_log = get_logger(__name__)


@dataclass
class BackfillResult:
    """Outcome of backfilling one (symbol, timeframe) series."""

    symbol: str
    timeframe: str
    requests: int = 0
    rows_fetched: int = 0
    rows_written: int = 0
    resumed_from: int | None = None
    first_open_time: int | None = None
    last_open_time: int | None = None
    gaps_before_repair: list[Gap] = field(default_factory=list)
    gaps_after_repair: list[Gap] = field(default_factory=list)
    outages: list[Gap] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "requests": self.requests,
            "rows_fetched": self.rows_fetched,
            "rows_written": self.rows_written,
            "resumed_from": self.resumed_from,
            "first_open_time": self.first_open_time,
            "last_open_time": self.last_open_time,
            "gaps_before": len(self.gaps_before_repair),
            "gaps_after": len(self.gaps_after_repair),
            "outages": len(self.outages),
        }


def last_closed_open_time(now_ms: int, interval_ms: int) -> int:
    """``open_time`` of the most recent *closed* bar at ``now_ms``.

    The bar containing ``now_ms`` is still forming, so the last closed one
    starts a full interval earlier.
    """
    return floor_to_interval(now_ms, interval_ms) - interval_ms


class Backfiller:
    """Paginated historical loader with resume and gap repair."""

    def __init__(
        self,
        client: BinanceRestClient,
        store: CandleStore,
        backfill_config: BackfillConfig,
        integrity_config: IntegrityConfig,
    ) -> None:
        self._client = client
        self._store = store
        self._config = backfill_config
        self._integrity = integrity_config

    async def backfill(
        self,
        symbol: str,
        timeframe: str,
        interval_ms: int,
        *,
        history_days: int,
        now_ms: int | None = None,
    ) -> BackfillResult:
        """Fetch and store the configured history window for one series."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        result = BackfillResult(symbol=symbol, timeframe=timeframe)

        end_open_time = last_closed_open_time(now, interval_ms)
        window_start = floor_to_interval(now - history_days * 86_400_000, interval_ms)

        stored_last = self._store.last_open_time(symbol, timeframe)
        if stored_last is not None:
            # Step back a couple of bars: the newest stored bar may have been
            # written from a live stream before it settled.
            resume_at = stored_last - self._config.resume_overlap_bars * interval_ms
            cursor = max(window_start, resume_at)
            result.resumed_from = cursor
        else:
            cursor = window_start

        if cursor > end_open_time:
            _log.info(
                "backfill.up_to_date",
                symbol=symbol,
                timeframe=timeframe,
                last_open_time=stored_last,
            )
            result.first_open_time = self._store.first_open_time(symbol, timeframe)
            result.last_open_time = stored_last
            return result

        await self._paginate(symbol, timeframe, interval_ms, cursor, end_open_time, result)

        stored = self._store.read(symbol, timeframe)
        clean, report = normalize(
            stored, interval_ms, ohlc_tolerance=self._integrity.ohlc_tolerance
        )
        result.gaps_before_repair = list(report.gaps)
        if report.gaps:
            await self._repair_gaps(symbol, timeframe, interval_ms, report.gaps, result)
            clean, report = normalize(
                self._store.read(symbol, timeframe),
                interval_ms,
                ohlc_tolerance=self._integrity.ohlc_tolerance,
            )
        result.gaps_after_repair = list(report.gaps)
        result.outages = [
            g for g in report.gaps if g.missing_bars >= self._integrity.outage_gap_bars
        ]

        if not clean.empty:
            result.first_open_time = int(clean["open_time"].iloc[0])
            result.last_open_time = int(clean["open_time"].iloc[-1])

        _log.info("backfill.done", **result.as_dict())
        if result.outages:
            _log.warning(
                "backfill.unrecoverable_gaps",
                symbol=symbol,
                timeframe=timeframe,
                outages=[g.as_dict() for g in result.outages],
                note="ranges Binance did not serve; usually a listing boundary or exchange halt",
            )
        return result

    async def _paginate(
        self,
        symbol: str,
        timeframe: str,
        interval_ms: int,
        cursor: int,
        end_open_time: int,
        result: BackfillResult,
    ) -> None:
        limit = self._config.klines_limit
        while cursor <= end_open_time:
            page = await self._client.klines(
                symbol,
                timeframe,
                start_time=cursor,
                end_time=end_open_time,
                limit=limit,
            )
            result.requests += 1
            if page.empty:
                _log.info(
                    "backfill.empty_page",
                    symbol=symbol,
                    timeframe=timeframe,
                    cursor=cursor,
                    note="no data at or after cursor; series likely starts later",
                )
                break

            clean, report = normalize(
                page, interval_ms, ohlc_tolerance=self._integrity.ohlc_tolerance
            )
            if not report.ok:
                _log.warning(
                    "backfill.page_repaired",
                    symbol=symbol,
                    timeframe=timeframe,
                    **report.as_dict(),
                )
            if clean.empty:
                break

            # Defensive: never let a forming bar reach the store.
            clean = clean.loc[clean["open_time"] <= end_open_time]
            if clean.empty:
                break

            result.rows_fetched += len(clean)
            result.rows_written += self._store.write(symbol, timeframe, clean)

            newest = int(clean["open_time"].iloc[-1])
            next_cursor = newest + interval_ms
            if next_cursor <= cursor:
                # The server did not advance; bail rather than spin forever.
                _log.error(
                    "backfill.cursor_stalled",
                    symbol=symbol,
                    timeframe=timeframe,
                    cursor=cursor,
                    newest=newest,
                )
                break
            cursor = next_cursor

            if len(page) < limit:
                # Short page means we reached the end of available history.
                break

    async def _repair_gaps(
        self,
        symbol: str,
        timeframe: str,
        interval_ms: int,
        gaps: list[Gap],
        result: BackfillResult,
    ) -> None:
        budget = self._integrity.max_gap_repair_requests
        _log.warning(
            "backfill.repairing_gaps",
            symbol=symbol,
            timeframe=timeframe,
            gap_count=len(gaps),
            missing_bars=sum(g.missing_bars for g in gaps),
        )
        for gap in gaps:
            if budget <= 0:
                _log.error(
                    "backfill.repair_budget_exhausted",
                    symbol=symbol,
                    timeframe=timeframe,
                    note="raise data.integrity.max_gap_repair_requests to continue",
                )
                return
            cursor = gap.start_open_time
            while cursor <= gap.end_open_time and budget > 0:
                page = await self._client.klines(
                    symbol,
                    timeframe,
                    start_time=cursor,
                    end_time=gap.end_open_time,
                    limit=self._config.klines_limit,
                )
                result.requests += 1
                budget -= 1
                if page.empty:
                    # Binance has nothing here: a genuine exchange-side hole.
                    break
                clean, _ = normalize(
                    page, interval_ms, ohlc_tolerance=self._integrity.ohlc_tolerance
                )
                if clean.empty:
                    break
                result.rows_fetched += len(clean)
                result.rows_written += self._store.write(symbol, timeframe, clean)
                newest = int(clean["open_time"].iloc[-1])
                if newest + interval_ms <= cursor:
                    break
                cursor = newest + interval_ms


def summarize_coverage(
    frame: pd.DataFrame, interval_ms: int, *, expected_start: int, expected_end: int
) -> dict[str, Any]:
    """Coverage stats for a stored series, used by the integrity CLI report."""
    if frame.empty:
        return {
            "rows": 0,
            "expected_bars": max(0, (expected_end - expected_start) // interval_ms + 1),
            "coverage_pct": 0.0,
            "gap_count": 0,
            "missing_bars": 0,
        }
    expected_bars = max(0, (expected_end - expected_start) // interval_ms + 1)
    gaps = find_gaps(frame, interval_ms)
    return {
        "rows": len(frame),
        "expected_bars": expected_bars,
        "coverage_pct": round(100.0 * len(frame) / expected_bars, 4) if expected_bars else 0.0,
        "gap_count": len(gaps),
        "missing_bars": sum(g.missing_bars for g in gaps),
        "first_open_time": int(frame["open_time"].iloc[0]),
        "last_open_time": int(frame["open_time"].iloc[-1]),
    }
