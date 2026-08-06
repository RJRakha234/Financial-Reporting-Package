"""Volume and flow features.

The one that matters most here is the **session-matched volume z-score**. NSE
intraday volume is strongly U-shaped: heavy at the open, thin through midday,
heavy into the close. Scoring 10:30 volume against a flat trailing average
therefore reports "unusually quiet" every single midday and "unusually busy"
every single open — a feature that looks informative and is measuring nothing
but the time of day. The fix is to compare each slot against the *same slot* in
prior sessions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nifty50.features.core import rolling_slope
from nifty50.features.session import session_cumulative


def session_matched_volume_zscore(
    volume: pd.Series,
    bar_of_session: pd.Series,
    *,
    lookback_sessions: int,
) -> pd.Series:
    """Volume z-score against the same slot of prior sessions.

    Strictly prior: the trailing window is shifted by one session so the current
    bar is never part of the distribution it is being scored against.
    """
    scores = pd.Series(np.nan, index=volume.index, dtype="float64")
    values = volume.astype("float64")

    for slot in sorted(set(bar_of_session.to_numpy().tolist())):
        if slot < 0:  # bar not on the calendar's grid for that session
            continue
        mask = (bar_of_session == slot).to_numpy()
        slot_values = values[mask]
        history = slot_values.shift(1)  # exclude today's own reading
        rolling = history.rolling(window=lookback_sessions, min_periods=lookback_sessions)
        deviation = rolling.std(ddof=0)
        scores[mask] = ((slot_values - rolling.mean()) / deviation.replace(0.0, np.nan)).to_numpy()
    named: pd.Series = scores.rename("volume_zscore_session_matched")
    return named


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume: signed volume accumulated over the whole series."""
    direction = np.sign(close.diff()).fillna(0.0)
    accumulated: pd.Series = (direction * volume).cumsum().rename("obv")
    return accumulated


def obv_slope(obv_values: pd.Series, window: int) -> pd.Series:
    """Trend of OBV. The level is arbitrary; only its slope carries information."""
    return rolling_slope(obv_values, window).rename("obv_slope")


def session_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    session_ordinal: pd.Series,
) -> pd.Series:
    """Volume-weighted average price, reset at every session open.

    A VWAP that runs across days is not the VWAP anyone trades against — the
    intraday reference resets each morning, and carrying yesterday's turnover
    into today's anchor makes the level drift meaninglessly.
    """
    typical_price = (high + low + close) / 3.0
    turnover = session_cumulative(typical_price * volume, session_ordinal)
    traded = session_cumulative(volume, session_ordinal)
    return (turnover / traded.replace(0.0, np.nan)).rename("session_vwap")


def distance_from_vwap_in_atr(
    close: pd.Series, vwap: pd.Series, atr_values: pd.Series
) -> pd.Series:
    """(close - VWAP) measured in ATR units.

    Expressed in ATR rather than rupees or percent so the same threshold means
    the same thing for a quiet FMCG name and a volatile metals name.
    """
    return ((close - vwap) / atr_values.replace(0.0, np.nan)).rename("vwap_distance_atr")


def order_book_imbalance(
    total_buy_quantity: pd.Series, total_sell_quantity: pd.Series
) -> pd.Series:
    """(bids - asks) / (bids + asks), in [-1, 1].

    Depth is a live-only field: it is not in any historical bar feed, so this is
    NaN throughout a backtest by construction. A backtest that appears to use
    order-book imbalance is using something it invented.
    """
    total = (total_buy_quantity + total_sell_quantity).replace(0.0, np.nan)
    return ((total_buy_quantity - total_sell_quantity) / total).rename("book_imbalance")


def breakout_state(close: pd.Series, upper: pd.Series, lower: pd.Series) -> pd.Series:
    """+1 above the upper level, -1 below the lower, 0 between or unknown."""
    state = pd.Series(0, index=close.index, dtype="int64")
    state[close > upper] = 1
    state[close < lower] = -1
    state[upper.isna() | lower.isna()] = 0
    return state.rename("range_breakout_state")


def relative_volume(
    volume: pd.Series, bar_of_session: pd.Series, *, lookback_sessions: int
) -> pd.Series:
    """Current volume as a multiple of the same slot's trailing median.

    A companion to the z-score that survives fat tails: one 20x print distorts a
    mean and standard deviation badly, and barely moves a median.
    """
    ratios = pd.Series(np.nan, index=volume.index, dtype="float64")
    values = volume.astype("float64")
    for slot in sorted(set(bar_of_session.to_numpy().tolist())):
        if slot < 0:
            continue
        mask = (bar_of_session == slot).to_numpy()
        slot_values = values[mask]
        median = (
            slot_values.shift(1)
            .rolling(window=lookback_sessions, min_periods=lookback_sessions)
            .median()
        )
        ratios[mask] = (slot_values / median.replace(0.0, np.nan)).to_numpy()
    named: pd.Series = ratios.rename("relative_volume")
    return named
