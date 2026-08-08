"""Replaying stored bars into the dashboard.

The failure this module exists to prevent is a silent one: a dashboard that
connects, reports itself healthy, and draws nothing -- or worse, draws bars
rebuilt from a source too coarse to shape them and says nothing about it.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from nifty50.dashboard.replay_driver import ReplayDataError, ReplayDriver
from nifty50.dashboard.state import DashboardState, RiskContext
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.replay import ReplayAdapter
from nifty50.data.stream import EngineState
from nifty50.domain import IST, Exchange, Timeframe
from nifty50.trading_calendar import TradingCalendar


def write_root(root: Path, *, symbol: str = "RELIANCE", timeframes: dict[str, int]) -> None:
    """Build a replay root of ``timeframes`` -> bar minutes, one session."""
    (root).mkdir(parents=True, exist_ok=True)
    with (root / "instruments.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "exchange", "kind", "token", "lot_size", "tick_size"]
        )
        writer.writeheader()
        writer.writerow(
            {"symbol": symbol, "exchange": "NSE", "kind": "equity",
             "token": "1", "lot_size": "", "tick_size": "0.05"}
        )
    for name, minutes in timeframes.items():
        start = dt.datetime(2026, 8, 7, 9, 15, tzinfo=IST)
        count = 375 // minutes
        index = pd.DatetimeIndex(
            [start + dt.timedelta(minutes=minutes * i) for i in range(count)], name="ts"
        )
        price = pd.Series([100.0 + i * 0.1 for i in range(count)], index=index)
        frame = pd.DataFrame(
            {
                "open": price, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": [1000] * count,
            }
        )
        directory = root / "NSE" / symbol
        directory.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(directory / f"{name}.parquet")


@pytest.fixture
def calendar(config):  # type: ignore[no-untyped-def]
    return TradingCalendar.from_config(config)


def build(config, root: Path, calendar, *, out: list[Timeframe]):  # type: ignore[no-untyped-def]
    adapter = ReplayAdapter(config, root=root)
    adapter.authenticate()
    instrument = adapter.resolve("RELIANCE", Exchange.NSE)
    state = DashboardState(risk=RiskContext(500_000.0, 0.01, 0.00083, atr_pct=0.003))
    aggregators = [CandleAggregator(calendar, timeframe) for timeframe in out]
    driver = ReplayDriver(
        adapter=adapter, aggregators=aggregators, state=state,
        instruments=[instrument], seconds_per_tick=0.0, max_sessions=None,
    )
    return driver, state


class TestSourceSelection:
    def test_it_picks_the_finest_stored_series(self, config, calendar, tmp_path) -> None:
        """One tick per output bar makes every candle a doji. Sampling the
        source more often is the only thing that gives the bar a shape."""
        write_root(tmp_path, timeframes={"1m": 1, "5m": 5, "15m": 15})
        driver, _ = build(config, tmp_path, calendar, out=[Timeframe.M15])
        assert driver.source_timeframe() is Timeframe.M1

    def test_it_refuses_a_source_coarser_than_the_bars_requested(
        self, config, calendar, tmp_path
    ) -> None:
        """15-minute bars cannot be built from 30-minute closes. The chart
        would look entirely plausible and be wrong."""
        write_root(tmp_path, timeframes={"30m": 30})
        driver, _ = build(config, tmp_path, calendar, out=[Timeframe.M15])
        with pytest.raises(ReplayDataError, match="at least as fine"):
            driver.source_timeframe()

    def test_an_empty_root_says_how_to_populate_it(self, config, calendar, tmp_path) -> None:
        write_root(tmp_path, timeframes={"5m": 5})
        (tmp_path / "NSE" / "RELIANCE" / "5m.parquet").unlink()
        driver, _ = build(config, tmp_path, calendar, out=[Timeframe.M15])
        with pytest.raises(ValueError, match="build_replay"):
            driver.source_timeframe()


class TestReplay:
    def test_bars_reach_the_dashboard(self, config, calendar, tmp_path) -> None:
        write_root(tmp_path, timeframes={"5m": 5})
        driver, state = build(config, tmp_path, calendar, out=[Timeframe.M15])
        asyncio.run(driver.run())

        payload = state.series_json("NSE:RELIANCE", Timeframe.M15)
        assert payload is not None
        assert len(payload["closed"]) == 25  # 375 minutes / 15
        assert state.health_json()["ticks_received"] == 75

    def test_the_bars_have_a_shape_rather_than_being_dojis(
        self, config, calendar, tmp_path
    ) -> None:
        """Three 5-minute closes per 15-minute bar must produce a real range.
        Equal OHLC everywhere is the signature of a source that is too coarse."""
        write_root(tmp_path, timeframes={"5m": 5})
        driver, state = build(config, tmp_path, calendar, out=[Timeframe.M15])
        asyncio.run(driver.run())
        payload = state.series_json("NSE:RELIANCE", Timeframe.M15)
        assert payload is not None
        ranged = [b for b in payload["closed"] if b["high"] > b["low"]]
        assert len(ranged) == len(payload["closed"])

    def test_it_never_reports_itself_as_live(self, config, calendar, tmp_path) -> None:
        """A replay that claims LIVE is how a weekend rehearsal gets mistaken
        for a market test."""
        write_root(tmp_path, timeframes={"5m": 5})
        driver, state = build(config, tmp_path, calendar, out=[Timeframe.M15])
        driver.seconds_per_tick = 0.001  # leave the observer room to sample
        seen: set[str] = set()

        async def observe() -> None:
            while True:
                seen.add(state.health_json()["state"])
                await asyncio.sleep(0.002)

        async def go() -> None:
            watcher = asyncio.create_task(observe())
            await driver.run()
            watcher.cancel()

        asyncio.run(go())
        assert seen  # the observer actually sampled something
        assert EngineState.LIVE.value not in seen
        assert EngineState.REST_FALLBACK.value in seen

    def test_the_fidelity_caveat_is_shown(self, config, calendar, tmp_path) -> None:
        """The cost-hurdle panel reads the bar range, and a range rebuilt from
        sampled closes is biased low. The viewer has to be told."""
        write_root(tmp_path, timeframes={"5m": 5})
        driver, state = build(config, tmp_path, calendar, out=[Timeframe.M15])
        asyncio.run(driver.run())
        notice = state.health_json()["notice"]
        assert "approximate" in notice
        assert "3 per 15m bar" in notice

    def test_stopping_early_leaves_a_usable_chart(self, config, calendar, tmp_path) -> None:
        write_root(tmp_path, timeframes={"5m": 5})
        driver, state = build(config, tmp_path, calendar, out=[Timeframe.M15])
        driver.request_stop()
        asyncio.run(driver.run())
        assert state.health_json()["state"] == EngineState.IDLE_MARKET_CLOSED.value
