"""Config validation: a bad config must fail loudly at startup, not silently later."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cse.config import Config, load_config


def build(raw: dict[str, Any], **overrides: Any) -> Config:
    data = copy.deepcopy(raw)
    data.update(overrides)
    return Config.model_validate(data)


def test_shipped_config_is_valid() -> None:
    config = load_config()

    assert config.symbols
    assert config.base_timeframe in config.timeframes


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_unknown_key_is_rejected(raw_config: dict[str, Any]) -> None:
    """A typo'd key must not silently become a no-op default."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(raw_config, unexpected_setting=True)


def test_base_timeframe_must_be_monitored(raw_config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="base_timeframe"):
        build(raw_config, base_timeframe="30m")


def test_timeframes_are_sorted_by_duration(raw_config: dict[str, Any]) -> None:
    """Ascending order keeps multi-timeframe joins predictable."""
    config = build(raw_config, timeframes=["4h", "1m", "1h", "15m", "5m"])

    assert config.timeframes == ["1m", "5m", "15m", "1h", "4h"]


def test_calendar_interval_is_rejected(raw_config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build(raw_config, timeframes=["1M"], base_timeframe="1M")


def test_duplicate_symbols_are_rejected(raw_config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        build(raw_config, symbols=["BTCUSDT", "BTCUSDT"])


def test_symbols_are_normalised_to_uppercase(raw_config: dict[str, Any]) -> None:
    config = build(raw_config, symbols=["btcusdt", "ethusdt"])

    assert config.symbols == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.parametrize("bad_risk", [0.0, 1.5, -0.1])
def test_risk_per_trade_must_be_a_sane_fraction(
    raw_config: dict[str, Any], bad_risk: float
) -> None:
    with pytest.raises(ValidationError):
        build(raw_config, risk_per_trade_pct=bad_risk)


def test_soft_threshold_must_sit_below_hard(raw_config: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_config)
    data["data"]["rate_limit"]["soft_threshold_pct"] = 0.99
    data["data"]["rate_limit"]["hard_threshold_pct"] = 0.5

    with pytest.raises(ValidationError, match="soft_threshold_pct"):
        Config.model_validate(data)


def test_klines_limit_cannot_exceed_the_binance_cap(raw_config: dict[str, Any]) -> None:
    data = copy.deepcopy(raw_config)
    data["data"]["backfill"]["klines_limit"] = 5000

    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_synthetic_mode_requires_a_price_for_every_symbol(
    raw_config: dict[str, Any],
) -> None:
    data = copy.deepcopy(raw_config)
    data["data"]["mode"] = "synthetic"
    data["symbols"] = ["BTCUSDT", "DOGEUSDT"]

    with pytest.raises(ValidationError, match="initial_price"):
        Config.model_validate(data)


def test_interval_helpers_agree_with_config(raw_config: dict[str, Any]) -> None:
    config = build(raw_config)

    assert config.interval_ms("15m") == 900_000
    assert config.base_interval_ms == config.interval_ms(config.base_timeframe)
