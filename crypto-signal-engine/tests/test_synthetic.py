"""Synthetic generator: determinism, OHLC validity, cross-timeframe consistency.

The generator's job is to make the pipeline testable offline. These tests check
it produces *structurally valid* market data — they say nothing about realism,
and neither does the generator. See the module docstring in cse.data.synthetic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cse.config import Config
from cse.data.integrity import find_gaps, malformed_ohlc_mask, normalize
from cse.data.synthetic import (
    GENERATION_INTERVAL_MS,
    SyntheticMarket,
    aggregate_candles,
)

FOUR_HOURS_MS = 240 * GENERATION_INTERVAL_MS
# Aligned to 4h so that 1m, 15m, 1h and 4h buckets all start together and the
# aggregation tests are not comparing against partial leading buckets.
START_MS = 1_700_000_000_000 // FOUR_HOURS_MS * FOUR_HOURS_MS
END_MS = START_MS + 5_000 * GENERATION_INTERVAL_MS
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@pytest.fixture
def market(config: Config) -> SyntheticMarket:
    return SyntheticMarket(config.data.synthetic, SYMBOLS)


def test_generation_is_deterministic(config: Config) -> None:
    """A fixed seed must give byte-identical output, or no test built on it is stable."""
    first = SyntheticMarket(config.data.synthetic, SYMBOLS).generate_1m(START_MS, END_MS)
    second = SyntheticMarket(config.data.synthetic, SYMBOLS).generate_1m(START_MS, END_MS)

    for symbol in SYMBOLS:
        pd.testing.assert_frame_equal(first[symbol], second[symbol])


def test_generated_candles_pass_the_integrity_layer(market: SyntheticMarket) -> None:
    frames = market.generate_1m(START_MS, END_MS)

    for symbol, frame in frames.items():
        clean, report = normalize(frame, GENERATION_INTERVAL_MS, ohlc_tolerance=1e-9)
        assert report.ok, f"{symbol}: {report.as_dict()}"
        assert report.gaps == [], f"{symbol} has gaps"
        assert len(clean) == 5_001


def test_ohlc_invariants_hold_on_every_bar(market: SyntheticMarket) -> None:
    frames = market.generate_1m(START_MS, END_MS)

    for symbol, frame in frames.items():
        assert not malformed_ohlc_mask(frame, 1e-9).any(), symbol
        assert (frame["high"] >= frame["low"]).all()
        assert (frame["volume"] > 0).all()
        # Aggressor volume can never exceed total volume.
        assert (frame["taker_buy_base"] <= frame["volume"] + 1e-9).all()


def test_bars_are_contiguous_and_aligned(market: SyntheticMarket) -> None:
    frame = market.generate_1m(START_MS, END_MS)["BTCUSDT"]

    assert (frame["open_time"] % GENERATION_INTERVAL_MS == 0).all()
    assert find_gaps(frame, GENERATION_INTERVAL_MS) == []
    assert (frame["close_time"] == frame["open_time"] + GENERATION_INTERVAL_MS - 1).all()


def test_bars_chain_open_to_previous_close(market: SyntheticMarket) -> None:
    """No phantom gaps between bars: each open equals the prior close."""
    frame = market.generate_1m(START_MS, END_MS)["BTCUSDT"]

    assert frame["open"].to_numpy()[1:] == pytest.approx(frame["close"].to_numpy()[:-1])


def test_alts_are_correlated_with_the_market_factor(market: SyntheticMarket) -> None:
    """Phase 2's BTC-beta-break feature needs real cross-asset structure."""
    frames = market.generate_1m(START_MS, END_MS)
    btc = np.diff(np.log(frames["BTCUSDT"]["close"].to_numpy()))
    eth = np.diff(np.log(frames["ETHUSDT"]["close"].to_numpy()))
    sol = np.diff(np.log(frames["SOLUSDT"]["close"].to_numpy()))

    assert np.corrcoef(btc, eth)[0, 1] > 0.5
    assert np.corrcoef(btc, sol)[0, 1] > 0.5
    # Alts are not carbon copies of each other either.
    assert np.corrcoef(eth, sol)[0, 1] < 0.99


def test_higher_beta_symbol_is_more_volatile(market: SyntheticMarket) -> None:
    frames = market.generate_1m(START_MS, END_MS)
    vol = {s: np.std(np.diff(np.log(frames[s]["close"].to_numpy()))) for s in SYMBOLS}

    assert vol["SOLUSDT"] > vol["ETHUSDT"] > vol["BTCUSDT"]


# ---- aggregation ------------------------------------------------------------


def test_aggregation_preserves_ohlcv_semantics(market: SyntheticMarket) -> None:
    frame = market.generate_1m(START_MS, END_MS)["BTCUSDT"]
    fifteen_min_ms = 15 * GENERATION_INTERVAL_MS

    aggregated = aggregate_candles(frame, fifteen_min_ms)

    first_bucket = frame.iloc[:15]
    row = aggregated.iloc[0]
    assert row["open_time"] == first_bucket["open_time"].iloc[0]
    assert row["open"] == pytest.approx(first_bucket["open"].iloc[0])
    assert row["close"] == pytest.approx(first_bucket["close"].iloc[-1])
    assert row["high"] == pytest.approx(first_bucket["high"].max())
    assert row["low"] == pytest.approx(first_bucket["low"].min())
    assert row["volume"] == pytest.approx(first_bucket["volume"].sum())
    assert row["trades"] == first_bucket["trades"].sum()
    assert row["taker_buy_base"] == pytest.approx(first_bucket["taker_buy_base"].sum())


def test_aggregation_preserves_dtypes(market: SyntheticMarket) -> None:
    aggregated = aggregate_candles(
        market.generate_1m(START_MS, END_MS)["BTCUSDT"], 15 * GENERATION_INTERVAL_MS
    )

    assert aggregated["open_time"].dtype == "int64"
    assert aggregated["trades"].dtype == "int64"
    assert aggregated["close"].dtype == "float64"


def test_aggregation_drops_the_incomplete_trailing_bucket() -> None:
    """Emitting a partial bucket is the unclosed-candle bug in another costume."""
    from tests.conftest import make_candles

    frame = make_candles(20, interval_ms=GENERATION_INTERVAL_MS, start_ms=START_MS)

    aggregated = aggregate_candles(frame, 15 * GENERATION_INTERVAL_MS)

    assert len(aggregated) == 1  # 20 minutes = one complete 15m bar, 5 left over


def test_aggregation_drops_the_incomplete_leading_bucket() -> None:
    """Input starting mid-bucket must not yield a bar missing its own opening."""
    from tests.conftest import make_candles

    # Start 5 minutes into a 15m bucket: the first bucket can never be complete.
    frame = make_candles(
        40, interval_ms=GENERATION_INTERVAL_MS, start_ms=START_MS + 5 * GENERATION_INTERVAL_MS
    )

    aggregated = aggregate_candles(frame, 15 * GENERATION_INTERVAL_MS)

    assert len(aggregated) == 2
    # First emitted bar is the next aligned boundary, not the partial one.
    assert int(aggregated["open_time"].iloc[0]) == START_MS + 15 * GENERATION_INTERVAL_MS
    assert (aggregated["open_time"] % (15 * GENERATION_INTERVAL_MS) == 0).all()


def test_aggregated_bars_are_contiguous(market: SyntheticMarket) -> None:
    frame = market.generate_1m(START_MS, END_MS)["BTCUSDT"]
    four_hours_ms = 240 * GENERATION_INTERVAL_MS

    aggregated = aggregate_candles(frame, four_hours_ms)

    assert find_gaps(aggregated, four_hours_ms) == []
    assert not malformed_ohlc_mask(aggregated, 1e-9).any()


def test_aggregation_is_transitive(market: SyntheticMarket) -> None:
    """1m -> 1h must equal 1m -> 15m -> 1h, or multi-timeframe features disagree."""
    frame = market.generate_1m(START_MS, END_MS)["BTCUSDT"]
    fifteen = 15 * GENERATION_INTERVAL_MS
    hour = 60 * GENERATION_INTERVAL_MS

    direct = aggregate_candles(frame, hour)
    staged = aggregate_candles(aggregate_candles(frame, fifteen), hour)

    n = min(len(direct), len(staged))
    pd.testing.assert_frame_equal(direct.iloc[:n], staged.iloc[:n])


def test_refuses_to_aggregate_downward(market: SyntheticMarket) -> None:
    frame = market.generate_1m(START_MS, START_MS + 100 * GENERATION_INTERVAL_MS)["BTCUSDT"]
    fifteen = aggregate_candles(frame, 15 * GENERATION_INTERVAL_MS)

    with pytest.raises(ValueError, match="target is finer"):
        aggregate_candles(fifteen, GENERATION_INTERVAL_MS)


def test_refuses_non_multiple_intervals(market: SyntheticMarket) -> None:
    frame = market.generate_1m(START_MS, START_MS + 100 * GENERATION_INTERVAL_MS)["BTCUSDT"]

    with pytest.raises(ValueError, match="not a multiple"):
        aggregate_candles(frame, 7 * GENERATION_INTERVAL_MS + 1)
