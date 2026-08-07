"""Assemble every feature for one symbol into a single frame.

Contract: the returned frame is indexed identically to the input bars, every
column is backward-looking, and warm-up is NaN rather than zero.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from nifty50.config import Config
from nifty50.domain import Timeframe
from nifty50.features import flow as flow_features
from nifty50.features import session as session_features
from nifty50.features import statistical, trend, volatility, volume
from nifty50.features.core import crossover_events
from nifty50.features.multiframe import align_higher_timeframe, conflict_score
from nifty50.frames import bar_index
from nifty50.trading_calendar.calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    """Everything the feature layer can consume for one symbol.

    Every field past ``timeframe`` is optional, and absence means the
    corresponding columns are simply missing from the output. That is the
    honest behaviour: a delivery-percentage column filled with zeros because
    the bhavcopy was not loaded looks exactly like a real column of low
    delivery, and would be trained on as if it were.

    The daily inputs — ``delivery_pct``, ``fii_net_crore``, ``dii_net_crore``
    — are indexed by *date* and are published after the close of the session
    they describe. :mod:`nifty50.features.flow` applies the publication lag;
    nothing here should pre-shift them.
    """

    bars: pd.DataFrame
    timeframe: Timeframe
    index_close: pd.Series | None = None
    vix_close: pd.Series | None = None
    higher_timeframe_bars: dict[Timeframe, pd.DataFrame] | None = None

    # --- India-specific daily inputs (see nifty50.features.flow) ------------
    delivery_pct: pd.Series | None = None
    fii_net_crore: pd.Series | None = None
    dii_net_crore: pd.Series | None = None
    fno_ban_dates: frozenset[dt.date] = field(default_factory=frozenset)
    index_event_dates: frozenset[dt.date] = field(default_factory=frozenset)


def compute_features(
    inputs: FeatureInputs,
    calendar: TradingCalendar,
    config: Config,
    *,
    include_statistical: bool = True,
) -> pd.DataFrame:
    """Compute the feature set for one symbol.

    ``include_statistical`` exists for the higher-timeframe recursion. The
    long-memory estimators use rolling regressions and are an order of
    magnitude more expensive than the rest of the layer; computing them on a
    higher timeframe whose columns are then discarded is pure waste. The
    recursion turns them on only when a statistical column is actually
    requested.
    """
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

    # --- statistical structure -------------------------------------------
    # Answers "is direction the right question here" before the trend columns
    # answer "which direction". Windows are long by necessity; on the base 15m
    # timeframe these warm up over several sessions.
    memory_window = int(settings["memory_window"])
    returns = statistical.log_returns(close)
    if include_statistical:
        columns += [
            returns,
            statistical.hurst_exponent(close, memory_window),
            statistical.variance_ratio(close, memory_window, lag=settings["variance_ratio_lag"]),
            statistical.variance_ratio_zstat(
                close, memory_window, lag=settings["variance_ratio_lag"]
            ),
            statistical.autocorrelation(returns, memory_window, lag=1),
            statistical.mean_reversion_half_life(close, memory_window),
            statistical.distance_from_mean_in_sigma(close, settings["reversion_window"]),
            statistical.distribution_shape(returns, memory_window),
        ]

    # --- index-relative and market risk ----------------------------------
    if inputs.index_close is not None:
        columns.append(trend.relative_strength(close, inputs.index_close, settings["roc_period"]))
        if include_statistical:
            index_returns = statistical.log_returns(inputs.index_close.reindex(bars.index))
            regression = statistical.rolling_beta(returns, index_returns, memory_window)
            residuals = statistical.residual_return(
                returns, index_returns, regression["beta"], regression["alpha"]
            )
            columns += [
                regression,
                residuals,
                statistical.residual_zscore(residuals, settings["reversion_window"]),
            ]
    if inputs.vix_close is not None:
        columns.append(
            volatility.vix_gate(
                inputs.vix_close, bars.index, panic_level=settings["vix_panic_level"]
            )
        )

    # --- India-specific flow ---------------------------------------------
    columns += _flow_columns(inputs, bars, calendar, settings)

    features = pd.concat(columns, axis=1)

    # --- higher-timeframe context ----------------------------------------
    requested = list(settings["higher_timeframe_columns"])
    higher_needs_statistical = any(name in _STATISTICAL_COLUMNS for name in requested)
    for higher_tf, higher_bars in (inputs.higher_timeframe_bars or {}).items():
        higher = compute_features(
            FeatureInputs(bars=higher_bars, timeframe=higher_tf),
            calendar,
            config,
            include_statistical=higher_needs_statistical,
        )
        keep = [c for c in requested if c in higher.columns]
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


# Columns produced by the statistical block, so the higher-timeframe recursion
# can tell whether it is worth paying for them.
_STATISTICAL_COLUMNS: frozenset[str] = frozenset(
    {
        "log_return",
        "hurst",
        "autocorr_1",
        "half_life_bars",
        "distance_from_mean_sigma",
        "return_skew",
        "return_excess_kurtosis",
        "vol_of_vol",
        "beta",
        "alpha",
        "r_squared",
        "index_correlation",
        "beta_instability",
        "residual_return",
        "residual_zscore",
    }
)


def _flow_columns(
    inputs: FeatureInputs,
    bars: pd.DataFrame,
    calendar: TradingCalendar,
    settings: dict[str, object],
) -> list[pd.Series | pd.DataFrame]:
    """India-specific flow columns, each present only if its input was supplied.

    The publication lags are applied inside :mod:`nifty50.features.flow` and
    are not overridable from here. Making them a config knob would invite
    someone to set them to zero to "improve" a backtest, which is precisely
    the mistake this layer exists to make impossible.
    """
    columns: list[pd.Series | pd.DataFrame] = []
    sessions = int(str(settings["flow_window_sessions"]))

    if inputs.delivery_pct is not None:
        columns.append(
            flow_features.delivery_features(
                inputs.delivery_pct, bars, calendar, window=sessions
            )
        )
    if inputs.fii_net_crore is not None and inputs.dii_net_crore is not None:
        columns.append(
            flow_features.participant_flow_features(
                inputs.fii_net_crore, inputs.dii_net_crore, bars, calendar, window=sessions
            )
        )
    if inputs.fno_ban_dates:
        columns.append(flow_features.fno_ban_flag(inputs.fno_ban_dates, bars))
    if inputs.index_event_dates:
        columns.append(
            flow_features.index_event_proximity(
                inputs.index_event_dates,
                bars,
                lead_sessions=int(str(settings["index_event_lead_sessions"])),
            )
        )

    # Circuit bands need no external file — previous close is in the bars — so
    # unlike the rest of this block they are always available.
    columns.append(
        flow_features.circuit_band_state(
            bars["high"],
            bars["low"],
            bars["close"],
            _previous_session_close(bars),
            band_pct=float(str(settings["circuit_band_pct"])),
        )
    )
    return columns


def _previous_session_close(bars: pd.DataFrame) -> pd.Series:
    """Prior session's closing price, broadcast across every bar of a session.

    Grouped-and-shifted at the *session* level, not the bar level. A bar-level
    shift would put a bar from earlier the same day here, and a
    ``transform("last")`` would put the session's own close — the price the
    band is supposed to be predicting — into every bar of that session.
    """
    dates = session_features.session_date(bars)
    session_close = bars["close"].groupby(dates).last()
    previous = session_close.shift(1)
    return pd.Series(
        dates.map(previous).to_numpy(dtype="float64"),
        index=bars.index,
        name="previous_session_close",
    )


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
