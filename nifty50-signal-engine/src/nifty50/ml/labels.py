"""Triple-barrier labelling.

This is the one module in the project that deliberately looks at the future.
It has to: supervised learning needs to know what happened next. Two things
follow, and both are load-bearing.

**A label is not a feature.** Nothing produced here may be fed back into
:mod:`nifty50.features`. The separation is structural — the ML package imports
from features, never the reverse — so a leak would have to be written on
purpose.

**Every label carries the time it was resolved.** A label stamped at 10:00
that took until 13:00 to resolve embeds information from the whole of that
window. Training on it while testing on 11:00 leaks, even though 11:00 is
strictly "after" 10:00 by the index. That is why :class:`TripleBarrierLabels`
returns ``touch_time`` alongside ``label``, and why
:mod:`nifty50.ml.splits` refuses to build a fold without it.

Why triple-barrier rather than "sign of the return in N bars": the fixed-horizon
label is a bad description of a trade nobody would place. It counts as a win a
path that fell 4% before recovering to close 0.2% up — a path that would have
stopped you out on the way. Labelling against the barriers a real position
would actually have is what makes the classifier's target correspond to
something achievable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from nifty50.domain import ensure_ist
from nifty50.frames import bar_index


class Barrier(StrEnum):
    """Which barrier resolved the label."""

    UPPER = "upper"
    LOWER = "lower"
    VERTICAL = "vertical"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class TripleBarrierLabels:
    """Labels plus the metadata the rest of the pipeline needs to use them safely.

    ``ambiguous_count`` is the number of bars where both the profit and stop
    barriers fell inside a single bar's high-low range. OHLC cannot say which
    came first, so those are resolved pessimistically as stop-outs. The count
    is surfaced rather than swallowed because if it is large, the barriers are
    too tight for the bar size and the labels are mostly an artefact of that.
    """

    label: pd.Series
    touch_time: pd.Series
    return_at_touch: pd.Series
    barrier: pd.Series
    ambiguous_count: int

    def __post_init__(self) -> None:
        if self.ambiguous_count < 0:
            raise ValueError("ambiguous_count cannot be negative")

    @property
    def ambiguous_fraction(self) -> float:
        resolved = int((self.barrier != Barrier.UNRESOLVED).sum())
        return self.ambiguous_count / resolved if resolved else 0.0

    def usable(self) -> pd.Index:
        """Bars whose label actually resolved before the data ran out.

        The tail of any series has labels that could not complete — the
        vertical barrier lies past the last bar. Training on those (as zeros,
        say) teaches the model that the end of the sample is uneventful.
        """
        return self.label.index[self.barrier != Barrier.UNRESOLVED]

    def describe(self) -> str:
        counts = self.label.value_counts(dropna=True).sort_index()
        total = int(counts.sum())
        lines = [f"Triple-barrier labels: {total} resolved of {len(self.label)} bars"]
        for value, count in counts.items():
            share = count / total if total else 0.0
            lines.append(f"  label {int(float(str(value))):+d}: {count:6d}  ({share:6.1%})")
        lines.append(
            f"  ambiguous (both barriers in one bar, resolved as stop): "
            f"{self.ambiguous_count} ({self.ambiguous_fraction:.1%})"
        )
        if self.ambiguous_fraction > 0.10:
            lines.append(
                "  WARNING: barriers are too tight for this bar size. More than one "
                "label in ten is a coin flip dressed as data."
            )
        return "\n".join(lines)


def triple_barrier_labels(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    volatility: pd.Series,
    horizon_bars: int,
    upper_multiple: float,
    lower_multiple: float,
    session_ordinal: pd.Series | None = None,
    min_barrier_pct: float = 0.0,
) -> TripleBarrierLabels:
    """Label each bar by which barrier a position opened there would have hit first.

    Barriers are volatility-scaled: ``upper_multiple * volatility`` above entry
    and ``lower_multiple * volatility`` below, where ``volatility`` is a
    *trailing* estimate — typically ATR as a fraction of price. Scaling matters
    because a fixed 1% barrier is a routine 15-minute move in one name and a
    three-day event in another; fixed barriers would mostly label which stock
    is volatile.

    ``session_ordinal`` caps the vertical barrier at the end of the entry's own
    session. Pass it whenever the strategy is intraday: without it, a label
    opened at 15:15 resolves against tomorrow morning's gap, which is a
    position an intraday strategy would never have been holding.

    ``min_barrier_pct`` floors the barrier width. During a dead lunchtime hour
    ATR can collapse to a few basis points, and barriers narrower than the
    round-trip cost produce labels that are pure noise — profitable in the
    label, loss-making in reality.

    Entry is assumed at the *close* of the labelled bar; barriers are checked
    from the following bar onward. Checking the entry bar's own high and low
    would resolve some labels against a range that had already happened.
    """
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1")
    if upper_multiple <= 0.0 or lower_multiple <= 0.0:
        raise ValueError("barrier multiples must be positive")

    index = bar_index(pd.DataFrame(index=close.index))
    prices = close.to_numpy(dtype="float64")
    highs = high.to_numpy(dtype="float64")
    lows = low.to_numpy(dtype="float64")
    widths = volatility.reindex(close.index).to_numpy(dtype="float64")
    total = len(prices)

    sessions = (
        session_ordinal.reindex(close.index).to_numpy(dtype="float64")
        if session_ordinal is not None
        else np.zeros(total, dtype="float64")
    )
    same_session_only = session_ordinal is not None

    labels = np.full(total, np.nan, dtype="float64")
    touch_positions = np.full(total, -1, dtype="int64")
    returns = np.full(total, np.nan, dtype="float64")
    barriers = np.full(total, Barrier.UNRESOLVED.value, dtype=object)
    ambiguous = 0

    for start in range(total):
        entry = prices[start]
        width = widths[start]
        if not np.isfinite(entry) or not np.isfinite(width) or entry <= 0.0 or width <= 0.0:
            continue
        width = max(width, min_barrier_pct)
        upper = entry * (1.0 + upper_multiple * width)
        lower = entry * (1.0 - lower_multiple * width)

        last = min(start + horizon_bars, total - 1)
        if same_session_only:
            # Walk the vertical barrier back to the final bar of the entry's
            # own session, so an intraday label never spans an overnight gap.
            while last > start and sessions[last] != sessions[start]:
                last -= 1
        if last <= start:
            continue

        resolved = False
        for step in range(start + 1, last + 1):
            touched_upper = highs[step] >= upper
            touched_lower = lows[step] <= lower
            if touched_upper and touched_lower:
                # One bar straddled both. OHLC does not record the order, so
                # assume the adverse one — an optimistic guess here is the
                # difference between a backtest that works and one that lies.
                ambiguous += 1
                _record(labels, touch_positions, returns, barriers, start, step, -1.0,
                        lower / entry - 1.0, Barrier.LOWER)
                resolved = True
                break
            if touched_upper:
                _record(labels, touch_positions, returns, barriers, start, step, 1.0,
                        upper / entry - 1.0, Barrier.UPPER)
                resolved = True
                break
            if touched_lower:
                _record(labels, touch_positions, returns, barriers, start, step, -1.0,
                        lower / entry - 1.0, Barrier.LOWER)
                resolved = True
                break
        if not resolved:
            _record(labels, touch_positions, returns, barriers, start, last, 0.0,
                    prices[last] / entry - 1.0, Barrier.VERTICAL)

    touch_time = pd.Series(
        [index[position] if position >= 0 else pd.NaT for position in touch_positions],
        index=close.index,
        name="touch_time",
    )
    return TripleBarrierLabels(
        label=pd.Series(labels, index=close.index, name="label"),
        touch_time=touch_time,
        return_at_touch=pd.Series(returns, index=close.index, name="return_at_touch"),
        barrier=pd.Series(barriers, index=close.index, name="barrier", dtype=object),
        ambiguous_count=ambiguous,
    )


def meta_labels(
    primary_side: pd.Series,
    labels: TripleBarrierLabels,
) -> pd.Series:
    """Binary labels for a meta-model: was the primary signal worth taking?

    Meta-labelling splits an intractable question into two easier ones. The
    primary model (here, the rule-based signal layer) decides *direction*; the
    meta-model only decides *whether to act*, which is a binary problem with a
    naturally balanced-ish target and a direct interpretation — the predicted
    probability is a position-sizing input.

    The practical reason to do it this way: a directional classifier trained on
    three-class barrier labels spends most of its capacity learning the
    unconditional drift, which is not what we need it for. Restricting it to
    "does this particular setup work" conditions on the setup and asks a much
    narrower question.

    Returns 1 where the primary side agreed with the realised barrier, 0 where
    it did not, and NaN where either is missing. Bars where the primary signal
    was flat are NaN, not 0: "no opinion" is not the same as "wrong".
    """
    side = primary_side.reindex(labels.label.index)
    outcome = labels.label
    agreed = (np.sign(side) == np.sign(outcome)) & (side != 0) & (outcome != 0)
    disagreed = (np.sign(side) != np.sign(outcome)) & (side != 0) & (outcome != 0)
    # A vertical-barrier outcome with a directional signal is a real "not worth
    # it": the trade was taken and went nowhere before time ran out.
    timed_out = (side != 0) & (outcome == 0)

    result = pd.Series(np.nan, index=labels.label.index, name="meta_label", dtype="float64")
    result[agreed] = 1.0
    result[disagreed | timed_out] = 0.0
    return result


def average_uniqueness(labels: TripleBarrierLabels) -> pd.Series:
    """How much of each label's window is not shared with other labels, in [0, 1].

    Overlapping labels are the reason a naive fit on bar-level data reports
    accuracy it does not have. On 15-minute bars with a 16-bar horizon, each
    observation shares its outcome window with sixteen neighbours, so the
    effective sample size is roughly a sixteenth of the row count. The model
    sees the same event many times and mistakes repetition for evidence.

    Used as sample weights, this restores something closer to the true sample
    size. It is López de Prado's construction: for each label, average across
    its window the reciprocal of the number of labels concurrently open.
    """
    resolved = labels.usable()
    if len(resolved) == 0:
        return pd.Series(dtype="float64", name="uniqueness")

    index = labels.label.index
    positions = {timestamp: position for position, timestamp in enumerate(index)}
    concurrency = np.zeros(len(index), dtype="float64")

    spans: list[tuple[int, int]] = []
    for timestamp in resolved:
        end = labels.touch_time.loc[timestamp]
        start_position = positions[timestamp]
        end_position = positions.get(end, start_position)
        # The label's window is (t0, t1], not [t0, t1]: entry happens at the
        # close of t0, so the outcome is determined entirely by bars after it.
        # Including t0 would make every pair of adjacent labels overlap by one
        # bar even at a one-bar horizon, understating uniqueness across the
        # board and over-shrinking the effective sample size.
        spans.append((start_position + 1, end_position))
        concurrency[start_position + 1 : end_position + 1] += 1.0

    weights = np.empty(len(spans), dtype="float64")
    for position, (start, end) in enumerate(spans):
        window = concurrency[start : end + 1]
        weights[position] = float((1.0 / window).mean()) if len(window) else float("nan")
    return pd.Series(weights, index=resolved, name="uniqueness")


def _record(
    labels: np.ndarray,
    touch_positions: np.ndarray,
    returns: np.ndarray,
    barriers: np.ndarray,
    start: int,
    touched: int,
    value: float,
    realised: float,
    barrier: Barrier,
) -> None:
    labels[start] = value
    touch_positions[start] = touched
    returns[start] = realised
    barriers[start] = barrier.value


def assert_no_label_leakage(features: pd.DataFrame, labels: TripleBarrierLabels) -> None:
    """Fail loudly if a label column has found its way into the feature matrix.

    Cheap, and it catches the mistake that is hardest to spot from the
    results: a leaked label does not look like a bug, it looks like success.
    """
    forbidden = {"label", "meta_label", "touch_time", "return_at_touch", "barrier"}
    present = forbidden.intersection(str(column) for column in features.columns)
    if present:
        raise ValueError(f"label columns present in the feature matrix: {sorted(present)}")

    for column in features.columns:
        series = features[column]
        if series.dtype.kind not in "fi":
            continue
        aligned = series.reindex(labels.label.index)
        overlap = aligned.notna() & labels.label.notna()
        if int(overlap.sum()) < 50:
            continue
        correlation = aligned[overlap].corr(labels.label[overlap])
        if pd.notna(correlation) and abs(float(correlation)) > 0.95:
            raise ValueError(
                f"feature {column!r} correlates {correlation:.3f} with the label. "
                "That is not a feature, that is the answer."
            )


def label_horizon_end(labels: TripleBarrierLabels) -> pd.Series:
    """``touch_time`` as tz-aware IST timestamps, for the splitter to purge on."""
    resolved = labels.usable()
    values = [ensure_ist(pd.Timestamp(t).to_pydatetime()) for t in labels.touch_time.loc[resolved]]
    return pd.Series(pd.to_datetime(values), index=resolved, name="horizon_end")
