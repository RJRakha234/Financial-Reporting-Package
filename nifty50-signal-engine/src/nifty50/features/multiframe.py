"""Higher-timeframe context, aligned without look-ahead.

This is the single easiest place in the whole feature layer to leak the future,
and the leak is invisible in the output.

A 1-hour bar stamped 10:15 covers 10:15 to 11:15 and is only **complete at 11:15**.
Joining higher-timeframe features onto 15-minute bars by bar *start* — the
obvious `reindex(...).ffill()` — hands the 10:30 bar a 1-hour bar containing
prices from 10:30 through 11:15. The strategy then appears to predict the next
45 minutes, because it was shown them.

The fix is to align on each higher bar's **close** time, so a base bar only ever
sees higher-timeframe bars that had already finished.
"""

from __future__ import annotations

import pandas as pd

from nifty50.domain import Timeframe
from nifty50.frames import bar_index
from nifty50.trading_calendar.calendar import TradingCalendar


def higher_timeframe_close_times(
    frame: pd.DataFrame, calendar: TradingCalendar, timeframe: Timeframe
) -> pd.DatetimeIndex:
    """When each higher-timeframe bar actually became complete.

    Clamped to the session close, so the ragged final hourly bar (15:15 to 15:30)
    is available from 15:30 rather than from a fictional 16:15.
    """
    index = bar_index(frame)
    return pd.DatetimeIndex([calendar.bar_end(ts, timeframe) for ts in index], name="available_at")


def align_higher_timeframe(
    base_index: pd.DatetimeIndex,
    higher_frame: pd.DataFrame,
    calendar: TradingCalendar,
    higher_timeframe: Timeframe,
    *,
    suffix: str | None = None,
) -> pd.DataFrame:
    """Attach higher-timeframe columns to ``base_index`` as-of their close.

    Each base bar receives the most recent higher-timeframe row that had already
    closed at or before that bar's start. Bars earlier than the first completed
    higher bar get NaN rather than a forward fill from the future.
    """
    if higher_frame.empty:
        return pd.DataFrame(index=base_index)

    label = suffix or higher_timeframe.value
    available_at = higher_timeframe_close_times(higher_frame, calendar, higher_timeframe)
    right = higher_frame.copy()
    right.insert(0, "available_at", available_at)
    right = right.sort_values("available_at")
    right.columns = ["available_at", *(f"{c}_{label}" for c in higher_frame.columns)]

    left = pd.DataFrame(index=base_index).reset_index(names="ts").sort_values("ts")
    merged = pd.merge_asof(
        left,
        right,
        left_on="ts",
        right_on="available_at",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.set_index("ts").drop(columns=["available_at"])
    merged.index.name = base_index.name
    return merged.reindex(base_index)


def conflict_score(base_state: pd.Series, higher_state: pd.Series) -> pd.Series:
    """Agreement between a base-timeframe state and its higher-timeframe one.

    +1 aligned, -1 opposed, 0 when either is flat or unknown.

    The spec is explicit that a conflict downgrades a signal rather than
    suppressing it, and that the disagreement must be recorded. So this is
    emitted as its own column for the decision engine to weigh and for the
    dashboard to show, not folded silently into a score.
    """
    base = base_state.fillna(0)
    higher = higher_state.reindex(base_state.index).fillna(0)
    product = base * higher
    score = pd.Series(0, index=base_state.index, dtype="int64")
    score[product > 0] = 1
    score[product < 0] = -1
    return score.rename("mtf_conflict")
