"""Shared fixtures.

Every test runs against the real ``config.yaml`` and the real reference data, so
a mistake in either is caught by the suite rather than by a live session. Only
the storage root is redirected, to a per-test temporary directory.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nifty50.config import Config, load_config, project_root
from nifty50.corporate_actions import CorporateActionSet
from nifty50.data.store import BarStore
from nifty50.domain import Exchange, Instrument, InstrumentKind
from nifty50.trading_calendar import TradingCalendar

PROJECT_ROOT: Path = project_root()

# A normal, unremarkable trading week in 2025 used across the suite.
# 2025-08-04 Mon .. 2025-08-08 Fri; 2025-08-15 (Fri) is Independence Day.
SAMPLE_WEEK_START = dt.date(2025, 8, 4)
SAMPLE_WEEK_END = dt.date(2025, 8, 8)


@pytest.fixture(scope="session")
def real_config() -> Config:
    return load_config(PROJECT_ROOT / "config.yaml")


@pytest.fixture
def config(real_config: Config, tmp_path: Path) -> Config:
    """The real config with storage redirected into ``tmp_path``."""
    store = real_config.data.store.model_copy(update={"root": tmp_path / "store"})
    data = real_config.data.model_copy(update={"store": store})
    replay = real_config.broker.replay.model_copy(update={"root": tmp_path / "replay"})
    broker = real_config.broker.model_copy(update={"replay": replay, "name": "replay"})
    return real_config.model_copy(update={"data": data, "broker": broker})


@pytest.fixture(scope="session")
def calendar(real_config: Config) -> TradingCalendar:
    return TradingCalendar.from_config(real_config)


@pytest.fixture(scope="session")
def actions(real_config: Config) -> CorporateActionSet:
    return CorporateActionSet.from_csv(real_config.path(real_config.corporate_actions.actions_file))


@pytest.fixture
def store(config: Config) -> BarStore:
    return BarStore.from_config(config)


@pytest.fixture
def reliance() -> Instrument:
    return Instrument(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        kind=InstrumentKind.EQUITY,
        broker_token=738561,
        tick_size=0.05,
        lot_size=1,
    )


@pytest.fixture
def hdfcbank() -> Instrument:
    return Instrument(
        symbol="HDFCBANK",
        exchange=Exchange.NSE,
        kind=InstrumentKind.EQUITY,
        broker_token=341249,
        tick_size=0.05,
        lot_size=1,
    )
