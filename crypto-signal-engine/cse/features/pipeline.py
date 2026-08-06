"""Assembles every feature group into one frame per symbol/timeframe.

The single entry point downstream layers use. Guarantees:

* the output is indexed positionally and carries ``open_time``, so a feature row
  can always be tied back to the bar it describes;
* no column is computed from data after its bar (see :mod:`tests.test_lookahead`);
* a ``warmup_bars`` count is reported, because the longest indicator window
  determines how much of the head is NaN and therefore unusable. Silently
  training or backtesting across that head is a real and common way to get
  nonsense results out of an otherwise correct pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from cse.config import Config, FeaturesConfig
from cse.data.schema import CANDLE_COLUMNS
from cse.features import multiframe, statistical, trend, volatility, volume
from cse.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class FeatureSet:
    """Computed features plus the metadata needed to use them safely."""

    symbol: str
    timeframe: str
    frame: pd.DataFrame
    warmup_bars: int

    @property
    def usable(self) -> pd.DataFrame:
        """Rows past the warmup period, where every feature is defined."""
        return self.frame.iloc[self.warmup_bars :]

    def feature_columns(self) -> list[str]:
        return [c for c in self.frame.columns if c not in CANDLE_COLUMNS]


def warmup_bars(config: FeaturesConfig) -> int:
    """Bars needed before every feature is defined.

    The maximum of every lookback in play. Ichimoku is the usual winner: its
    Senkou span B needs ``span_b`` bars and is then displaced a further
    ``displacement`` bars forward.
    """
    candidates = [
        max(config.trend.ema_periods),
        config.trend.macd_slow + config.trend.macd_signal,
        config.trend.rsi_period + config.trend.rsi_divergence_lookback,
        config.trend.adx_period * 2,
        config.trend.ichimoku_span_b + config.trend.ichimoku_displacement,
        config.trend.supertrend_period,
        config.volatility.bollinger_period,
        config.volatility.atr_percentile_window,
        config.volatility.realized_vol_window + config.volatility.regime_window,
        config.volume.zscore_window,
        config.volume.profile_window,
        config.statistical.hurst_window,
        config.statistical.half_life_window,
        config.statistical.adf_window,
        config.statistical.beta_window,
    ]
    return int(max(candidates))


def compute_single_timeframe(
    frame: pd.DataFrame,
    config: FeaturesConfig,
    *,
    reference_close: pd.Series[float] | None = None,
) -> pd.DataFrame:
    """All non-multi-timeframe features for one candle frame."""
    if frame.empty:
        return pd.DataFrame(index=frame.index)

    working = frame.reset_index(drop=True)
    trend_features = trend.compute(working, config.trend)
    volatility_features = volatility.compute(working, config.volatility)
    volume_features = volume.compute(working, config.volume, atr_values=volatility_features["atr"])
    statistical_features = statistical.compute(
        working,
        config.statistical,
        reference_close=(
            reference_close.reset_index(drop=True) if reference_close is not None else None
        ),
    )

    out = pd.concat(
        [
            working[["open_time", "open", "high", "low", "close", "volume"]],
            trend_features,
            volatility_features,
            volume_features,
            statistical_features,
        ],
        axis=1,
    )
    duplicated = out.columns.duplicated()
    if duplicated.any():
        raise ValueError(f"duplicate feature columns: {sorted(out.columns[duplicated])}")
    return out


def compute_feature_set(
    symbol: str,
    timeframe: str,
    candles: dict[str, pd.DataFrame],
    config: Config,
    *,
    reference_candles: dict[str, pd.DataFrame] | None = None,
) -> FeatureSet:
    """Full feature set for one symbol at its base timeframe.

    ``candles`` maps timeframe -> candle frame for this symbol, and must include
    ``timeframe`` itself plus every configured context timeframe.
    ``reference_candles`` is the same mapping for the market-factor symbol (BTC),
    used for the beta-break feature; pass ``None`` for the reference itself.
    """
    features_config = config.features
    if timeframe not in candles:
        raise KeyError(f"candles missing base timeframe {timeframe!r}")

    base_frame = candles[timeframe].reset_index(drop=True)

    reference_close: pd.Series[float] | None = None
    if reference_candles is not None and timeframe in reference_candles:
        reference = reference_candles[timeframe].reset_index(drop=True)
        # Align the reference to this symbol's bars by open_time; a positional
        # join would silently pair mismatched timestamps if either series has a
        # gap the other does not.
        aligned = base_frame[["open_time"]].merge(
            reference[["open_time", "close"]], on="open_time", how="left"
        )
        reference_close = aligned["close"]

    base_features = compute_single_timeframe(
        base_frame, features_config, reference_close=reference_close
    )

    higher: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for context_timeframe in features_config.multiframe.context_timeframes:
        if context_timeframe not in candles:
            _log.warning(
                "features.missing_context_timeframe",
                symbol=symbol,
                timeframe=context_timeframe,
                note="multi-timeframe confirmation will treat it as neutral",
            )
            continue
        context_frame = candles[context_timeframe].reset_index(drop=True)
        higher[context_timeframe] = (
            context_frame,
            trend.compute(context_frame, features_config.trend),
        )

    mtf = multiframe.compute(base_features, base_frame, higher, features_config.multiframe)
    out = pd.concat([base_features, mtf], axis=1)

    warmup = warmup_bars(features_config)
    _log.info(
        "features.computed",
        symbol=symbol,
        timeframe=timeframe,
        rows=len(out),
        columns=len(out.columns),
        warmup_bars=warmup,
        usable_rows=max(0, len(out) - warmup),
    )
    return FeatureSet(symbol=symbol, timeframe=timeframe, frame=out, warmup_bars=warmup)
