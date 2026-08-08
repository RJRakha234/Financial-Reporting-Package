"""Features that only exist across many symbols at once.

Everything in the rest of :mod:`nifty50.features` describes one instrument in
isolation. This module describes an instrument *relative to its peers at the
same instant*, which is a different kind of statement and — on the evidence of
seven years of single-stock work — a more promising one.

The reason is arithmetic rather than opinion. A single stock's return is
``beta * market + idiosyncratic``. Roughly half the variance of a Nifty 50
constituent is the market leg, and no stock-specific feature can predict it.
Subtracting the cross-sectional mean removes that leg exactly, by construction
and with no estimation error, leaving the part a stock-specific signal could
plausibly explain. That is a signal-to-noise improvement of about 1.4x before
any modelling happens.

**Why this is not look-ahead.** Every function here reads other symbols at the
*same* timestamp, never a later one. At 10:15 a trader can see all fifty
prices; the ranking uses only what was simultaneously visible. The look-ahead
test in ``tests/test_lookahead.py`` still applies unchanged, because
truncating the future does not alter any cross-section.

**Two traps this module is built around.**

*Survivorship.* The panel must be assembled from the point-in-time universe,
not from whichever symbols happen to have files on disk. Ranking today's 50
constituents over seven years quietly deletes every company that was dropped
for performing badly, and manufactures an edge that never existed. See
:func:`build_panel`, which takes a universe and refuses to guess.

*Uneven coverage.* If three symbols are missing at one bar, the remaining
forty-seven still rank fine — but a raw integer rank silently changes scale.
Ranks here are normalised to [0, 1] so a cross-section of 47 is comparable
with one of 50.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from nifty50.trading_calendar.calendar import TradingCalendar
from nifty50.universe import PointInTimeUniverse

# Below this many symbols a cross-section is not a cross-section. Ranking three
# names produces ranks of 0, 0.5 and 1 that look like signal and are noise.
_MIN_CROSS_SECTION: int = 10


class ThinCrossSectionError(ValueError):
    """Too few symbols at a timestamp for a cross-sectional statistic to mean anything."""


@dataclass(frozen=True, slots=True)
class Panel:
    """One value per symbol per timestamp, with membership already applied.

    ``frame`` is timestamps x symbols. A NaN means the symbol was not a
    constituent then, or had no bar — the two are deliberately indistinguishable
    downstream, because in both cases it is untradeable and must not be ranked.
    """

    frame: pd.DataFrame
    name: str = ""

    @property
    def symbols(self) -> list[str]:
        return [str(column) for column in self.frame.columns]

    @property
    def coverage(self) -> pd.Series:
        """Symbols available at each timestamp."""
        return self.frame.notna().sum(axis=1).rename("coverage")

    def describe(self) -> str:
        counts = self.coverage
        return "\n".join(
            [
                f"Panel '{self.name}': {self.frame.shape[0]:,} bars x "
                f"{self.frame.shape[1]} symbols",
                f"  coverage: min {int(counts.min())}, median {int(counts.median())}, "
                f"max {int(counts.max())} symbols per bar",
                f"  bars below the {_MIN_CROSS_SECTION}-symbol floor: "
                f"{int((counts < _MIN_CROSS_SECTION).sum()):,}",
            ]
        )


def build_panel(
    frames: dict[str, pd.DataFrame],
    column: str,
    universe: PointInTimeUniverse,
    calendar: TradingCalendar,
    *,
    name: str = "",
) -> Panel:
    """Assemble one column across symbols, masked by point-in-time membership.

    A symbol's value is kept only on the sessions it was actually an index
    constituent. This is the single most important line of defence against
    survivorship bias in the whole project, and it belongs here rather than in
    the strategy because a strategy that has already been handed a
    forward-looking panel cannot undo it.
    """
    if not frames:
        raise ValueError("no frames supplied")

    columns: dict[str, pd.Series] = {}
    for symbol, frame in frames.items():
        if column not in frame.columns:
            continue
        columns[symbol] = frame[column]
    if not columns:
        raise ValueError(f"no frame contained a {column!r} column")

    wide = pd.DataFrame(columns).sort_index()
    sessions = pd.DatetimeIndex(wide.index).date
    mask = _membership_mask(wide, sessions, universe)
    return Panel(frame=wide.where(mask), name=name or column)


def _membership_mask(
    wide: pd.DataFrame, sessions: np.ndarray, universe: PointInTimeUniverse
) -> pd.DataFrame:
    """True where the symbol was an index member on that bar's session."""
    # Membership changes only at reconstitutions, so resolve once per session
    # rather than once per bar -- on 15m data that is 25x less work.
    unique = sorted(set(sessions))
    members_by_day: dict[dt.date, frozenset[str]] = {
        day: universe.members_on(day) for day in unique
    }
    mask = np.empty(wide.shape, dtype=bool)
    symbols = list(wide.columns)
    for position, day in enumerate(sessions):
        members = members_by_day[day]
        mask[position] = [symbol in members for symbol in symbols]
    return pd.DataFrame(mask, index=wide.index, columns=wide.columns)


# --------------------------------------------------------------------------
# The cross-sectional transforms
# --------------------------------------------------------------------------


def cross_sectional_rank(panel: Panel, *, min_symbols: int = _MIN_CROSS_SECTION) -> pd.DataFrame:
    """Rank each symbol against its peers at the same bar, normalised to [0, 1].

    0.0 is the weakest name in the cross-section, 1.0 the strongest, 0.5 the
    median. Normalising matters because coverage varies: a raw rank of 25 means
    "median" out of 50 and "strongest quartile" out of 30.

    Rank rather than z-score is the default because a single stock gapping 20%
    on results would dominate a z-scored cross-section and drag every other
    name's score with it. Ranks are immune to that by construction — which is
    also their weakness, since they discard magnitude. Both are provided.
    """
    ranked = panel.frame.rank(axis=1, pct=True, na_option="keep")
    return ranked.where(panel.coverage >= min_symbols)


def cross_sectional_zscore(
    panel: Panel, *, min_symbols: int = _MIN_CROSS_SECTION, winsorise: float = 0.0
) -> pd.DataFrame:
    """Standardise each bar's cross-section to mean 0, standard deviation 1.

    Keeps magnitude, which rank throws away — the difference between "slightly
    the strongest" and "twice as strong as anything else" is real information.

    ``winsorise`` clips each tail before standardising (0.02 is a common
    choice). Without it a single earnings gap sets the scale for the whole
    cross-section and every other name is squeezed towards zero.
    """
    frame = panel.frame
    if winsorise:
        if not 0.0 < winsorise < 0.5:
            raise ValueError("winsorise must be a fraction in (0, 0.5)")
        lower = frame.quantile(winsorise, axis=1)
        upper = frame.quantile(1.0 - winsorise, axis=1)
        frame = frame.clip(lower=lower, upper=upper, axis=0)

    mean = frame.mean(axis=1)
    deviation = frame.std(axis=1, ddof=0)
    scores = frame.sub(mean, axis=0).div(deviation.replace(0.0, np.nan), axis=0)
    return scores.where(panel.coverage >= min_symbols)


def demean_by_cross_section(panel: Panel) -> pd.DataFrame:
    """Subtract the equal-weight cross-sectional mean from every symbol.

    This *is* the market-neutral operation, and it is worth being clear about
    why it beats regressing on an index. A rolling beta is an estimate, with
    standard error, computed on a trailing window that spans regimes it no
    longer describes. The cross-sectional mean is not estimated at all — it is
    the realised equal-weight portfolio return that instant, exact and current.

    What remains is the part of each name's move that was not shared. That is
    the only part a stock-specific signal has any business trying to predict.
    """
    return panel.frame.sub(panel.frame.mean(axis=1), axis=0)


# --------------------------------------------------------------------------
# Regime: when is stock-picking even possible
# --------------------------------------------------------------------------


def dispersion(returns: Panel, *, min_symbols: int = _MIN_CROSS_SECTION) -> pd.Series:
    """Cross-sectional standard deviation of returns at each bar.

    The single most useful gate a cross-sectional book can have. When
    dispersion collapses everything moves together, the spread between your
    longs and shorts has nowhere to come from, and you are paying full costs to
    harvest nothing. When it spikes, stock-specific information is driving
    prices and ranking has something to rank.

    Note the asymmetry with volatility: high *index* volatility often means low
    dispersion, because a macro shock moves every name the same way. They are
    different regimes and must not be conflated.
    """
    values = returns.frame.std(axis=1, ddof=1)
    return values.where(returns.coverage >= min_symbols).rename("dispersion")


def average_pairwise_correlation(
    returns: Panel, window: int, *, min_symbols: int = _MIN_CROSS_SECTION
) -> pd.Series:
    """Mean pairwise correlation across the cross-section, over a trailing window.

    Derived from the variance ratio rather than by building an N x N matrix per
    bar: for an equal-weight portfolio, ``var(mean) / mean(var)`` equals
    ``(1 + (N-1) * rho) / N``, which rearranges to rho directly. Exact, and
    O(N) instead of O(N^2).

    This is the number that decides how much a 50-stock panel is really worth.
    At rho = 0.45 the effective sample size is 2.2 independent series, not 50.
    """
    if window < 2:
        raise ValueError("correlation window must be at least 2")
    frame = returns.frame
    portfolio = frame.mean(axis=1)

    portfolio_variance = portfolio.rolling(window=window, min_periods=window).var(ddof=1)
    mean_variance = (
        frame.rolling(window=window, min_periods=window).var(ddof=1).mean(axis=1)
    )
    count = returns.coverage.clip(lower=1)

    ratio = portfolio_variance / mean_variance.replace(0.0, np.nan)
    rho = (count * ratio - 1.0) / (count - 1).replace(0, np.nan)
    return rho.where(returns.coverage >= min_symbols).clip(-1.0, 1.0).rename("avg_pairwise_corr")


def breadth(returns: Panel, *, min_symbols: int = _MIN_CROSS_SECTION) -> pd.Series:
    """Fraction of the cross-section that rose on the bar, in [0, 1].

    A blunt instrument with one specific use: separating a move led by a few
    heavyweights from one the whole index participated in. RELIANCE, HDFC Bank
    and ICICI are a large share of the Nifty 50 by weight, so the index can
    rise on a session where most constituents fell.
    """
    positive = (returns.frame > 0).sum(axis=1)
    total = returns.frame.notna().sum(axis=1)
    values = positive / total.replace(0, np.nan)
    return values.where(returns.coverage >= min_symbols).rename("breadth")


# --------------------------------------------------------------------------
# Turning a cross-section into target weights
# --------------------------------------------------------------------------


def rank_portfolio_weights(
    scores: pd.DataFrame,
    *,
    long_count: int,
    short_count: int = 0,
    gross_exposure: float = 1.0,
) -> pd.DataFrame:
    """Convert per-bar scores into target weights: long the top, short the bottom.

    Weights are equal within each leg and sum to ``gross_exposure`` in absolute
    value, so a long/short book at ``gross_exposure=1.0`` is 50% long and 50%
    short — *not* 100% each way. Getting that wrong is how a "market-neutral"
    backtest quietly runs at 2x leverage.

    ``short_count=0`` gives a long-only book, which for Indian cash equities is
    the only version that can be held overnight: cash-segment short selling is
    intraday-only, and an overnight short needs futures or SLB with a different
    cost and margin model entirely. The engine does not know that; you do.
    """
    if long_count < 1:
        raise ValueError("long_count must be at least 1")
    if short_count < 0:
        raise ValueError("short_count cannot be negative")
    if gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")

    weights = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)
    legs = 2 if short_count else 1
    long_weight = gross_exposure / legs / long_count
    short_weight = -gross_exposure / legs / short_count if short_count else 0.0

    ranked = scores.rank(axis=1, ascending=False, na_option="keep")
    available = scores.notna().sum(axis=1)
    enough = available >= (long_count + short_count)

    longs = ranked.le(long_count, axis=0) & enough.to_numpy()[:, None]
    weights = weights.mask(longs, long_weight)
    if short_count:
        from_bottom = scores.rank(axis=1, ascending=True, na_option="keep")
        shorts = from_bottom.le(short_count, axis=0) & enough.to_numpy()[:, None]
        weights = weights.mask(shorts, short_weight)
    return weights


def turnover(weights: pd.DataFrame) -> pd.Series:
    """One-way turnover per bar: half the sum of absolute weight changes.

    The number that decides whether a cross-sectional book is viable. Every
    unit of turnover pays the full round-trip cost stack, and a daily-rebalanced
    20-name book generates enough of it to bury most realistic edges — which is
    why this is returned as a first-class output rather than buried in the
    backtest's totals.
    """
    changes = weights.fillna(0.0).diff().abs().sum(axis=1) / 2.0
    if len(changes):
        changes.iloc[0] = weights.iloc[0].abs().sum() / 2.0
    return changes.rename("turnover")
