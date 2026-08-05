"""End-to-end data pipeline: generate -> store -> verify -> read -> replay.

This is the integration test the spec asks for. It runs the same code path the
live engine uses (source -> store -> integrity -> read), with the synthetic
source standing in for Binance because the build environment has no egress to
the exchange. The wiring under test is identical; only the bytes differ.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cse.config import Config
from cse.data.integrity import find_gaps, malformed_ohlc_mask
from cse.data.schema import interval_to_ms
from cse.data.source import (
    PROVENANCE_SYNTHETIC,
    ReplaySource,
    SyntheticSource,
    build_source,
)
from cse.data.store import CandleStore

# A short window keeps the test fast; the pipeline logic is window-independent.
HISTORY_DAYS = 3
NOW_MS = 1_700_000_000_000
TIMEFRAMES = ["1m", "5m", "15m", "1h"]


@pytest.fixture
def pipeline_config(config: Config) -> Config:
    # Generation is anchored to synthetic.start, so pin it near NOW_MS to keep
    # these tests generating days of bars rather than years.
    anchor = pd.Timestamp(NOW_MS - 6 * 86_400_000, unit="ms", tz="UTC").isoformat()
    synthetic = config.data.synthetic.model_copy(update={"start": anchor})
    return config.model_copy(
        update={
            "history_days": HISTORY_DAYS,
            "timeframes": TIMEFRAMES,
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "data": config.data.model_copy(update={"synthetic": synthetic}),
        }
    )


@pytest.fixture
async def populated(pipeline_config: Config, store: CandleStore) -> tuple[Config, CandleStore]:
    source = SyntheticSource(pipeline_config, store, now_ms=NOW_MS)
    for symbol in pipeline_config.symbols:
        for timeframe in pipeline_config.timeframes:
            await source.ensure_history(symbol, timeframe)
    return pipeline_config, store


async def test_pipeline_populates_every_series(
    populated: tuple[Config, CandleStore],
) -> None:
    config, store = populated

    for symbol in config.symbols:
        for timeframe in config.timeframes:
            frame = store.read(symbol, timeframe)
            interval_ms = interval_to_ms(timeframe)
            expected = HISTORY_DAYS * 86_400_000 // interval_ms
            assert len(frame) > 0, f"{symbol} {timeframe} is empty"
            # Allow a couple of bars of slack at the window edges.
            assert abs(len(frame) - expected) <= 2, f"{symbol} {timeframe}: {len(frame)}"


async def test_stored_series_pass_integrity(populated: tuple[Config, CandleStore]) -> None:
    config, store = populated

    for symbol in config.symbols:
        for timeframe in config.timeframes:
            interval_ms = interval_to_ms(timeframe)
            report = store.verify(symbol, timeframe, interval_ms)
            assert report.ok, f"{symbol} {timeframe}: {report.as_dict()}"
            assert report.gaps == [], f"{symbol} {timeframe} has gaps"


async def test_no_unclosed_bar_reaches_the_store(
    populated: tuple[Config, CandleStore],
) -> None:
    """Every stored bar must have closed strictly before `now`."""
    config, store = populated

    for symbol in config.symbols:
        for timeframe in config.timeframes:
            frame = store.read(symbol, timeframe)
            assert int(frame["close_time"].max()) < NOW_MS


async def test_timeframes_agree_with_each_other(
    populated: tuple[Config, CandleStore],
) -> None:
    """A 1h bar must equal the 15m bars inside it.

    Cross-timeframe disagreement would quietly corrupt the multi-timeframe
    confirmation logic in Phase 2, where a 15m signal is judged against its 4h
    context.
    """
    _, store = populated
    fifteen = store.read("BTCUSDT", "15m")
    hourly = store.read("BTCUSDT", "1h")

    bucket = (fifteen["open_time"] // 3_600_000) * 3_600_000
    rebuilt = fifteen.groupby(bucket).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        bars=("open", "size"),
    )
    rebuilt = rebuilt.loc[rebuilt["bars"] == 4]

    joined = hourly.set_index("open_time").join(rebuilt, how="inner", rsuffix="_r")
    assert len(joined) > 20, "not enough overlapping hourly bars to compare"
    for column in ("open", "high", "low", "close", "volume"):
        assert joined[column].to_numpy() == pytest.approx(
            joined[f"{column}_r"].to_numpy(), rel=1e-9
        ), column


async def test_rerunning_the_pipeline_is_idempotent(
    populated: tuple[Config, CandleStore],
) -> None:
    """Restart must not duplicate or re-download; the store must be unchanged."""
    config, store = populated
    before = {(s, tf): store.read(s, tf).copy() for s in config.symbols for tf in config.timeframes}

    source = SyntheticSource(config, store, now_ms=NOW_MS)
    for symbol in config.symbols:
        for timeframe in config.timeframes:
            await source.ensure_history(symbol, timeframe)

    for key, frame in before.items():
        pd.testing.assert_frame_equal(store.read(*key), frame)


async def test_price_at_a_timestamp_does_not_depend_on_when_you_ran(
    pipeline_config: Config, store: CandleStore, tmp_path: Path
) -> None:
    """Regression: a later run must not rewrite the price history of earlier bars.

    The generated path is a cumulative walk indexed from the first bar of the
    window. When that window was anchored to a rolling ``now - history_days``,
    bar index 0 landed on a different timestamp every run, so the same timestamp
    carried a different price — and re-running the backfill silently spliced two
    different realisations together. The seam passed every integrity check
    (gapless, valid OHLC), which is exactly what made it dangerous.

    Generation is now anchored to the fixed ``synthetic.start``, so re-running
    only ever appends.
    """
    later_store = CandleStore(
        pipeline_config.data.storage.model_copy(update={"root": tmp_path / "later"}),
        ohlc_tolerance=pipeline_config.data.integrity.ohlc_tolerance,
    )

    early = SyntheticSource(pipeline_config, store, now_ms=NOW_MS)
    await early.ensure_history("BTCUSDT", "15m")

    # Same config, run "6 hours later".
    late = SyntheticSource(pipeline_config, later_store, now_ms=NOW_MS + 6 * 3_600_000)
    await late.ensure_history("BTCUSDT", "15m")

    first = store.read("BTCUSDT", "15m").set_index("open_time")
    second = later_store.read("BTCUSDT", "15m").set_index("open_time")
    shared = first.index.intersection(second.index)

    assert len(shared) > 100, "runs must overlap substantially"
    pd.testing.assert_frame_equal(first.loc[shared], second.loc[shared])


async def test_appending_new_bars_leaves_no_price_discontinuity(
    pipeline_config: Config, store: CandleStore
) -> None:
    """After a top-up, each bar's open still equals the previous bar's close.

    Synthetic bars chain exactly by construction. A break in that chain means
    two different generated realisations were spliced together — the failure
    mode this anchoring fixes. (Real market data legitimately gaps between bars,
    so this invariant is asserted only for the synthetic source.)
    """
    first = SyntheticSource(pipeline_config, store, now_ms=NOW_MS - 3_600_000)
    await first.ensure_history("BTCUSDT", "1m")

    second = SyntheticSource(pipeline_config, store, now_ms=NOW_MS)
    await second.ensure_history("BTCUSDT", "1m")

    frame = store.read("BTCUSDT", "1m")
    opens = frame["open"].to_numpy()[1:]
    previous_closes = frame["close"].to_numpy()[:-1]
    breaks = np.flatnonzero(np.abs(opens - previous_closes) / previous_closes > 1e-9)

    assert len(frame) > 60, "the second run must actually have appended bars"
    assert breaks.size == 0, f"chain broken at row(s) {breaks[:5]}"


async def test_resume_after_partial_history(pipeline_config: Config, store: CandleStore) -> None:
    """A store holding only older bars is topped up, not rebuilt."""
    earlier = NOW_MS - 2 * 86_400_000
    first = SyntheticSource(pipeline_config, store, now_ms=earlier)
    await first.ensure_history("BTCUSDT", "15m")
    partial_rows = len(store.read("BTCUSDT", "15m"))

    second = SyntheticSource(pipeline_config, store, now_ms=NOW_MS)
    await second.ensure_history("BTCUSDT", "15m")

    full = store.read("BTCUSDT", "15m")
    assert len(full) > partial_rows
    assert find_gaps(full, interval_to_ms("15m")) == []
    assert full["open_time"].is_unique


async def test_replay_yields_bars_in_order_one_at_a_time(
    populated: tuple[Config, CandleStore],
) -> None:
    """The backtester consumes this; it must never see the future."""
    config, store = populated
    replay = ReplaySource(config, store)

    open_times = [int(bar["open_time"]) for bar in replay.replay("BTCUSDT", "15m")]

    assert len(open_times) > 100
    assert open_times == sorted(open_times)
    assert len(set(open_times)) == len(open_times)
    assert open_times == store.read("BTCUSDT", "15m")["open_time"].tolist()


async def test_replayed_bars_are_all_well_formed(
    populated: tuple[Config, CandleStore],
) -> None:
    config, store = populated
    frame = store.read("BTCUSDT", "15m")

    assert not malformed_ohlc_mask(frame, config.data.integrity.ohlc_tolerance).any()
    assert np.isfinite(frame[["open", "high", "low", "close", "volume"]].to_numpy()).all()


async def test_synthetic_data_is_tagged_as_not_real(
    pipeline_config: Config, store: CandleStore
) -> None:
    """Provenance must make a synthetic result impossible to mistake for a real one."""
    source = SyntheticSource(pipeline_config, store, now_ms=NOW_MS)

    assert source.provenance is PROVENANCE_SYNTHETIC
    assert source.provenance.real_market_data is False
    assert "GENERATED DATA" in source.provenance.note


def test_build_source_honours_configured_mode(pipeline_config: Config, store: CandleStore) -> None:
    for mode, expected in (("synthetic", SyntheticSource), ("replay", ReplaySource)):
        data = pipeline_config.data.model_copy(update={"mode": mode})
        built = build_source(pipeline_config.model_copy(update={"data": data}), store)
        assert isinstance(built, expected)
