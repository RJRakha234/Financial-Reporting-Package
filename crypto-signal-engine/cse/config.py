"""Typed configuration loaded from ``config.yaml`` plus secrets from ``.env``.

Design rule for this project: no magic numbers in code. Anything a reasonable
person might want to tune lives in ``config.yaml`` and is validated here, so a
bad value fails at startup with a precise message instead of producing a subtly
wrong indicator 40 minutes into a run.

Secrets never appear in ``config.yaml``. They come from the environment (see
``.env.example``) and are optional — the engine runs entirely on Binance public
endpoints, and no code path in this package can place an order.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cse.data.schema import interval_to_ms

Fraction = Annotated[float, Field(gt=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


class _Base(BaseModel):
    """Forbid unknown keys so a typo in config.yaml is an error, not a no-op."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RestConfig(_Base):
    base_url: str
    fallback_base_url: str
    timeout_seconds: Positive
    max_retries: int = Field(ge=0)
    backoff_initial_seconds: Positive
    backoff_multiplier: float = Field(gt=1.0)
    backoff_max_seconds: Positive
    backoff_jitter: float = Field(ge=0.0, lt=1.0)


class RateLimitConfig(_Base):
    weight_limit_per_minute: int = Field(gt=0)
    soft_threshold_pct: Fraction
    hard_threshold_pct: Fraction
    endpoint_weights: dict[str, int]
    retry_after_default_seconds: Positive
    ip_ban_backoff_seconds: Positive

    @model_validator(mode="after")
    def _soft_below_hard(self) -> RateLimitConfig:
        if self.soft_threshold_pct >= self.hard_threshold_pct:
            raise ValueError("soft_threshold_pct must be below hard_threshold_pct")
        return self

    @field_validator("endpoint_weights")
    @classmethod
    def _weights_positive(cls, value: dict[str, int]) -> dict[str, int]:
        bad = {k: v for k, v in value.items() if v <= 0}
        if bad:
            raise ValueError(f"endpoint weights must be positive: {bad}")
        return value


class BackfillConfig(_Base):
    klines_limit: int = Field(gt=0, le=1000)  # Binance hard cap
    resume_overlap_bars: int = Field(ge=0)


class WebsocketConfig(_Base):
    base_url: str
    ping_interval_seconds: Positive
    ping_timeout_seconds: Positive
    rest_fallback_after_seconds: Positive
    rest_fallback_poll_seconds: Positive
    reconnect_initial_seconds: Positive
    reconnect_multiplier: float = Field(gt=1.0)
    reconnect_max_seconds: Positive
    reconnect_jitter: float = Field(ge=0.0, lt=1.0)
    depth_levels: int = Field(gt=0)
    depth_update_ms: int = Field(gt=0)
    max_connection_seconds: Positive


class StorageConfig(_Base):
    root: Path
    format: Literal["parquet", "duckdb"]
    compression: str
    partition_by: list[str]


class IntegrityConfig(_Base):
    ohlc_tolerance: NonNegative
    reconcile_price_tolerance: NonNegative
    reconcile_volume_tolerance: NonNegative
    outage_gap_bars: int = Field(gt=0)
    max_gap_repair_requests: int = Field(ge=0)
    resume_verify_bars: int = Field(gt=0)


class SyntheticConfig(_Base):
    seed: int
    start: str
    initial_price: dict[str, Positive]
    annual_drift: float
    annual_volatility: Positive
    regime_switch_probability: Fraction
    regime_vol_multipliers: list[Positive]
    bars_per_year: int = Field(gt=0)
    base_volume: Positive
    volume_noise: NonNegative
    taker_buy_ratio_mean: Fraction
    taker_buy_ratio_std: NonNegative
    market_factor_symbol: str
    beta: dict[str, float]
    idiosyncratic_vol_fraction: Fraction
    volume_volatility_elasticity: NonNegative
    intrabar_substeps: int = Field(gt=0)


class DataConfig(_Base):
    mode: Literal["live", "replay", "synthetic"]
    intrabar: bool
    rest: RestConfig
    rate_limit: RateLimitConfig
    backfill: BackfillConfig
    websocket: WebsocketConfig
    storage: StorageConfig
    integrity: IntegrityConfig
    synthetic: SyntheticConfig


class TrendFeatureConfig(_Base):
    ema_periods: list[int]
    macd_fast: int = Field(gt=0)
    macd_slow: int = Field(gt=0)
    macd_signal: int = Field(gt=0)
    rsi_period: int = Field(gt=1)
    rsi_divergence_lookback: int = Field(gt=0)
    rsi_pivot_window: int = Field(gt=0)
    adx_period: int = Field(gt=1)
    adx_trend_threshold: float = Field(gt=0.0)
    ichimoku_conversion: int = Field(gt=0)
    ichimoku_base: int = Field(gt=0)
    ichimoku_span_b: int = Field(gt=0)
    ichimoku_displacement: int = Field(gt=0)
    supertrend_period: int = Field(gt=0)
    supertrend_multiplier: Positive

    @field_validator("ema_periods")
    @classmethod
    def _ema_periods_valid(cls, value: list[int]) -> list[int]:
        if not value or any(p <= 0 for p in value):
            raise ValueError(f"ema_periods must all be positive: {value}")
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate ema_periods: {value}")
        return sorted(value)

    @model_validator(mode="after")
    def _macd_ordering(self) -> TrendFeatureConfig:
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be shorter than macd_slow")
        return self


class VolatilityFeatureConfig(_Base):
    bollinger_period: int = Field(gt=1)
    bollinger_std: Positive
    atr_period: int = Field(gt=1)
    atr_percentile_window: int = Field(gt=1)
    keltner_period: int = Field(gt=1)
    keltner_atr_period: int = Field(gt=1)
    keltner_multiplier: Positive
    realized_vol_window: int = Field(gt=1)
    regime_window: int = Field(gt=1)
    regime_low_quantile: Fraction
    regime_high_quantile: Fraction

    @model_validator(mode="after")
    def _quantile_ordering(self) -> VolatilityFeatureConfig:
        if self.regime_low_quantile >= self.regime_high_quantile:
            raise ValueError("regime_low_quantile must be below regime_high_quantile")
        return self


class VolumeFeatureConfig(_Base):
    zscore_window: int = Field(gt=1)
    obv_slope_window: int = Field(gt=1)
    vwap_window: int = Field(gt=0)
    profile_window: int = Field(gt=1)
    profile_bins: int = Field(gt=1)
    value_area_fraction: Fraction
    depth_levels: int = Field(gt=0)


class StatisticalFeatureConfig(_Base):
    zscore_window: int = Field(gt=1)
    hurst_window: int = Field(gt=1)
    hurst_min_lag: int = Field(gt=0)
    hurst_max_lag: int = Field(gt=1)
    hurst_deadband_sds: NonNegative
    hurst_null_samples: int = Field(gt=1)
    hurst_null_seed: int
    half_life_window: int = Field(gt=1)
    half_life_max_bars: Positive
    adf_window: int = Field(gt=1)
    adf_stride: int = Field(gt=0)
    beta_window: int = Field(gt=1)
    beta_break_zscore: Positive
    beta_reference_symbol: str

    @model_validator(mode="after")
    def _lag_ordering(self) -> StatisticalFeatureConfig:
        if self.hurst_min_lag >= self.hurst_max_lag:
            raise ValueError("hurst_min_lag must be below hurst_max_lag")
        if self.hurst_max_lag >= self.hurst_window:
            raise ValueError("hurst_max_lag must be below hurst_window")
        return self


class MultiframeFeatureConfig(_Base):
    context_timeframes: list[str]
    conflict_downgrade: Fraction

    @field_validator("context_timeframes")
    @classmethod
    def _timeframes_valid(cls, value: list[str]) -> list[str]:
        for timeframe in value:
            interval_to_ms(timeframe)
        return sorted(value, key=interval_to_ms)


class FeaturesConfig(_Base):
    trend: TrendFeatureConfig
    volatility: VolatilityFeatureConfig
    volume: VolumeFeatureConfig
    statistical: StatisticalFeatureConfig
    multiframe: MultiframeFeatureConfig


class LoggingConfig(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    directory: Path
    filename: str
    max_bytes: int = Field(gt=0)
    backup_count: int = Field(ge=0)
    json_to_file: bool
    console: bool


class Config(_Base):
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    base_timeframe: str
    history_days: int = Field(gt=0)
    alert_channels: list[str]
    capital_model_usdt: Positive
    risk_per_trade_pct: Fraction
    run_mode: Literal["local", "docker"]
    data: DataConfig
    features: FeaturesConfig
    logging: LoggingConfig

    @field_validator("symbols")
    @classmethod
    def _symbols_upper(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate symbols: {value}")
        return [s.upper() for s in value]

    @field_validator("timeframes")
    @classmethod
    def _timeframes_valid(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError(f"duplicate timeframes: {value}")
        for timeframe in value:
            interval_to_ms(timeframe)  # raises IntervalError on anything unusable
        # Ascending by duration keeps multi-timeframe joins predictable.
        return sorted(value, key=interval_to_ms)

    @model_validator(mode="after")
    def _base_timeframe_present(self) -> Config:
        if self.base_timeframe not in self.timeframes:
            raise ValueError(
                f"base_timeframe {self.base_timeframe!r} is not in timeframes {self.timeframes}"
            )
        if self.data.mode == "synthetic":
            missing = set(self.symbols) - set(self.data.synthetic.initial_price)
            if missing:
                raise ValueError(f"synthetic.initial_price missing entries for {sorted(missing)}")
        return self

    def interval_ms(self, timeframe: str) -> int:
        return interval_to_ms(timeframe)

    @property
    def base_interval_ms(self) -> int:
        return interval_to_ms(self.base_timeframe)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path | str | None = None) -> Config:
    """Read and validate ``config.yaml``.

    The path may be overridden by the ``CSE_CONFIG`` environment variable, which
    is what the docker-compose setup uses.
    """
    resolved = (
        Path(path) if path is not None else Path(os.environ.get("CSE_CONFIG", DEFAULT_CONFIG_PATH))
    )
    if not resolved.is_file():
        raise FileNotFoundError(f"config file not found: {resolved}")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config file {resolved} did not parse to a mapping")
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached config. Tests should call ``load_config`` directly."""
    return load_config()
