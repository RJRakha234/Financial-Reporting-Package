"""Open interest: positioning, and the contract cycle that contaminates it.

Open interest is the only genuinely new information in a futures file. Cash
OHLCV shows what traded; OI shows how many contracts are still *held*, which is
the difference between a move that people are committing to and one they are
closing out of. Nothing derived from price and volume approximates it.

**It also arrives with a monthly artifact large enough to swamp any signal
built on it, and that is what this module exists to handle.**

NSE single-stock futures expire on the last Thursday of the month. A continuous
near-month series therefore shows, every cycle: OI bleeding away over the final
sessions as holders roll forward, collapsing to near zero on expiry, then
jumping by a thousand percent or more as the series switches to the next
contract. Measured on real SBIN data over seven years: 139 cliffs in 1,735
sessions, and a naive five-day OI change whose fifth percentile is -80.6%.

A model handed that will learn the calendar. It is the same failure as
``bar_of_session`` dominating the triple-barrier classifier — an artifact
wearing a signal's name, and it looks like skill because it is genuinely
predictable.

So every function here is cycle-aware. Changes are NaN across a roll rather
than enormous, and :func:`sessions_to_roll` is exposed so the caller can
exclude the pre-expiry window where OI decline is rolling rather than
sentiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

# A roll shows as OI multiplying overnight. Nifty 50 single-stock futures carry
# tens of millions of contracts-equivalent, so a genuine one-day doubling does
# not happen; anything at or above this is a contract switch.
_ROLL_MULTIPLE: float = 2.0

# Sessions before expiry where OI decline is dominated by holders rolling
# forward rather than by anyone changing their mind about direction.
_ROLL_WINDOW_SESSIONS: int = 5

# Two roll candidates closer together than this belong to one expiry. On real
# SBIN data OI printed exactly 0 on expiry day, then 16.2m, then 108.0m the day
# after -- three sessions, two threshold crossings, one contract change.
_MIN_SESSIONS_BETWEEN_ROLLS: int = 10


class Positioning(StrEnum):
    """The four-quadrant read every Indian derivatives desk uses.

    Price and open interest together say something neither says alone. The
    labels are conventional; the interpretations are not guarantees, and the
    quadrants are frequently mislabelled in retail commentary — short covering
    in particular is routinely reported as fresh buying.
    """

    LONG_BUILDUP = "long_buildup"        # price up,   OI up   -> new longs
    SHORT_BUILDUP = "short_buildup"      # price down, OI up   -> new shorts
    SHORT_COVERING = "short_covering"    # price up,   OI down -> shorts closing
    LONG_UNWINDING = "long_unwinding"    # price down, OI down -> longs closing
    UNDEFINED = "undefined"


@dataclass(frozen=True, slots=True)
class ContractCycle:
    """Where each session sits in the near-month contract's life."""

    cycle_id: pd.Series
    session_in_cycle: pd.Series
    sessions_to_roll: pd.Series
    roll_dates: pd.DatetimeIndex

    def describe(self) -> str:
        lengths = self.session_in_cycle.groupby(self.cycle_id).max() + 1
        return "\n".join(
            [
                f"ContractCycle: {len(self.roll_dates)} rolls detected",
                f"  cycle length: median {int(lengths.median())} sessions, "
                f"min {int(lengths.min())}, max {int(lengths.max())}",
            ]
        )


def detect_contract_cycles(
    open_interest: pd.Series, *, roll_multiple: float = _ROLL_MULTIPLE
) -> ContractCycle:
    """Split a continuous near-month series into its contract cycles.

    A roll is detected from the OI jump rather than from a calendar rule.
    Expiry is nominally the last Thursday, but it moves for holidays — the
    seven-year SBIN sample lands on Thursday only 67 times out of 139, with
    the rest on Wednesday, Tuesday, Monday or Friday. Hard-coding the weekday
    would misplace half the rolls; the data marks them unambiguously.
    """
    if roll_multiple <= 1.0:
        raise ValueError("roll_multiple must exceed 1.0")
    previous = open_interest.shift(1)
    # OI prints exactly 0 on some expiry days, which makes the ratio infinite.
    # Treat "was zero, now is not" as a roll rather than dividing by it.
    ratio = open_interest / previous.replace(0.0, np.nan)
    candidate = (
        (ratio >= roll_multiple) | ((previous <= 0.0) & (open_interest > 0.0))
    ).fillna(value=False)
    is_roll = _collapse_roll_cluster(candidate)

    cycle_id = is_roll.cumsum().astype("int64").rename("oi_cycle_id")
    position = cycle_id.groupby(cycle_id).cumcount()
    session_in_cycle = pd.Series(
        position.to_numpy(), index=open_interest.index, name="oi_session_in_cycle"
    )
    length = cycle_id.map(cycle_id.value_counts())
    to_roll = pd.Series(
        (length.to_numpy() - 1 - session_in_cycle.to_numpy()),
        index=open_interest.index,
        name="oi_sessions_to_roll",
    )
    return ContractCycle(
        cycle_id=cycle_id,
        session_in_cycle=session_in_cycle,
        sessions_to_roll=to_roll,
        roll_dates=pd.DatetimeIndex(open_interest.index[is_roll.to_numpy()]),
    )


def _collapse_roll_cluster(
    candidate: pd.Series, *, min_gap: int = _MIN_SESSIONS_BETWEEN_ROLLS
) -> pd.Series:
    """Keep one roll per expiry: the last crossing in each cluster.

    An expiry can trip the threshold on consecutive sessions -- OI collapses,
    prints a stub, then establishes the new contract. Counting each crossing
    separately creates a one-session "cycle" that then poisons every
    cycle-grouped statistic computed over it.

    The LAST crossing is kept because that is the session on which the new
    contract's open interest is actually established.
    """
    positions = [int(value) for value in np.flatnonzero(candidate.to_numpy())]
    keep: list[int] = []
    for position in positions:
        if keep and position - keep[-1] < min_gap:
            keep[-1] = position          # same expiry, later crossing wins
        else:
            keep.append(position)
    collapsed = np.zeros(len(candidate), dtype=bool)
    collapsed[keep] = True
    return pd.Series(collapsed, index=candidate.index)


def oi_change(
    open_interest: pd.Series, cycle: ContractCycle, *, periods: int = 1
) -> pd.Series:
    """Fractional OI change over ``periods``, NaN wherever it spans a roll.

    NaN rather than zero or a forward fill. A missing reading is honest; a
    zero says "positioning did not change", which is a claim about the market
    that the data cannot support across a contract switch.
    """
    if periods < 1:
        raise ValueError("periods must be at least 1")
    change = open_interest.pct_change(periods=periods, fill_method=None)
    same_cycle = cycle.cycle_id == cycle.cycle_id.shift(periods)
    return change.where(same_cycle).rename(f"oi_change_{periods}")


def oi_zscore(
    open_interest: pd.Series, cycle: ContractCycle, *, window: int = 5
) -> pd.Series:
    """OI relative to its own recent level, standardised within the cycle.

    Compares like with like: OI builds mechanically through a cycle as the
    contract becomes the front month, so an absolute level says more about the
    date than about conviction. Only readings with a full window inside one
    cycle are scored.

    **The window must be short relative to the cycle.** A contract cycle is
    about twenty sessions, so a twenty-session window completes only at the
    very end -- where OI is collapsing into expiry. Run that way the score is
    negative essentially everywhere: the first attempt here used window=20 and
    produced a median of -2.93 with a maximum of +0.8, which is a measurement
    of the calendar, not of positioning.
    """
    lengths = cycle.session_in_cycle.groupby(cycle.cycle_id).max() + 1
    typical = float(lengths.median()) if len(lengths) else float("inf")
    if window > typical / 2.0:
        raise ValueError(
            f"window={window} is more than half the median cycle length "
            f"({typical:.0f} sessions). It would only ever complete near expiry, "
            "where OI is collapsing, and the score would measure the roll."
        )
    grouped = open_interest.groupby(cycle.cycle_id)
    mean = grouped.transform(lambda s: s.rolling(window, min_periods=window).mean())
    deviation = grouped.transform(lambda s: s.rolling(window, min_periods=window).std(ddof=0))
    scores = (open_interest - mean) / deviation.replace(0.0, np.nan)
    return scores.rename("oi_zscore")


def oi_cycle_matched_zscore(
    open_interest: pd.Series, cycle: ContractCycle, *, lookback_cycles: int = 6
) -> pd.Series:
    """OI scored against the SAME point in previous contract cycles.

    The correct comparison, and the direct analogue of
    :func:`nifty50.features.volume.session_matched_volume_zscore`: NSE
    intraday volume is U-shaped, so 10:30 is scored against prior 10:30s
    rather than a flat mean. Open interest has the same problem one level up —
    it builds through a contract cycle and bleeds out into expiry — so day 7 of
    a cycle must be scored against day 7 of earlier cycles, not against the
    last five sessions of its own.

    The trailing-window version (:func:`oi_zscore`) cannot do this. Its window
    must be short to fit inside a ~20-session cycle, and a five-point z-score
    is arithmetically bounded at ``(n-1)/sqrt(n)`` = 1.79 — it cannot express
    an extreme even when one occurs. Prefer this function; the other is kept
    for the case where cycle history is too short.

    Strictly backward-looking: only cycles that closed before the current one
    contribute.
    """
    if lookback_cycles < 2:
        raise ValueError("need at least two prior cycles to score against")
    frame = pd.DataFrame(
        {
            "oi": open_interest.to_numpy(),
            "cycle": cycle.cycle_id.to_numpy(),
            "day": cycle.session_in_cycle.to_numpy(),
        },
        index=open_interest.index,
    )
    scores = pd.Series(np.nan, index=open_interest.index, dtype="float64")
    for _day, group in frame.groupby("day"):
        if len(group) < 3:
            continue
        series = group["oi"]
        # shift(1) first: the current cycle's own value must not be in its
        # own baseline, or every reading is scored against itself.
        history = series.shift(1)
        mean = history.rolling(lookback_cycles, min_periods=2).mean()
        deviation = history.rolling(lookback_cycles, min_periods=2).std(ddof=0)
        scores.loc[group.index] = (
            (series - mean) / deviation.replace(0.0, np.nan)
        ).to_numpy()
    return scores.rename("oi_cycle_matched_zscore")


def positioning_state(
    close: pd.Series,
    open_interest: pd.Series,
    cycle: ContractCycle,
    *,
    periods: int = 1,
    exclude_roll_window: int = _ROLL_WINDOW_SESSIONS,
) -> pd.Series:
    """The four-quadrant price/OI read, with the roll window excluded.

    ``exclude_roll_window`` blanks the sessions before expiry. OI falls there
    because holders are rolling forward, not because anyone turned bearish, so
    a quadrant computed then reports "long unwinding" every single month.
    """
    price_change = close.pct_change(periods=periods, fill_method=None)
    interest_change = oi_change(open_interest, cycle, periods=periods)

    state = pd.Series(Positioning.UNDEFINED.value, index=close.index, dtype=object)
    rising_price = price_change > 0
    rising_interest = interest_change > 0
    known = price_change.notna() & interest_change.notna()

    state[known & rising_price & rising_interest] = Positioning.LONG_BUILDUP.value
    state[known & ~rising_price & rising_interest] = Positioning.SHORT_BUILDUP.value
    state[known & rising_price & ~rising_interest] = Positioning.SHORT_COVERING.value
    state[known & ~rising_price & ~rising_interest] = Positioning.LONG_UNWINDING.value

    if exclude_roll_window:
        near = cycle.sessions_to_roll < exclude_roll_window
        state[near.to_numpy()] = Positioning.UNDEFINED.value
    return state.rename("positioning")


def basis(futures_close: pd.Series, spot_close: pd.Series) -> pd.Series:
    """Futures premium over spot, as a fraction.

    Normally a small positive cost of carry — the seven-year SBIN median is
    +0.23%. It is informative in two directions: an unusually wide premium
    means leveraged longs are paying up to be positioned, and a discount in a
    liquid large-cap is rare enough to be worth a look rather than a trade.

    Note it decays mechanically to zero at expiry as the contract converges to
    spot, so a basis feature must be read alongside
    :attr:`ContractCycle.sessions_to_roll` or it will mostly measure the date.
    """
    aligned = spot_close.reindex(futures_close.index)
    ratio: pd.Series = futures_close / aligned.replace(0.0, np.nan) - 1.0
    return ratio.rename("basis")


def compute_open_interest_features(
    futures: pd.DataFrame,
    *,
    spot_close: pd.Series | None = None,
    change_periods: int = 5,
    zscore_window: int = 5,
) -> pd.DataFrame:
    """All cycle-safe OI features for one instrument.

    ``futures`` needs ``close`` and ``open_interest``; ``spot_close`` adds the
    basis. Every column is NaN rather than wrong wherever the contract cycle
    makes it undefined.
    """
    for column in ("close", "open_interest"):
        if column not in futures.columns:
            raise ValueError(f"futures frame needs a {column!r} column")

    cycle = detect_contract_cycles(futures["open_interest"])
    columns: dict[str, pd.Series] = {
        "oi_session_in_cycle": cycle.session_in_cycle.astype("float64"),
        "oi_sessions_to_roll": cycle.sessions_to_roll.astype("float64"),
        "oi_change_1": oi_change(futures["open_interest"], cycle, periods=1),
        f"oi_change_{change_periods}": oi_change(
            futures["open_interest"], cycle, periods=change_periods
        ),
        "oi_zscore": oi_zscore(futures["open_interest"], cycle, window=zscore_window),
        "oi_cycle_matched_zscore": oi_cycle_matched_zscore(
            futures["open_interest"], cycle
        ),
    }
    state = positioning_state(futures["close"], futures["open_interest"], cycle)
    for name in (
        Positioning.LONG_BUILDUP,
        Positioning.SHORT_BUILDUP,
        Positioning.SHORT_COVERING,
        Positioning.LONG_UNWINDING,
    ):
        columns[f"pos_{name.value}"] = (state == name.value).astype("float64")

    if spot_close is not None:
        columns["basis"] = basis(futures["close"], spot_close)
    return pd.DataFrame(columns, index=futures.index)
