"""Session-aware helpers.

These exist so the rest of the feature layer never has to assume that "one bar
back" means "fifteen minutes back". On NSE the bar before 09:15 is 17.75 hours
earlier, and any accumulator that ignores that — VWAP, OBV, first-hour range,
the volume baseline — silently carries yesterday's state into today.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from nifty50.domain import Timeframe
from nifty50.frames import bar_index, ist_index
from nifty50.trading_calendar.calendar import TradingCalendar


def session_date(frame: pd.DataFrame) -> pd.Series:
    """The exchange date each bar belongs to."""
    return pd.Series(ist_index(frame).date, index=frame.index, name="session_date")


def session_ordinal(frame: pd.DataFrame) -> pd.Series:
    """0-based session counter: same integer for every bar of one session.

    Used to group intraday accumulators so they reset at the open.
    """
    dates = session_date(frame)
    codes, _ = pd.factorize(dates, sort=True)
    return pd.Series(codes, index=frame.index, dtype="int64", name="session_ordinal")


def bar_of_session(
    frame: pd.DataFrame, calendar: TradingCalendar, timeframe: Timeframe
) -> pd.Series:
    """0-based position of each bar within its session.

    This is the key to session-matched comparisons: NSE intraday volume is
    strongly U-shaped, so 10:30 volume is only meaningful against *prior 10:30s*,
    never against a flat daily average. That comparison needs a stable index of
    "which slot of the day is this", which is what this returns.
    """
    index = bar_index(frame)
    positions = np.full(len(index), -1, dtype="int64")
    dates = np.asarray(ist_index(frame).date)
    for day in pd.unique(dates):
        slots = {ts: i for i, ts in enumerate(calendar.bar_starts(day, timeframe))}
        mask = dates == day
        positions[mask] = [slots.get(ts, -1) for ts in index[mask]]
    return pd.Series(positions, index=frame.index, dtype="int64", name="bar_of_session")


def is_session_open_bar(frame: pd.DataFrame) -> pd.Series:
    """True on the first bar of each session."""
    ordinal = session_ordinal(frame)
    return pd.Series(
        ordinal.ne(ordinal.shift(1)).to_numpy(),
        index=frame.index,
        dtype="bool",
        name="is_session_open_bar",
    )


def overnight_gap(frame: pd.DataFrame) -> pd.Series:
    """Open-vs-previous-session-close return, on session-open bars only.

    NaN everywhere else, deliberately. The overnight move is a discrete event
    between two sessions, not a bar-to-bar price change, and a feature that
    smears it across the day would let intraday logic react to it repeatedly.
    """
    ordinal = session_ordinal(frame)
    session_last_close = frame.groupby(ordinal)["close"].last()
    previous_close = ordinal.map(session_last_close.shift(1))
    gap = (frame["open"] / previous_close) - 1.0
    return gap.where(is_session_open_bar(frame)).rename("overnight_gap")


def session_cumulative(values: pd.Series, ordinal: pd.Series) -> pd.Series:
    """Cumulative sum that restarts at every session boundary."""
    return values.groupby(ordinal).cumsum()


def session_window_extreme(
    frame: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    minutes: int,
    column: str,
    how: str,
) -> pd.Series:
    """High or low of the first ``minutes`` of each session.

    Availability is the whole point: the value is NaN until the window has
    *closed*. A first-hour range known at 09:20 would be a look-ahead, so bars
    inside the window see nothing and the level appears on the first bar after
    it ends.
    """
    if how not in {"max", "min"}:
        raise ValueError("how must be 'max' or 'min'")
    index = bar_index(frame)
    stamps = pd.Series(index, index=frame.index)
    dates = np.asarray(ist_index(frame).date)
    result = pd.Series(np.nan, index=frame.index, dtype="float64")

    for day in pd.unique(dates):
        schedule = calendar.schedule(day)
        window_end = schedule.continuous.start + dt.timedelta(minutes=minutes)
        in_day = dates == day
        in_window = in_day & (stamps < window_end).to_numpy()
        if not in_window.any():
            continue
        values = frame.loc[in_window, column]
        level = float(values.max() if how == "max" else values.min())
        # Only bars at or after the window's end may see it.
        result[in_day & (stamps >= window_end).to_numpy()] = level
    return result.rename(f"first_{minutes}m_{how}")


def sessions_available(frame: pd.DataFrame) -> int:
    """Number of distinct sessions in the frame; a warm-up sanity check."""
    return int(session_ordinal(frame).nunique())
