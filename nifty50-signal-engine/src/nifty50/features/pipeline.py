"""Assemble every feature for one symbol into a single frame.

Contract: the returned frame is indexed identically to the input bars, every
column is backward-looking, and warm-up is NaN rather than zero.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from nifty50.config import Config
from nifty50.domain import Timeframe
from nifty50.features import session as session_features
from nifty50.features import trend, volatility, volume
from nifty50.features.core import crossover_events
from nifty50.features.multiframe import align_higher_timeframe, conflict_score
from nifty50.frames import bar_index
from nifty50.trading_calendar.calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    """Everything the feature layer can consume for one symbol.

    ``index_close`` and ``vix_close`` are optional: without them the
    index-relative and market-risk-gate columns are simply absent, which is
    honest, rather than silently substituting the symbol's own series.
    """

    bars: pd.DataFrame
    timeframe: Timeframe
    index_close: pd.Series | None = None
    vix_close: pd.Series | None = None
    higher_timeframe_bars: dict[Timeframe, pd.DataFrame] | None = None


def compute_features(
    inputs: FeatureInputs, calendar: TradingCalendar, config: Config
) -> pd.DataFrame:
    """Compute the Phase 3 feature set for one symbol."""
    settings = config.features
    bars = inputs.bars
    if bars.empty:
        return pd.DataFrame(index=bars.index)

    high, low, close = bars["high"], bars["low"], bars["close"]
    volumes = bars["volume"].astype("float64")
    columns: list[pd.Series | pd.DataFrame] = []

    # --- session context -------------------------------------------------
    ordinal = session_features.session_ordinal(bars)
    slot = session_features.bar_of_session(bars, calendar, inputs.timeframe)
    columns += [
        slot,
        session_features.is_session_open_bar(bars),
        session_features.overnight_gap(bars),
    ]

    # --- volatility first: several other features are denominated in ATR --
    atr_values = volatility.atr(high, low, close, settings["atr_period"])
    bollinger_frame = volatility.bollinger(
        close, settings["bollinger_window"], settings["bollinger_std"]
    )
    keltner_frame = volatility.keltner(
        high,
        low,
        close,
        ema_period=settings["keltner_ema_period"],
        atr_period=settings["keltner_atr_period"],
        multiplier=settings["keltner_multiplier"],
    )
    realized = volatility.realized_volatility(
        close,
        settings["realized_vol_window"],
        bars_per_year=_bars_per_year(inputs.timeframe, calendar, settings),
    )
    columns += [
        atr_values,
        volatility.atr_percentile(atr_values, settings["percentile_window"]),
        bollinger_frame,
        keltner_frame,
        volatility.squeeze(bollinger_frame, keltner_frame),
        realized,
        volatility.volatility_regime(
            realized,
            settings["percentile_window"],
            low_quantile=settings["regime_low_quantile"],
            high_quantile=settings["regime_high_quantile"],
        ),
    ]

    # --- trend / momentum ------------------------------------------------
    periods = tuple(settings["ema_periods"])
    ribbon = trend.ema_ribbon(close, periods)
    macd_frame = trend.macd(
        close, settings["macd_fast"], settings["macd_slow"], settings["macd_signal"]
    )
    rsi_values = trend.rsi(close, settings["rsi_period"])
    ichimoku_frame = trend.ichimoku(
        high,
        low,
        tenkan=settings["ichimoku_tenkan"],
        kijun=settings["ichimoku_kijun"],
        senkou_b=settings["ichimoku_senkou_b"],
        displacement=settings["ichimoku_displacement"],
    )
    fast_ema, slow_ema = f"ema_{periods[0]}", f"ema_{periods[1]}"
    columns += [
        ribbon,
        trend.ema_stack_score(ribbon, periods),
        trend.trend_state(ribbon[fast_ema], ribbon[slow_ema]),
        crossover_events(ribbon[fast_ema], ribbon[slow_ema]).rename("ema_cross_event"),
        macd_frame,
        rsi_values,
        trend.rsi_divergence(
            close,
            rsi_values,
            left=settings["pivot_left"],
            right=settings["pivot_right"],
        ),
        trend.adx(high, low, close, settings["adx_period"]),
        trend.supertrend(high, low, close, atr_values, settings["supertrend_multiplier"]),
        ichimoku_frame,
        trend.cloud_position(
            close,
            ichimoku_frame["ichimoku_senkou_a"],
            ichimoku_frame["ichimoku_senkou_b"],
        ),
        trend.rate_of_change(close, settings["roc_period"]),
    ]

    # --- volume / flow ---------------------------------------------------
    obv_values = volume.obv(close, volumes)
    vwap = volume.session_vwap(high, low, close, volumes, ordinal)
    first_high = session_features.session_window_extreme(
        bars, calendar, minutes=settings["opening_range_minutes"], column="high", how="max"
    )
    first_low = session_features.session_window_extreme(
        bars, calendar, minutes=settings["opening_range_minutes"], column="low", how="min"
    )
    columns += [
        volume.session_matched_volume_zscore(
            volumes, slot, lookback_sessions=settings["volume_lookback_sessions"]
        ),
        volume.relative_volume(
            volumes, slot, lookback_sessions=settings["volume_lookback_sessions"]
        ),
        obv_values,
        volume.obv_slope(obv_values, settings["obv_slope_window"]),
        vwap,
        volume.distance_from_vwap_in_atr(close, vwap, atr_values),
        first_high,
        first_low,
        volume.breakout_state(close, first_high, first_low),
    ]

    # --- index-relative and market risk ----------------------------------
    if inputs.index_close is not None:
        columns.append(trend.relative_strength(close, inputs.index_close, settings["roc_period"]))
    if inputs.vix_close is not None:
        columns.append(
            volatility.vix_gate(
                inputs.vix_close, bars.index, panic_level=settings["vix_panic_level"]
            )
        )

    features = pd.concat(columns, axis=1)

    # --- higher-timeframe context ----------------------------------------
    for higher_tf, higher_bars in (inputs.higher_timeframe_bars or {}).items():
        higher = compute_features(
            FeatureInputs(bars=higher_bars, timeframe=higher_tf),
            calendar,
            config,
        )
        keep = [c for c in settings["higher_timeframe_columns"] if c in higher.columns]
        if not keep:
            continue
        aligned = align_higher_timeframe(bar_index(bars), higher[keep], calendar, higher_tf)
        features = pd.concat([features, aligned], axis=1)
        higher_state = f"trend_state_{higher_tf.value}"
        if "trend_state" in features.columns and higher_state in features.columns:
            features[f"mtf_conflict_{higher_tf.value}"] = conflict_score(
                features["trend_state"], features[higher_state]
            )

    return features


def _bars_per_year(
    timeframe: Timeframe, calendar: TradingCalendar, settings: dict[str, object]
) -> float:
    """Annualisation factor for the timeframe.

    Derived from the calendar's actual session length rather than assumed, so a
    375-minute NSE session is not annualised as if it were a 24-hour crypto tape.
    """
    sessions = float(settings["trading_days_per_year"])  # type: ignore[arg-type]
    if timeframe is Timeframe.D1:
        return sessions
    import datetime as dt

    reference = calendar.next_trading_day(dt.date(2025, 1, 1))
    return sessions * calendar.bars_per_session(reference, timeframe)
