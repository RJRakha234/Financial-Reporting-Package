"""Shared fixtures. Every test runs against a temp store; none touch the network."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from cse.config import DEFAULT_CONFIG_PATH, Config
from cse.data.schema import CANDLE_COLUMNS, coerce_dtypes
from cse.data.store import CandleStore

MINUTE_MS = 60_000
FIFTEEN_MIN_MS = 15 * MINUTE_MS


@pytest.fixture(scope="session")
def raw_config() -> dict[str, Any]:
    """The real config.yaml, so tests fail if a key is renamed without updating it."""
    loaded = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture
def config(raw_config: dict[str, Any], tmp_path: Path) -> Config:
    """Validated config with storage and logs redirected into a temp directory."""
    data = copy.deepcopy(raw_config)
    data["data"]["storage"]["root"] = str(tmp_path / "store")
    data["logging"]["directory"] = str(tmp_path / "logs")
    data["logging"]["console"] = False
    return Config.model_validate(data)


@pytest.fixture
def store(config: Config) -> CandleStore:
    return CandleStore(config.data.storage, ohlc_tolerance=config.data.integrity.ohlc_tolerance)


def make_candles(
    n: int,
    *,
    start_ms: int = 1_700_000_000_000 // FIFTEEN_MIN_MS * FIFTEEN_MIN_MS,
    interval_ms: int = FIFTEEN_MIN_MS,
    start_price: float = 100.0,
    step: float = 1.0,
) -> pd.DataFrame:
    """A clean, well-formed, gap-free candle frame for tests.

    Deterministic on purpose: assertions reference exact values, so a change in
    the generator shows up as a test failure rather than as flakiness.
    """
    open_time = start_ms + np.arange(n, dtype=np.int64) * interval_ms
    opens = start_price + np.arange(n, dtype=np.float64) * step
    closes = opens + step * 0.5
    highs = np.maximum(opens, closes) + 0.25
    lows = np.minimum(opens, closes) - 0.25
    volume = np.full(n, 10.0)
    frame = pd.DataFrame(
        {
            "open_time": open_time,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volume,
            "close_time": open_time + interval_ms - 1,
            "quote_volume": volume * closes,
            "trades": np.full(n, 5, dtype=np.int64),
            "taker_buy_base": volume * 0.6,
            "taker_buy_quote": volume * closes * 0.6,
        }
    )
    return coerce_dtypes(frame)


def to_rest_rows(frame: pd.DataFrame) -> list[list[object]]:
    """Render a candle frame the way /api/v3/klines does: arrays of strings."""
    rows: list[list[object]] = []
    for record in frame.to_dict("records"):
        rows.append(
            [
                int(record["open_time"]),
                f"{record['open']:.8f}",
                f"{record['high']:.8f}",
                f"{record['low']:.8f}",
                f"{record['close']:.8f}",
                f"{record['volume']:.8f}",
                int(record["close_time"]),
                f"{record['quote_volume']:.8f}",
                int(record["trades"]),
                f"{record['taker_buy_base']:.8f}",
                f"{record['taker_buy_quote']:.8f}",
                "0",  # documented "ignore" field
            ]
        )
    return rows


@pytest.fixture
def candles() -> pd.DataFrame:
    return make_candles(200)


__all__ = ["CANDLE_COLUMNS", "FIFTEEN_MIN_MS", "MINUTE_MS", "make_candles", "to_rest_rows"]
