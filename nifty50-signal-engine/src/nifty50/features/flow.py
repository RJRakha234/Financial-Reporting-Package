"""India-specific flow and market-structure features.

This is the module most likely to leak the future, and the leak is subtle
enough that it is worth naming before any code appears.

**Almost every India-specific dataset is published after the close of the
session it describes.** NSE's ``sec_bhavdata_full`` — the source of delivery
percentage — lands in the evening. The FII/DII provisional figures land around
19:30 and are revised the next day. A backtest that stamps a session's delivery
percentage onto that session's 09:15 bar is trading on a number that did not
exist for another nine hours, and it will show a spectacular, entirely fake
edge, because delivery percentage is mechanically correlated with the day's
own move.

So every function here takes an explicit ``lag_sessions`` and there is no
default that silently means zero. Where a dataset genuinely *is* known before
the open — the F&O ban list, the day's circuit bands — that is called out by
name, because those are the exceptions and treating them like the rest would
throw away real information.

The second theme is that most of these inputs are *daily* while the engine
trades 15-minute bars. Broadcasting a daily value across a session is correct
only if the value was known at the session open. Combined with the lag rule
above, that is exactly what :func:`align_daily_to_bars` enforces.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import numpy as np
import pandas as pd

from nifty50.features.core import rolling_zscore
from nifty50.features.session import session_date
from nifty50.trading_calendar.calendar import TradingCalendar

# NSE publishes the full securities bhavcopy, which carries delivered quantity,
# after the close. One session of lag is the minimum that is not a lie.
DELIVERY_PUBLICATION_LAG_SESSIONS: int = 1

# FII/DII figures are provisional on the evening of the trade date and revised
# the following evening. One session is the earliest a provisional number is
# usable; two is the earliest the revised one is.
PARTICIPANT_FLOW_LAG_SESSIONS: int = 1


class LookAheadError(ValueError):
    """Raised when an alignment would expose data before it was published."""


def align_daily_to_bars(
    daily: pd.Series,
    bars: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    lag_sessions: int,
) -> pd.Series:
    """Broadcast a daily series onto intraday bars, respecting publication lag.

    A bar belonging to session ``S`` receives the daily observation for the
    session ``lag_sessions`` trading days before ``S``. With
    ``lag_sessions=1``, Monday's bars all carry Friday's value — which is what
    a trader actually knew at Monday's open.

    ``lag_sessions=0`` is permitted but must be a deliberate choice: it asserts
    the value was public before the session opened. It is correct for circuit
    bands and the F&O ban list, and wrong for everything else in this module.

    Negative lags raise rather than being clamped. A negative lag is not a
    configuration mistake to be forgiven; it is a request to see the future.
    """
    if lag_sessions < 0:
        raise LookAheadError(
            f"lag_sessions={lag_sessions} would align bars to data published after them"
        )
    if bars.empty:
        return pd.Series(dtype="float64", index=bars.index, name=daily.name)

    sessions = session_date(bars)
    unique_sessions = sorted(set(sessions))
    source_for: dict[dt.date, dt.date] = {
        day: (calendar.previous_trading_day(day, lag_sessions) if lag_sessions else day)
        for day in unique_sessions
    }

    lookup = _date_keyed(daily)
    values = [lookup.get(source_for[day], np.nan) for day in sessions]
    return pd.Series(values, index=bars.index, dtype="float64", name=daily.name)


# --------------------------------------------------------------------------
# Delivery: the closest thing NSE gives retail to a conviction gauge
# --------------------------------------------------------------------------


def delivery_features(
    delivery_pct: pd.Series,
    bars: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    window: int,
    lag_sessions: int = DELIVERY_PUBLICATION_LAG_SESSIONS,
) -> pd.DataFrame:
    """Delivery percentage, its trailing z-score, and its trend.

    Delivery percentage is delivered quantity over traded quantity: the share
    of the day's volume that resulted in an actual change of beneficial
    ownership rather than an intraday round trip. High delivery on a rising
    price is the standard Indian read for accumulation rather than churn.

    What it is not: a clean signal. Delivery percentage is mechanically higher
    on low-volume days, because intraday churn scales faster than positional
    buying, so the z-score below is against the symbol's own history rather
    than a cross-sectional level — comparing a bank's 45% against an IT name's
    70% says more about who trades them than about conviction in either.

    ``window`` is in *sessions*, not bars: the underlying series is daily, and
    a 20-bar window on a 15-minute chart would be 20 identical values.
    """
    if window < 2:
        raise ValueError("delivery window must span at least two sessions")
    daily = delivery_pct.sort_index()
    frame = pd.DataFrame(
        {
            "delivery_pct": daily,
            "delivery_pct_zscore": rolling_zscore(daily, window),
            "delivery_pct_change": daily.diff(),
            # Rising delivery share while price rises is the accumulation read;
            # this column carries the delivery half, the decision layer pairs it.
            "delivery_pct_trend": daily.rolling(window=window, min_periods=window).mean().diff(),
        }
    )
    return _align_frame(frame, bars, calendar, lag_sessions=lag_sessions)


# --------------------------------------------------------------------------
# Participant flows
# --------------------------------------------------------------------------


def participant_flow_features(
    fii_net_crore: pd.Series,
    dii_net_crore: pd.Series,
    bars: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    window: int,
    lag_sessions: int = PARTICIPANT_FLOW_LAG_SESSIONS,
) -> pd.DataFrame:
    """FII and DII net cash-market flows, z-scored, plus their divergence.

    These are *market-wide* figures, identical for every symbol. They are
    included because Indian equities have a persistent, well-documented
    regime: sustained FII selling absorbed by DII buying produces an index
    that grinds sideways while individual names diverge sharply, and that is a
    materially different environment for a trend signal than either side
    acting alone.

    Two honest caveats. The figures are cash-segment only, so they miss the
    index-futures positioning where a great deal of FII directional risk
    actually sits. And they are provisional on release and revised later, so a
    live signal and a backtest signal are computed from different numbers for
    the same date unless the store keeps the provisional vintage — which is a
    point-in-time data problem identical in kind to index membership.
    """
    aligned_dii = dii_net_crore.reindex(fii_net_crore.index)
    frame = pd.DataFrame(
        {
            "fii_net_crore": fii_net_crore,
            "dii_net_crore": aligned_dii,
            "fii_flow_zscore": rolling_zscore(fii_net_crore, window),
            "dii_flow_zscore": rolling_zscore(aligned_dii, window),
            # Opposite signs mean the two are trading against each other, which
            # is the absorption regime described above.
            "fii_dii_opposed": (
                np.sign(fii_net_crore) * np.sign(aligned_dii) < 0
            ).astype("float64"),
            "net_institutional_crore": fii_net_crore + aligned_dii,
        }
    ).sort_index()
    return _align_frame(frame, bars, calendar, lag_sessions=lag_sessions)


# --------------------------------------------------------------------------
# Known-before-the-open data: the exceptions to the lag rule
# --------------------------------------------------------------------------


def circuit_band_state(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    previous_close: pd.Series,
    *,
    band_pct: float,
) -> pd.DataFrame:
    """Distance to the day's price band, and whether it has been touched.

    Circuit bands are set by the exchange *before* the session on the previous
    close, so unlike everything else in this module they carry no publication
    lag — the band applicable today is known at today's open.

    Why it matters for an alert-only tool: a signal that fires as a stock
    approaches its upper band is a signal you cannot act on, because at the
    band there is no offer. Reporting a fill there is the single most common
    way an Indian backtest manufactures returns that do not exist. The
    ``at_upper_band`` / ``at_lower_band`` flags exist so the execution model
    can refuse those fills rather than pretending.

    ``band_pct`` is per-symbol in reality (2%, 5%, 10%, 20%, or no band for
    F&O names). Passing a single scalar is a simplification, and for Nifty 50
    constituents — all of which are in F&O and therefore have a 10% dynamic
    band rather than a hard one — it is a rough one. Stated here rather than
    buried.
    """
    if not 0.0 < band_pct < 1.0:
        raise ValueError(f"band_pct must be a fraction in (0, 1), got {band_pct}")
    # Rounded to paise, because that is what the exchange actually publishes.
    # Left unrounded, a 10% band on a close of 100.00 is 110.00000000000001 in
    # binary floating point, and a trade printed at exactly 110.00 does not
    # register as a band touch. The failure is silent and one-sided: it only
    # ever under-reports circuit hits, which is the direction that makes a
    # backtest look better than reality.
    upper = (previous_close * (1.0 + band_pct)).round(2)
    lower = (previous_close * (1.0 - band_pct)).round(2)
    span = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "upper_band": upper,
            "lower_band": lower,
            # 0 at the lower band, 1 at the upper: where in the permitted range
            # the close sits.
            "band_position": ((close - lower) / span).clip(0.0, 1.0),
            "at_upper_band": (high >= upper).astype("float64"),
            "at_lower_band": (low <= lower).astype("float64"),
        }
    )


def fno_ban_flag(
    ban_dates: Iterable[dt.date], bars: pd.DataFrame, *, name: str = "fno_ban"
) -> pd.Series:
    """1.0 on sessions where the symbol is in the F&O ban period, else 0.0.

    The ban list is the other exception to the lag rule: the exchange
    publishes tomorrow's list this evening, so the flag for a session is
    genuinely known before that session opens.

    A ban means open interest has crossed 95% of market-wide position limit
    and only position-reducing F&O trades are allowed. The cash market stays
    open, but the constraint on the derivative leg changes cash behaviour —
    ban periods run hot and reverse hard — so this is a regime flag, not a
    tradeability flag.
    """
    banned = set(ban_dates)
    if bars.empty:
        return pd.Series(dtype="float64", index=bars.index, name=name)
    sessions = session_date(bars)
    return pd.Series(
        [1.0 if day in banned else 0.0 for day in sessions],
        index=bars.index,
        dtype="float64",
        name=name,
    )


def index_event_proximity(
    event_dates: Iterable[dt.date],
    bars: pd.DataFrame,
    *,
    lead_sessions: int = 5,
    name: str = "index_event_proximity",
) -> pd.Series:
    """Sessions until the next index reconstitution effective date, scaled to [0, 1].

    NSE Indices announces Nifty 50 changes roughly four weeks before they take
    effect, and the window between announcement and effect is the single most
    distorted period in an affected name's tape: index funds must trade the
    change, and everyone knows the date and the direction.

    1.0 on the effective date itself, decaying to 0.0 at ``lead_sessions``
    away and beyond. Announcement dates, not effective dates, are what a
    trader actually learns first — but the effective date is what is
    reconstructible from the constituents file, so that is what this uses.
    Passing announcement dates instead is supported and preferable if you have
    them.
    """
    scheduled = sorted(set(event_dates))
    if bars.empty or not scheduled:
        return pd.Series(0.0, index=bars.index, dtype="float64", name=name)
    sessions = session_date(bars)
    scores: list[float] = []
    for day in sessions:
        upcoming = [d for d in scheduled if d >= day]
        if not upcoming:
            scores.append(0.0)
            continue
        # Calendar days, not sessions: the announcement window is quoted in
        # weeks and the approximation is immaterial at this resolution.
        distance = (upcoming[0] - day).days
        scores.append(max(0.0, 1.0 - distance / float(lead_sessions)))
    return pd.Series(scores, index=bars.index, dtype="float64", name=name)


def _align_frame(
    frame: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    lag_sessions: int,
) -> pd.DataFrame:
    aligned = {
        str(column): align_daily_to_bars(
            frame[column].rename(column), bars, calendar, lag_sessions=lag_sessions
        )
        for column in frame.columns
    }
    return pd.DataFrame(aligned, index=bars.index)


def _date_keyed(daily: pd.Series) -> dict[dt.date, float]:
    """Index a daily series by ``date``, whatever datetime-ish type it arrived as."""
    keyed: dict[dt.date, float] = {}
    for label, value in daily.items():
        if isinstance(label, (pd.Timestamp, dt.datetime)):
            key = label.date()
        elif isinstance(label, dt.date):
            key = label
        else:
            raise TypeError(f"daily series index must be dates, got {type(label)!r}")
        keyed[key] = float(value)
    return keyed
