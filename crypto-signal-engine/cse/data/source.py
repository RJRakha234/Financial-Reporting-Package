"""Pluggable market-data sources behind one interface.

Three implementations share a single contract so that every downstream layer —
features, backtest, decision engine, dashboard — is written once and does not
care where candles came from:

``LiveBinanceSource``
    Real Binance public REST + WebSocket. The production path.

``ReplaySource``
    Reads candles already in the local store and replays them in bar order.
    Used by the backtester and to re-run a session deterministically.

``SyntheticSource``
    Generated data (see :mod:`cse.data.synthetic`). Exists because this build
    environment has no network route to Binance. Results carry the
    ``synthetic`` provenance tag and are evidence about plumbing only.

Every source stamps :attr:`MarketDataSource.provenance` onto the data it
produces, and every report downstream prints it. A synthetic backtest can
therefore never be mistaken for a real one.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pandas as pd

from cse.config import Config
from cse.data.backfill import Backfiller, last_closed_open_time
from cse.data.integrity import IntegrityReport, normalize
from cse.data.rest import BinanceRestClient
from cse.data.schema import floor_to_interval, interval_to_ms
from cse.data.store import CandleStore
from cse.data.synthetic import GENERATION_INTERVAL_MS, SyntheticMarket, aggregate_candles
from cse.data.ws import MarketEvent, MarketStream, StreamHealth
from cse.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class Provenance:
    """Where a dataset came from. Attached to every report the engine emits."""

    source: str
    real_market_data: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "real_market_data": self.real_market_data,
            "note": self.note,
        }


PROVENANCE_LIVE = Provenance(
    source="binance_live",
    real_market_data=True,
    note="Binance public REST/WebSocket market data.",
)
PROVENANCE_REPLAY = Provenance(
    source="store_replay",
    real_market_data=True,
    note="Replayed from the local store; provenance is that of the original fetch.",
)
PROVENANCE_SYNTHETIC = Provenance(
    source="synthetic",
    real_market_data=False,
    note=(
        "GENERATED DATA. Regime-switching GBM with a common market factor. "
        "Validates pipeline correctness only; carries no evidence about strategy "
        "profitability."
    ),
)


class MarketDataSource(ABC):
    """Contract every data source implements."""

    provenance: Provenance

    @abstractmethod
    async def ensure_history(self, symbol: str, timeframe: str) -> IntegrityReport:
        """Make the configured history window available in the local store."""

    @abstractmethod
    def load(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        """Read a candle range from the store."""

    async def aclose(self) -> None:
        """Release any held resources."""
        return None


class LiveBinanceSource(MarketDataSource):
    """Binance public REST for history, WebSocket for live events."""

    provenance = PROVENANCE_LIVE

    def __init__(
        self,
        config: Config,
        store: CandleStore,
        *,
        client: BinanceRestClient | None = None,
        stream: MarketStream | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client or BinanceRestClient(config.data.rest, config.data.rate_limit)
        self._backfiller = Backfiller(
            self._client, store, config.data.backfill, config.data.integrity
        )
        self._stream = stream or MarketStream(
            config.data.websocket,
            config.symbols,
            config.timeframes,
            intrabar=config.data.intrabar,
            rest_client=self._client,
        )

    @property
    def stream(self) -> MarketStream:
        return self._stream

    @property
    def health(self) -> StreamHealth:
        return self._stream.health

    async def ensure_history(self, symbol: str, timeframe: str) -> IntegrityReport:
        interval_ms = interval_to_ms(timeframe)
        await self._backfiller.backfill(
            symbol, timeframe, interval_ms, history_days=self._config.history_days
        )
        return self._store.verify(symbol, timeframe, interval_ms)

    def load(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        return self._store.read(symbol, timeframe, start_time=start_time, end_time=end_time)

    async def events(self) -> AsyncIterator[MarketEvent]:
        """Yield live events. Caller is responsible for running ``stream.run()``."""
        while True:
            yield await self._stream.events.get()

    async def aclose(self) -> None:
        self._stream.stop()
        await self._client.aclose()


class SyntheticSource(MarketDataSource):
    """Generated candles written into the same store the live path uses."""

    provenance = PROVENANCE_SYNTHETIC

    def __init__(self, config: Config, store: CandleStore, *, now_ms: int | None = None) -> None:
        self._config = config
        self._store = store
        self._market = SyntheticMarket(config.data.synthetic, config.symbols)
        self._now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self._cache_1m: dict[str, pd.DataFrame] = {}

    def _generation_window(self) -> tuple[int, int]:
        """Bars to generate: ALWAYS anchored to the fixed configured start.

        This anchor is the whole correctness argument for the synthetic source,
        and getting it wrong is subtle.

        The generated path is a cumulative random walk indexed from the first
        bar of the window. Anchoring the window to a rolling ``now -
        history_days`` therefore makes bar *index* 0 land on a different
        *timestamp* on every run — so the same timestamp carries a different
        price each time. Re-running the backfill then silently rewrites stored
        history and splices two different realisations together at the seam,
        and no integrity check catches it (the result is still gapless, still
        valid OHLC — real markets gap between bars, so continuity cannot be
        asserted globally).

        Anchoring to the fixed ``synthetic.start`` makes the price at a given
        timestamp a pure function of config, so re-running only ever appends.
        The cost is that generation spans start-to-now rather than just the
        retained window; retention is applied separately in
        :meth:`_storage_window`.
        """
        return self._market.start_ms, last_closed_open_time(self._now_ms, GENERATION_INTERVAL_MS)

    def _storage_window(self) -> tuple[int, int]:
        """Bars to persist: the trailing ``history_days`` of the generated path."""
        generation_start, end = self._generation_window()
        retention_start = floor_to_interval(
            self._now_ms - self._config.history_days * 86_400_000, GENERATION_INTERVAL_MS
        )
        return max(generation_start, retention_start), end

    def _base_1m(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._cache_1m:
            start, end = self._generation_window()
            _log.info("synthetic.generating", symbol=symbol, start_ms=start, end_ms=end)
            self._cache_1m.update(self._market.generate_1m(start, end))
        return self._cache_1m[symbol]

    async def ensure_history(self, symbol: str, timeframe: str) -> IntegrityReport:
        interval_ms = interval_to_ms(timeframe)
        existing = self._store.last_open_time(symbol, timeframe)
        expected_last = last_closed_open_time(self._now_ms, interval_ms)
        if existing is not None and existing >= expected_last:
            _log.info("synthetic.up_to_date", symbol=symbol, timeframe=timeframe)
            return self._store.verify(symbol, timeframe, interval_ms)

        base = self._base_1m(symbol)
        frame = (
            base if interval_ms == GENERATION_INTERVAL_MS else aggregate_candles(base, interval_ms)
        )
        retention_start, _ = self._storage_window()
        # Same rule as the live path: only closed bars reach the store.
        frame = frame.loc[
            (frame["open_time"] >= retention_start) & (frame["open_time"] <= expected_last)
        ]
        self._store.write(symbol, timeframe, frame)
        return self._store.verify(symbol, timeframe, interval_ms)

    def load(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        return self._store.read(symbol, timeframe, start_time=start_time, end_time=end_time)

    def release_cache(self) -> None:
        """Drop generated 1m frames once every timeframe has been written."""
        self._cache_1m.clear()


class ReplaySource(MarketDataSource):
    """Deterministic bar-by-bar replay of stored candles.

    The backtester consumes this. It intentionally exposes only *one bar at a
    time*: a replay that handed over the whole frame would make look-ahead bias
    a one-typo mistake.
    """

    provenance = PROVENANCE_REPLAY

    def __init__(self, config: Config, store: CandleStore) -> None:
        self._config = config
        self._store = store

    async def ensure_history(self, symbol: str, timeframe: str) -> IntegrityReport:
        interval_ms = interval_to_ms(timeframe)
        return self._store.verify(symbol, timeframe, interval_ms)

    def load(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        return self._store.read(symbol, timeframe, start_time=start_time, end_time=end_time)

    def replay(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Iterator[pd.Series]:
        """Yield closed bars in chronological order, one at a time."""
        frame = self.load(symbol, timeframe, start_time=start_time, end_time=end_time)
        clean, report = normalize(
            frame,
            interval_to_ms(timeframe),
            ohlc_tolerance=self._config.data.integrity.ohlc_tolerance,
        )
        if not report.ok:
            _log.warning(
                "replay.normalized", symbol=symbol, timeframe=timeframe, **report.as_dict()
            )
        for _, row in clean.iterrows():
            yield row


def build_source(config: Config, store: CandleStore) -> MarketDataSource:
    """Instantiate the source named by ``data.mode`` in config.yaml."""
    if config.data.mode == "live":
        return LiveBinanceSource(config, store)
    if config.data.mode == "synthetic":
        return SyntheticSource(config, store)
    if config.data.mode == "replay":
        return ReplaySource(config, store)
    raise ValueError(f"unknown data.mode: {config.data.mode!r}")
