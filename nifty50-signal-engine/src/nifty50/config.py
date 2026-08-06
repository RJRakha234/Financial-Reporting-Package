"""Typed loader for ``config.yaml`` plus ``.env`` secrets.

Every parameter the engine uses is validated here once, at startup, so that a
typo in the YAML fails immediately with a readable error instead of surfacing as
a ``KeyError`` inside a websocket callback three hours into the session.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nifty50.domain import Exchange, InstrumentKind, Timeframe


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(_Base):
    timezone: str
    mode: Literal["paper", "backtest"]
    intrabar_signals: bool

    @field_validator("timezone")
    @classmethod
    def _must_be_ist(cls, v: str) -> str:
        # The whole engine assumes IST internally; making this configurable would be
        # a lie, so we validate it instead of pretending to support alternatives.
        if v != "Asia/Kolkata":
            raise ValueError("runtime.timezone must be Asia/Kolkata")
        return v


class IndexInstrumentConfig(_Base):
    symbol: str
    exchange: Exchange
    kind: InstrumentKind


class UniverseConfig(_Base):
    index: str
    constituents_file: Path
    require_point_in_time: bool
    expected_size: int
    index_instruments: tuple[IndexInstrumentConfig, ...]


class KiteConfig(_Base):
    api_key_env: str
    api_secret_env: str
    access_token_env: str
    request_token_env: str
    token_expiry_local_time: dt.time
    max_ws_instruments: int
    max_ws_connections: int
    historical_chunk_days: dict[Timeframe, int]
    rest_rate_limit_per_second: dict[str, int]


class ReplayConfig(_Base):
    root: Path


class BrokerConfig(_Base):
    name: Literal["kite", "replay"]
    kite: KiteConfig
    replay: ReplayConfig


class StoreConfig(_Base):
    root: Path
    format: Literal["parquet"]
    partition_granularity: Literal["month"]
    compression: str


class BhavcopyConfig(_Base):
    root: Path
    availability_lag_sessions: int


class IntegrityConfig(_Base):
    suspect_unadjusted_abs_log_return: float
    reconcile_price_rel_tolerance: float
    reconcile_volume_rel_tolerance: float
    max_missing_bars_pct_before_quarantine: float


class DataConfig(_Base):
    timeframes: tuple[Timeframe, ...]
    base_timeframe: Timeframe
    history_years: int
    store: StoreConfig
    bhavcopy: BhavcopyConfig
    integrity: IntegrityConfig

    @field_validator("base_timeframe")
    @classmethod
    def _base_must_be_listed(cls, v: Timeframe, info: Any) -> Timeframe:
        timeframes = info.data.get("timeframes")
        if timeframes is not None and v not in timeframes:
            raise ValueError(f"base_timeframe {v.value} is not in data.timeframes")
        return v


class StreamConfig(_Base):
    heartbeat_timeout_seconds: float
    reconnect_backoff_initial_seconds: float
    reconnect_backoff_max_seconds: float
    reconnect_backoff_multiplier: float
    reconnect_jitter_fraction: float
    max_consecutive_reconnects_before_halt: int
    rest_fallback_after_seconds: float
    rest_fallback_poll_seconds: float
    idle_poll_seconds_when_closed: float


class SessionWindowConfig(_Base):
    start: dt.time
    end: dt.time


class SessionsConfig(_Base):
    pre_open: SessionWindowConfig
    continuous: SessionWindowConfig
    post_close: SessionWindowConfig


class CalendarConfig(_Base):
    holidays_file: Path
    special_sessions_file: Path
    sessions: SessionsConfig
    anchor_intraday_bars_to_session_open: bool


class CorporateActionsConfig(_Base):
    actions_file: Path
    adjust_on_read: bool
    adjust_dividends: bool
    feature_series: Literal["adjusted", "raw"]
    cost_and_band_series: Literal["adjusted", "raw"]


class CapitalConfig(_Base):
    starting_capital_inr: float
    risk_per_trade_fraction: float
    trade_style: Literal["intraday", "swing", "positional"]


class BrokerageConfig(_Base):
    delivery_pct: float
    delivery_flat_inr: float
    intraday_pct: float
    intraday_cap_inr: float


class SttConfig(_Base):
    delivery_buy_pct: float
    delivery_sell_pct: float
    intraday_sell_pct: float


class StampDutyConfig(_Base):
    delivery_buy_pct: float
    intraday_buy_pct: float


class SlippageConfig(_Base):
    model: Literal["spread_and_impact", "flat"]
    fallback_bps: float


class CostsConfig(_Base):
    effective_from: dt.date
    brokerage: BrokerageConfig
    stt: SttConfig
    exchange_txn_charge_pct: float
    sebi_turnover_fee_pct: float
    ipft_pct: float
    stamp_duty: StampDutyConfig
    gst_pct: float
    dp_charge_per_scrip_per_sell_inr: float
    slippage: SlippageConfig
    impact_cost_file: Path
    settlement_cycle_days: int


class DashboardConfig(_Base):
    host: str
    port: int


class AlertsConfig(_Base):
    channels: tuple[str, ...]
    min_confidence: float
    rate_limit_per_symbol_minutes: int
    rate_limit_global_per_hour: int


class LoggingConfig(_Base):
    level: str
    dir: Path
    file: str
    max_bytes: int
    backup_count: int
    json_lines: bool


class Config(_Base):
    """The whole of ``config.yaml``, validated."""

    runtime: RuntimeConfig
    universe: UniverseConfig
    broker: BrokerConfig
    data: DataConfig
    stream: StreamConfig
    calendar: CalendarConfig
    corporate_actions: CorporateActionsConfig
    capital: CapitalConfig
    costs: CostsConfig
    dashboard: DashboardConfig
    alerts: AlertsConfig
    logging: LoggingConfig

    # Populated by later phases; accepted as opaque mappings for now so that a
    # Phase-5 key does not have to exist before Phase 5 is written.
    features: dict[str, Any] = Field(default_factory=dict)
    ml: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    backtest: dict[str, Any] = Field(default_factory=dict)

    # Set by :func:`load_config`; all relative paths in the file resolve against it.
    project_root: Path = Field(default=Path("."))

    def path(self, relative: Path) -> Path:
        """Resolve a config-declared path against the project root."""
        return relative if relative.is_absolute() else (self.project_root / relative)


def _default_config_path() -> Path:
    override = os.environ.get("NIFTY50_CONFIG")
    if override:
        return Path(override)
    return project_root() / "config.yaml"


def project_root() -> Path:
    """Repository directory holding ``config.yaml`` (…/nifty50-signal-engine)."""
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> Config:
    """Read, validate and return the configuration.

    Not cached, so tests can load bespoke configs. Application code should call
    :func:`get_config`.
    """
    config_path = path or _default_config_path()
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle)
    # Later-phase sections are declared as `{}` in the YAML; keep them permissive.
    for optional_section in ("features", "ml", "decision", "backtest"):
        if raw.get(optional_section) is None:
            raw[optional_section] = {}
    raw["project_root"] = config_path.resolve().parent
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide configuration singleton."""
    return load_config()
