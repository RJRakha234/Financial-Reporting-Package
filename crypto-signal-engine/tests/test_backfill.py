"""Backfill: pagination, resume, unclosed-bar exclusion, gap repair.

The mock implements Binance's actual klines semantics (inclusive bounds,
``limit`` rows per page, ordered by open_time) so the pagination cursor logic is
genuinely exercised rather than assumed.
"""

from __future__ import annotations

import random

import httpx
import pandas as pd
import pytest
import respx

from cse.config import Config
from cse.data.backfill import Backfiller, last_closed_open_time, summarize_coverage
from cse.data.integrity import find_gaps
from cse.data.ratelimit import WeightLimiter
from cse.data.rest import BinanceRestClient
from cse.data.store import CandleStore
from tests.conftest import FIFTEEN_MIN_MS, make_candles, to_rest_rows

BASE = "https://api.binance.com"
KLINES = f"{BASE}/api/v3/klines"
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"

# Aligned to 15m. 400 bars of history available on the "exchange".
EXCHANGE_START = 1_700_000_000_000 // FIFTEEN_MIN_MS * FIFTEEN_MIN_MS
EXCHANGE_BARS = 400
# "Now" sits mid-bar so the last bar is still forming, as in real life.
NOW_MS = EXCHANGE_START + EXCHANGE_BARS * FIFTEEN_MIN_MS + 3 * 60_000


class FakeBinance:
    """Serves klines from an in-memory series with Binance's paging semantics."""

    def __init__(self, frame: pd.DataFrame, *, limit_cap: int = 1000) -> None:
        self.frame = frame
        self.limit_cap = limit_cap
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        frame = self.frame
        start = int(params.get("startTime", 0))
        end = int(params.get("endTime", 2**63 - 1))
        limit = min(int(params.get("limit", 500)), self.limit_cap)
        subset = frame.loc[(frame["open_time"] >= start) & (frame["open_time"] <= end)].head(limit)
        return httpx.Response(200, json=to_rest_rows(subset))


@pytest.fixture
def exchange_frame() -> pd.DataFrame:
    return make_candles(EXCHANGE_BARS, start_ms=EXCHANGE_START, interval_ms=FIFTEEN_MIN_MS)


@pytest.fixture
def fast_config(config: Config) -> Config:
    rest = config.data.rest.model_copy(
        update={"backoff_initial_seconds": 0.001, "backoff_jitter": 0.0}
    )
    # Small pages so the pagination loop runs many times in the tests.
    backfill = config.data.backfill.model_copy(update={"klines_limit": 100})
    return config.model_copy(
        update={"data": config.data.model_copy(update={"rest": rest, "backfill": backfill})}
    )


def build_backfiller(config: Config, store: CandleStore) -> tuple[Backfiller, BinanceRestClient]:
    client = BinanceRestClient(config.data.rest, config.data.rate_limit, rng=random.Random(0))
    limiter = WeightLimiter(config.data.rate_limit, sleep_fn=_no_sleep)
    client._limiter = limiter  # keep the tests instant
    return (
        Backfiller(client, store, config.data.backfill, config.data.integrity),
        client,
    )


async def _no_sleep(seconds: float) -> None:
    return None


def test_last_closed_bar_excludes_the_forming_one() -> None:
    """The bar containing `now` is still moving; the last closed one precedes it."""
    bar_open = 1_700_000_000_000 // FIFTEEN_MIN_MS * FIFTEEN_MIN_MS

    assert last_closed_open_time(bar_open + 60_000, FIFTEEN_MIN_MS) == bar_open - FIFTEEN_MIN_MS
    # Exactly on a boundary: the bar just opened, so the previous one is last closed.
    assert last_closed_open_time(bar_open, FIFTEEN_MIN_MS) == bar_open - FIFTEEN_MIN_MS


@respx.mock
async def test_full_backfill_paginates_and_stores_everything(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    fake = FakeBinance(exchange_frame)
    respx.get(KLINES).mock(side_effect=fake)
    backfiller, client = build_backfiller(fast_config, store)

    result = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS
    )
    await client.aclose()

    stored = store.read(SYMBOL, TIMEFRAME)
    assert result.requests > 1, "100-row pages over 400 bars must take several calls"
    assert len(stored) == EXCHANGE_BARS
    assert find_gaps(stored, FIFTEEN_MIN_MS) == []
    assert stored["open_time"].is_monotonic_increasing
    assert stored["open_time"].is_unique


@respx.mock
async def test_backfill_never_stores_an_unclosed_bar(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    """The single most damaging silent bug in this layer: a forming bar on disk."""
    # Give the exchange one extra, still-forming bar beyond `NOW_MS`'s boundary.
    forming = make_candles(EXCHANGE_BARS + 1, start_ms=EXCHANGE_START, interval_ms=FIFTEEN_MIN_MS)
    respx.get(KLINES).mock(side_effect=FakeBinance(forming))
    backfiller, client = build_backfiller(fast_config, store)

    await backfiller.backfill(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS)
    await client.aclose()

    stored = store.read(SYMBOL, TIMEFRAME)
    newest = int(stored["open_time"].max())
    assert newest == last_closed_open_time(NOW_MS, FIFTEEN_MIN_MS)
    assert newest + FIFTEEN_MIN_MS <= NOW_MS, "stored bar must have already closed"


@respx.mock
async def test_history_window_is_respected(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    respx.get(KLINES).mock(side_effect=FakeBinance(exchange_frame))
    backfiller, client = build_backfiller(fast_config, store)

    # 1 day of history at 15m = 96 bars.
    await backfiller.backfill(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=1, now_ms=NOW_MS)
    await client.aclose()

    stored = store.read(SYMBOL, TIMEFRAME)
    assert 90 <= len(stored) <= 97
    assert int(stored["open_time"].min()) >= NOW_MS - 86_400_000 - FIFTEEN_MIN_MS


@respx.mock
async def test_resume_does_not_redownload_history(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    fake = FakeBinance(exchange_frame)
    respx.get(KLINES).mock(side_effect=fake)
    backfiller, client = build_backfiller(fast_config, store)

    first = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS
    )
    requests_after_first = len(fake.requests)
    second = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS
    )
    await client.aclose()

    assert first.rows_fetched == EXCHANGE_BARS
    # The resume pass re-reads only the small configured overlap.
    assert second.rows_fetched <= fast_config.data.backfill.resume_overlap_bars + 1
    assert len(fake.requests) - requests_after_first <= 2
    assert len(store.read(SYMBOL, TIMEFRAME)) == EXCHANGE_BARS


@respx.mock
async def test_resume_overlap_corrects_a_stale_final_bar(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    """A bar written from a live stream before it settled must be re-fetched."""
    stale = exchange_frame.iloc[:50].copy()
    stale.loc[49, "close"] = 1.0  # obviously wrong, as if captured mid-formation
    stale.loc[49, "volume"] = 0.1
    store.write(SYMBOL, TIMEFRAME, stale)

    respx.get(KLINES).mock(side_effect=FakeBinance(exchange_frame))
    backfiller, client = build_backfiller(fast_config, store)
    await backfiller.backfill(SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS)
    await client.aclose()

    stored = store.read(SYMBOL, TIMEFRAME)
    corrected = stored.loc[stored["open_time"] == int(exchange_frame["open_time"].iloc[49])]
    assert corrected["close"].item() == pytest.approx(float(exchange_frame["close"].iloc[49]))
    assert corrected["volume"].item() == pytest.approx(float(exchange_frame["volume"].iloc[49]))


@respx.mock
async def test_gaps_are_detected_and_repaired(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    # Store a series with a hole; the exchange still has the missing bars.
    holed = exchange_frame.drop(index=range(100, 130)).reset_index(drop=True)
    store.write(SYMBOL, TIMEFRAME, holed)
    assert len(find_gaps(store.read(SYMBOL, TIMEFRAME), FIFTEEN_MIN_MS)) == 1

    respx.get(KLINES).mock(side_effect=FakeBinance(exchange_frame))
    backfiller, client = build_backfiller(fast_config, store)
    result = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS
    )
    await client.aclose()

    assert len(result.gaps_before_repair) == 1
    assert result.gaps_before_repair[0].missing_bars == 30
    assert result.gaps_after_repair == []
    assert len(store.read(SYMBOL, TIMEFRAME)) == EXCHANGE_BARS


@respx.mock
async def test_unfillable_gap_is_reported_as_an_outage(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    """When Binance itself has no data, say so instead of retrying forever."""
    exchange_with_hole = exchange_frame.drop(index=range(100, 130)).reset_index(drop=True)
    store.write(SYMBOL, TIMEFRAME, exchange_with_hole.drop(index=range(200, 210)))

    respx.get(KLINES).mock(side_effect=FakeBinance(exchange_with_hole))
    backfiller, client = build_backfiller(fast_config, store)
    result = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=3650, now_ms=NOW_MS
    )
    await client.aclose()

    assert result.gaps_after_repair, "the exchange-side hole cannot be filled"
    assert result.outages, "a 30-bar hole exceeds outage_gap_bars and must be flagged"
    assert result.outages[0].missing_bars == 30


@respx.mock
async def test_backfill_on_empty_exchange_response_is_safe(
    fast_config: Config, store: CandleStore
) -> None:
    respx.get(KLINES).mock(return_value=httpx.Response(200, json=[]))
    backfiller, client = build_backfiller(fast_config, store)

    result = await backfiller.backfill(
        SYMBOL, TIMEFRAME, FIFTEEN_MIN_MS, history_days=30, now_ms=NOW_MS
    )
    await client.aclose()

    assert result.rows_written == 0
    assert store.read(SYMBOL, TIMEFRAME).empty


@respx.mock
async def test_current_series_refetches_only_the_overlap(
    fast_config: Config, store: CandleStore, exchange_frame: pd.DataFrame
) -> None:
    """An up-to-date series costs one small request, not a re-download.

    It is deliberately not zero requests: ``resume_overlap_bars`` exists so the
    newest stored bar — which may have been written from a live stream before it
    settled — is re-fetched and corrected.
    """
    store.write(SYMBOL, TIMEFRAME, exchange_frame)
    fake = FakeBinance(exchange_frame)
    respx.get(KLINES).mock(side_effect=fake)
    backfiller, client = build_backfiller(fast_config, store)

    result = await backfiller.backfill(
        SYMBOL,
        TIMEFRAME,
        FIFTEEN_MIN_MS,
        history_days=3650,
        now_ms=EXCHANGE_START + EXCHANGE_BARS * FIFTEEN_MIN_MS,
    )
    await client.aclose()

    overlap = fast_config.data.backfill.resume_overlap_bars
    assert result.requests == 1
    assert result.rows_written <= overlap + 1
    assert len(store.read(SYMBOL, TIMEFRAME)) == EXCHANGE_BARS  # no growth
    # The request started at the overlap point, not at the beginning of history.
    assert int(fake.requests[0]["startTime"]) >= int(
        exchange_frame["open_time"].iloc[-1] - overlap * FIFTEEN_MIN_MS
    )


def test_coverage_summary_reports_missing_bars(exchange_frame: pd.DataFrame) -> None:
    holed = exchange_frame.drop(index=range(10, 20)).reset_index(drop=True)

    summary = summarize_coverage(
        holed,
        FIFTEEN_MIN_MS,
        expected_start=EXCHANGE_START,
        expected_end=EXCHANGE_START + (EXCHANGE_BARS - 1) * FIFTEEN_MIN_MS,
    )

    assert summary["rows"] == EXCHANGE_BARS - 10
    assert summary["expected_bars"] == EXCHANGE_BARS
    assert summary["missing_bars"] == 10
    assert summary["coverage_pct"] == pytest.approx(97.5)
