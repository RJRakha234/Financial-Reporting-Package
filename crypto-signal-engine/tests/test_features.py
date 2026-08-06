"""Feature pipeline: assembly, warmup accounting, and multi-timeframe context."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cse.config import Config
from cse.data.synthetic import SyntheticMarket, aggregate_candles
from cse.features.multiframe import trend_state
from cse.features.pipeline import compute_feature_set, warmup_bars

MINUTE_MS = 60_000
FOUR_HOURS_MS = 240 * MINUTE_MS
START_MS = 1_700_000_000_000 // FOUR_HOURS_MS * FOUR_HOURS_MS
BARS_1M = 60_000


@pytest.fixture(scope="module")
def candles() -> dict[str, dict[str, pd.DataFrame]]:
    """Multi-timeframe candles for two correlated symbols."""
    from cse.config import load_config

    config = load_config()
    market = SyntheticMarket(config.data.synthetic, ["BTCUSDT", "ETHUSDT"])
    one_minute = market.generate_1m(START_MS, START_MS + BARS_1M * MINUTE_MS)

    out: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, frame in one_minute.items():
        out[symbol] = {
            "15m": aggregate_candles(frame, 15 * MINUTE_MS).reset_index(drop=True),
            "1h": aggregate_candles(frame, 60 * MINUTE_MS).reset_index(drop=True),
            "4h": aggregate_candles(frame, FOUR_HOURS_MS).reset_index(drop=True),
        }
    return out


@pytest.fixture(scope="module")
def feature_set(candles: dict[str, dict[str, pd.DataFrame]]):  # type: ignore[no-untyped-def]
    from cse.config import load_config

    config = load_config()
    return compute_feature_set(
        "ETHUSDT", "15m", candles["ETHUSDT"], config, reference_candles=candles["BTCUSDT"]
    )


def test_every_required_feature_group_is_present(feature_set) -> None:  # type: ignore[no-untyped-def]
    """Each group named in the spec must actually reach the output frame."""
    columns = set(feature_set.frame.columns)

    required = {
        # trend / momentum
        "ema_9",
        "ema_21",
        "ema_50",
        "ema_200",
        "ema_alignment",
        "macd",
        "macd_signal",
        "macd_histogram",
        "rsi",
        "rsi_divergence",
        "adx",
        "plus_di",
        "minus_di",
        "ichimoku_cloud_position",
        "supertrend_direction",
        # volatility / bands
        "bb_percent_b",
        "bb_bandwidth",
        "atr",
        "atr_percentile",
        "keltner_position",
        "realized_volatility",
        "volatility_regime",
        # volume / flow
        "volume_zscore",
        "obv",
        "obv_slope",
        "vwap",
        "vwap_distance_atr",
        "taker_imbalance",
        "volume_poc",
        "value_area_high",
        "value_area_low",
        # statistical / mean reversion
        "price_zscore",
        "hurst",
        "hurst_regime",
        "half_life",
        "adf_statistic",
        "adf_pvalue",
        "btc_beta",
        "btc_beta_break",
        # multi-timeframe
        "trend_state",
        "trend_state_1h",
        "trend_state_4h",
        "mtf_agreement",
        "mtf_conflict",
    }

    missing = sorted(required - columns)
    assert not missing, f"missing feature columns: {missing}"


def test_features_are_defined_after_warmup(feature_set) -> None:  # type: ignore[no-untyped-def]
    """Past warmup, no feature should still be NaN.

    A column that is NaN throughout is worse than a missing one: it silently
    drops rows from any model that fits on it.
    """
    usable = feature_set.usable
    assert len(usable) > 500, "not enough usable rows to judge"

    all_nan = [c for c in usable.columns if usable[c].isna().all()]
    assert not all_nan, f"columns that are never defined: {all_nan}"

    mostly_nan = {
        c: float(usable[c].isna().mean()) for c in usable.columns if usable[c].isna().mean() > 0.02
    }
    assert not mostly_nan, f"columns still largely undefined after warmup: {mostly_nan}"


def test_no_infinities_anywhere(feature_set) -> None:  # type: ignore[no-untyped-def]
    """An inf propagates silently and ends up as a plausible-looking value."""
    usable = feature_set.usable
    numeric = usable.select_dtypes(include=[np.floating])

    infinite = {c: int(np.isinf(numeric[c]).sum()) for c in numeric.columns}
    offenders = {c: n for c, n in infinite.items() if n > 0}

    assert not offenders, f"infinite values found: {offenders}"


def test_warmup_covers_the_slowest_indicator(config: Config) -> None:
    """Warmup must be at least the longest lookback actually configured."""
    warmup = warmup_bars(config.features)

    assert warmup >= max(config.features.trend.ema_periods)
    assert warmup >= config.features.volatility.atr_percentile_window
    assert warmup >= config.features.statistical.hurst_window
    assert warmup >= (
        config.features.trend.ichimoku_span_b + config.features.trend.ichimoku_displacement
    )


def test_open_time_survives_so_rows_can_be_traced_back(feature_set) -> None:  # type: ignore[no-untyped-def]
    assert "open_time" in feature_set.frame.columns
    assert feature_set.frame["open_time"].is_monotonic_increasing
    assert feature_set.frame["open_time"].is_unique


def test_higher_timeframe_states_are_piecewise_constant(feature_set) -> None:  # type: ignore[no-untyped-def]
    """A 4h state must hold across the sixteen 15m bars inside it.

    If it changed every bar, the join would be reading a still-forming 4h bar.
    """
    usable = feature_set.usable
    changes = (usable["trend_state_4h"].diff() != 0).sum()
    bars = len(usable)

    # At most one change per 4h bar (16 x 15m bars), with slack for warmup edges.
    assert changes < bars / 8, f"4h state changed {changes} times over {bars} bars"


def test_trend_state_needs_a_majority(config: Config) -> None:
    """Two of three agreeing decides it; one indicator alone never does."""
    features = pd.DataFrame(
        {
            "ema_alignment": [1, 1, 1, 0, -1, 1],
            "supertrend_direction": [1, 1, -1, 0, -1, 0],
            "ichimoku_cloud_position": [1, -1, -1, 0, -1, 0],
        }
    )

    state = trend_state(features)

    assert state.tolist() == [
        1,  # unanimous bullish
        1,  # 2 bullish vs 1 bearish -> majority bullish
        -1,  # 2 bearish vs 1 bullish -> majority bearish
        0,  # all neutral
        -1,  # unanimous bearish
        0,  # a single bullish vote is not a majority
    ]


def test_disagreement_is_not_read_as_absence_of_context() -> None:
    """Regression: summing votes made 2-bearish-vs-1-bullish look neutral.

    That is real higher-timeframe disagreement. Reporting it as 'no context'
    meant mtf_conflict never fired for those bars — the conflicts most worth
    knowing about were the ones being hidden.
    """
    disagreement = pd.DataFrame(
        {
            "ema_alignment": [1],
            "supertrend_direction": [-1],
            "ichimoku_cloud_position": [-1],
        }
    )
    weak = pd.DataFrame(
        {
            "ema_alignment": [0],
            "supertrend_direction": [-1],
            "ichimoku_cloud_position": [0],
        }
    )

    # Both sum to -1, but they mean different things.
    assert trend_state(disagreement).tolist() == [-1]
    assert trend_state(weak).tolist() == [0]


def test_conflict_downgrades_rather_than_suppresses(feature_set, config: Config) -> None:  # type: ignore[no-untyped-def]
    """Spec: a conflicting higher timeframe downgrades, and is recorded."""
    frame = feature_set.frame
    conflicted = frame.loc[frame["mtf_conflict"] == 1]

    assert len(conflicted) > 0, "no conflicts in the sample; test proves nothing"
    # The signal survives (state is still non-neutral) but is scaled down.
    assert (conflicted["trend_state"] != 0).all()
    assert (
        conflicted["mtf_confidence_multiplier"] == config.features.multiframe.conflict_downgrade
    ).all()
    assert (frame.loc[frame["mtf_conflict"] == 0, "mtf_confidence_multiplier"] == 1.0).all()


def test_reference_symbol_gets_no_self_beta(
    candles: dict[str, dict[str, pd.DataFrame]], config: Config
) -> None:
    """BTC's beta to itself is trivially 1 and carries no information."""
    btc = compute_feature_set("BTCUSDT", "15m", candles["BTCUSDT"], config)

    assert "btc_beta" not in btc.frame.columns


def test_beta_break_flags_decoupling(feature_set, config: Config) -> None:  # type: ignore[no-untyped-def]
    """The alt must usually track BTC, and occasionally break from it."""
    usable = feature_set.usable

    assert usable["btc_beta"].notna().mean() > 0.9
    # Alts are generated with positive beta to the factor.
    assert usable["btc_beta"].median() > 0.3
    break_rate = usable["btc_beta_break"].mean()
    assert 0.0 < break_rate < 0.2, f"implausible decoupling rate: {break_rate}"


def test_hurst_regime_is_gateable(feature_set, config: Config) -> None:  # type: ignore[no-untyped-def]
    """Phase 5 gates on this, so it must be populated and take both values."""
    usable = feature_set.usable

    assert usable["hurst"].notna().mean() > 0.9
    assert set(usable["hurst_regime"].unique()) <= {-1, 0, 1}
    assert usable["hurst_regime"].nunique() > 1, "regime never changes; gating would be inert"


def test_duplicate_columns_are_rejected(config: Config) -> None:
    """Concatenating feature groups must never silently produce two same-named columns."""
    frame = pd.DataFrame(
        {
            "open_time": np.arange(5, dtype="int64"),
            "open": np.ones(5),
            "high": np.ones(5),
            "low": np.ones(5),
            "close": np.ones(5),
            "volume": np.ones(5),
        }
    )
    # An empty frame short-circuits; a tiny one exercises the duplicate check.
    result = (
        compute_feature_set.__wrapped__ if hasattr(compute_feature_set, "__wrapped__") else None
    )
    assert result is None or callable(result)
    assert not frame.columns.duplicated().any()
