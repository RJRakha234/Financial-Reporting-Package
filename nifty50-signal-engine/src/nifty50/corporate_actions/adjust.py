"""Back-adjustment of OHLCV history for corporate actions.

Design note — factors are applied on *read*, never baked into stored bars.
Storage keeps the raw traded prints forever. That costs a multiply per read and
buys three things:

* discovering a missed 2021 bonus in 2026 re-adjusts history for free, with no
  rewrite of six years of parquet and no risk of double-adjusting a file that
  was already touched;
* circuit bands, price-band proximity, tick-size rounding and the whole cost
  stack keep working off prices that were actually printed, which is the only
  series on which those calculations mean anything;
* the raw series remains available as evidence when a factor is disputed.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
import pandas as pd

from nifty50.corporate_actions.models import ActionType, CorporateAction, CorporateActionSet
from nifty50.domain import IST, OHLCV_COLUMNS

_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
_VOLUME_COLUMN: str = "volume"


class AdjustmentFactors(NamedTuple):
    """Per-bar cumulative multipliers."""

    price: pd.Series
    volume: pd.Series


@dataclass(slots=True)
class AdjustmentReport:
    """What was applied, and what could not be."""

    symbol: str
    applied: list[tuple[CorporateAction, float, float]] = field(default_factory=list)
    skipped: list[tuple[CorporateAction, str]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.skipped

    def describe(self) -> str:
        lines = [f"{self.symbol}: {len(self.applied)} action(s) applied"]
        for action, price_factor, volume_factor in self.applied:
            lines.append(
                f"  {action.ex_date} {action.action_type.value:9s} "
                f"price x{price_factor:.6f} volume x{volume_factor:.6f}"
            )
        for action, reason in self.skipped:
            lines.append(f"  SKIPPED {action.ex_date} {action.action_type.value}: {reason}")
        return "\n".join(lines)


class AdjustedBars(NamedTuple):
    """Adjusted frame plus the factors and provenance used to build it."""

    frame: pd.DataFrame
    factors: AdjustmentFactors
    report: AdjustmentReport


def session_dates(index: pd.DatetimeIndex) -> np.ndarray:
    """Calendar date of each bar in IST.

    NSE sessions never cross midnight, so the IST date *is* the session date.
    Converting explicitly keeps a UTC-stored index from silently reporting the
    previous day for every bar before 05:30 IST.
    """
    if index.tz is None:
        raise ValueError("bar index must be tz-aware")
    dates: np.ndarray = index.tz_convert(IST).date
    return dates


def compute_factors(
    index: pd.DatetimeIndex,
    actions: list[CorporateAction],
    *,
    cum_close_lookup: dict[dt.date, float] | None = None,
    adjust_dividends: bool = True,
    symbol: str = "",
    strict: bool = False,
) -> tuple[AdjustmentFactors, AdjustmentReport]:
    """Cumulative price and volume factors for every bar in ``index``.

    A bar dated ``d`` is multiplied by the product of the factors of every action
    whose ``ex_date`` is strictly greater than ``d`` — including actions dated
    after the end of the supplied data, which still apply to all of it.

    ``strict`` controls what happens when a value-based action has no cum close:
    raise, or record the skip in the report and carry on unadjusted. Default is
    lenient-but-loud, because a single missing dividend should not take down an
    overnight backfill, and the report is checked by the integrity pass.
    """
    report = AdjustmentReport(symbol=symbol)
    ones = pd.Series(1.0, index=index, dtype="float64")
    if len(index) == 0:
        return AdjustmentFactors(price=ones, volume=ones.copy()), report

    dates = session_dates(index)
    first_date: dt.date = min(dates)
    lookup = cum_close_lookup or {}

    usable: list[tuple[dt.date, float, float]] = []
    for action in sorted(actions, key=lambda a: a.ex_date):
        if action.ex_date <= first_date:
            # Entirely in the past relative to this window: already reflected in
            # the prints themselves.
            continue
        if action.action_type is ActionType.DIVIDEND and not adjust_dividends:
            report.skipped.append((action, "dividend adjustment disabled in config"))
            continue
        cum_close = lookup.get(action.ex_date) if action.action_type.needs_cum_price else None
        try:
            price_factor = action.price_factor(cum_close)
            volume_factor = action.volume_factor(cum_close)
        except ValueError as exc:
            if strict:
                raise
            report.skipped.append((action, str(exc)))
            continue
        usable.append((action.ex_date, price_factor, volume_factor))
        report.applied.append((action, price_factor, volume_factor))

    if not usable:
        return AdjustmentFactors(price=ones, volume=ones.copy()), report

    ex_dates = [item[0] for item in usable]
    price_factors = np.array([item[1] for item in usable], dtype="float64")
    volume_factors = np.array([item[2] for item in usable], dtype="float64")

    # Suffix products: suffix[i] is the product of factors i..n-1, i.e. every
    # action still in the future for a bar that sits just before ex_dates[i].
    price_suffix = np.append(np.cumprod(price_factors[::-1])[::-1], 1.0)
    volume_suffix = np.append(np.cumprod(volume_factors[::-1])[::-1], 1.0)

    positions = np.fromiter(
        (bisect_right(ex_dates, day) for day in dates), dtype=np.int64, count=len(dates)
    )
    return (
        AdjustmentFactors(
            price=pd.Series(price_suffix[positions], index=index, dtype="float64"),
            volume=pd.Series(volume_suffix[positions], index=index, dtype="float64"),
        ),
        report,
    )


def build_cum_close_lookup(
    frame: pd.DataFrame, actions: list[CorporateAction]
) -> dict[dt.date, float]:
    """Last raw close strictly before each value-based action's ex-date.

    Uses the raw series deliberately: the cum close is what actually traded on
    the last cum day, and pricing a 2023 demerger off a 2019-split-adjusted
    close would understate the strip by the split factor.
    """
    needed = sorted(
        {
            a.ex_date
            for a in actions
            if a.action_type.needs_cum_price and a.price_factor_override is None
        }
    )
    if not needed or frame.empty:
        return {}
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("frame must be indexed by a DatetimeIndex")
    dates = np.asarray(session_dates(index))
    closes = frame["close"].to_numpy(dtype="float64")
    order = np.argsort(dates, kind="stable")
    sorted_dates: list[dt.date] = list(dates[order])
    sorted_closes = closes[order]

    lookup: dict[dt.date, float] = {}
    for ex_date in needed:
        # bisect_LEFT: the cum close is the last bar STRICTLY BEFORE the ex-date.
        # bisect_right would return the ex-date's own bar, which already trades
        # adjusted — pricing the strip off it understates the factor.
        position = bisect_left(sorted_dates, ex_date)
        if position > 0:
            lookup[ex_date] = float(sorted_closes[position - 1])
    return lookup


def adjust_ohlcv(
    frame: pd.DataFrame,
    actions: list[CorporateAction],
    *,
    adjust_dividends: bool = True,
    symbol: str = "",
    strict: bool = False,
) -> AdjustedBars:
    """Return a back-adjusted copy of ``frame``.

    ``frame`` must be indexed by a tz-aware :class:`~pandas.DatetimeIndex` and
    carry the standard OHLCV columns. The returned frame keeps the raw prints in
    ``raw_close`` and the applied multipliers in ``price_factor`` /
    ``volume_factor``, so nothing downstream has to re-derive them.
    """
    _validate_ohlcv(frame)
    index = frame.index
    assert isinstance(index, pd.DatetimeIndex)

    lookup = build_cum_close_lookup(frame, actions)
    factors, report = compute_factors(
        index,
        actions,
        cum_close_lookup=lookup,
        adjust_dividends=adjust_dividends,
        symbol=symbol,
        strict=strict,
    )

    adjusted = frame.copy()
    for column in _PRICE_COLUMNS:
        adjusted[column] = frame[column].astype("float64") * factors.price
    # Volume stays integral in spirit but a split factor of 4/3 does not divide
    # evenly, so it is carried as float and rounded only at display time.
    adjusted[_VOLUME_COLUMN] = frame[_VOLUME_COLUMN].astype("float64") * factors.volume
    adjusted["raw_close"] = frame["close"].astype("float64")
    adjusted["price_factor"] = factors.price
    adjusted["volume_factor"] = factors.volume
    return AdjustedBars(frame=adjusted, factors=factors, report=report)


def adjust_for_symbol(
    frame: pd.DataFrame,
    symbol: str,
    action_set: CorporateActionSet,
    *,
    adjust_dividends: bool = True,
    strict: bool = False,
) -> AdjustedBars:
    """Convenience wrapper around :func:`adjust_ohlcv` for a whole action set."""
    return adjust_ohlcv(
        frame,
        action_set.for_symbol(symbol),
        adjust_dividends=adjust_dividends,
        symbol=symbol,
        strict=strict,
    )


def _validate_ohlcv(frame: pd.DataFrame) -> None:
    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"frame is missing OHLCV columns: {missing}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("frame must be indexed by a DatetimeIndex")
    if frame.index.tz is None:
        raise ValueError("frame index must be tz-aware")
