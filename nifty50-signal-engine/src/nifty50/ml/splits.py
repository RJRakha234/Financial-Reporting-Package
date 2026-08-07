"""Purged walk-forward cross-validation.

Ordinary k-fold on a time series is not slightly wrong, it is catastrophically
wrong: it trains on next month and tests on last month. Everyone knows that.
The subtler failure — the one that survives switching to `TimeSeriesSplit` and
still inflates every reported score — is that a label is not resolved at the
bar it is stamped on.

A label at 10:00 with a three-hour barrier window is a statement about
10:00 to 13:00. If the test fold starts at 11:00, that training label already
contains an hour of test-period outcome. The chronological split looks clean;
the information is not. Purging removes exactly those overlapping training
labels.

Embargo handles the reverse direction. Features are built from trailing
windows, so a test observation at 11:00 shares serial correlation with
training observations shortly *after* the test fold ends. Dropping a short
band of training data after each test fold removes that residual coupling.

The cost of doing this is real and worth stating: purging and embargo throw
away training data, and the discarded rows cluster at fold boundaries where
regime changes are. Scores come out lower. That is the point — they come out
lower because the inflated part was never real.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class Fold:
    """One train/test split, plus the accounting of what purging removed."""

    number: int
    train: pd.Index
    test: pd.Index
    purged: int
    embargoed: int

    @property
    def train_size(self) -> int:
        return len(self.train)

    @property
    def test_size(self) -> int:
        return len(self.test)

    def describe(self) -> str:
        return (
            f"fold {self.number}: train {self.train_size:6d} "
            f"[{self.train.min()} .. {self.train.max()}]  "
            f"test {self.test_size:6d} [{self.test.min()} .. {self.test.max()}]  "
            f"purged {self.purged} embargoed {self.embargoed}"
        )


@dataclass(frozen=True, slots=True)
class PurgedWalkForward:
    """Walk-forward splitter that purges label-overlap and applies an embargo.

    ``folds`` test windows tile the sample after an initial training block of
    ``min_train_size`` observations. **Training data always precedes its test
    window.** That is the whole difference between this and the combinatorial
    purged CV it superficially resembles: the k-fold variant trains on data
    from both sides of the test window, which for the earliest fold means
    fitting on 2025 and predicting 2019.

    That variant has legitimate uses, but it is not what a production model
    experiences and it must not be described as walk-forward. A model that has
    seen the future's volatility regime, its typical bar ranges and its
    correlation structure is not making an out-of-sample prediction in any
    sense a live trader would recognise.

    Training is expanding by default — every fold trains on all history before
    it — which matches how the model would actually be retrained. Set
    ``max_train_size`` for a rolling window instead, which is the right choice
    if you believe the far past is a different market rather than more data.

    ``embargo_fraction`` is expressed as a share of the total sample, following
    the convention in the literature. Because training here is strictly
    one-sided, the embargo is a gap immediately *before* each test window
    rather than after it: it removes the training rows whose features share
    trailing windows with the first test observations.
    """

    folds: int = 5
    embargo_fraction: float = 0.01
    max_train_size: int | None = None
    min_train_size: int = 500

    def __post_init__(self) -> None:
        if self.folds < 2:
            raise ValueError("need at least two folds")
        if not 0.0 <= self.embargo_fraction < 0.5:
            raise ValueError("embargo_fraction must be in [0, 0.5)")
        if self.min_train_size < 1:
            raise ValueError("min_train_size must be positive")

    def split(
        self, index: pd.Index, horizon_end: pd.Series
    ) -> Iterator[Fold]:
        """Yield folds over ``index``, purging against ``horizon_end``.

        ``horizon_end`` maps each observation to the timestamp at which its
        label was resolved — :func:`nifty50.ml.labels.label_horizon_end`.
        There is no default and no fallback to "assume labels resolve
        instantly", because that assumption is the bug this class exists to
        prevent.
        """
        if not index.is_monotonic_increasing:
            raise ValueError("index must be sorted ascending before splitting")
        missing = index.difference(horizon_end.index)
        if len(missing) > 0:
            raise ValueError(
                f"{len(missing)} observations have no label horizon; "
                "purging cannot be performed without one"
            )

        total = len(index)
        if total < self.folds * 2:
            raise ValueError(f"{total} observations cannot support {self.folds} folds")

        end_values = horizon_end.reindex(index).to_numpy()
        start_values = np.asarray(index)
        embargo_bars = round(total * self.embargo_fraction)

        # Test windows tile only the portion after the initial training block,
        # so every fold has real history behind it rather than the first one
        # being skipped for having none.
        first_test = min(self.min_train_size, total - self.folds)
        if first_test < 1:
            raise ValueError(
                f"{total} observations cannot support min_train_size="
                f"{self.min_train_size} plus {self.folds} test windows"
            )
        boundaries = np.linspace(first_test, total, self.folds + 1, dtype="int64")

        for number in range(self.folds):
            test_start, test_stop = int(boundaries[number]), int(boundaries[number + 1])
            if test_stop - test_start < 1:
                continue
            test_index = index[test_start:test_stop]

            # Training is everything strictly before the test window. Nothing
            # after it, ever — that is what makes this walk-forward.
            candidate = np.zeros(total, dtype=bool)
            candidate[:test_start] = True

            # --- purge: drop training labels whose window reaches into the test
            test_first = start_values[test_start]
            overlaps_test = end_values >= test_first
            purge_mask = candidate & overlaps_test
            candidate &= ~overlaps_test

            # --- embargo: drop a band immediately before the test window
            embargo_mask = np.zeros(total, dtype=bool)
            if embargo_bars:
                embargo_start = max(0, test_start - embargo_bars)
                embargo_mask[embargo_start:test_start] = candidate[embargo_start:test_start]
                candidate[embargo_start:test_start] = False

            train_index = index[candidate]
            if self.max_train_size is not None and len(train_index) > self.max_train_size:
                train_index = train_index[-self.max_train_size :]

            if len(train_index) < self.min_train_size:
                # Early folds legitimately have too little history once purging
                # and the embargo have taken their share. Skipping is honest;
                # padding would not be.
                continue

            yield Fold(
                number=number,
                train=train_index,
                test=test_index,
                purged=int(purge_mask.sum()),
                embargoed=int(embargo_mask.sum()),
            )

    def report(self, index: pd.Index, horizon_end: pd.Series) -> str:
        folds = list(self.split(index, horizon_end))
        if not folds:
            return "No usable folds: every candidate fell below min_train_size."
        lines = [f"PurgedWalkForward: {len(folds)} usable folds of {self.folds} requested"]
        lines += [f"  {fold.describe()}" for fold in folds]
        total_purged = sum(fold.purged for fold in folds)
        total_embargoed = sum(fold.embargoed for fold in folds)
        lines.append(
            f"  removed {total_purged} overlapping and {total_embargoed} embargoed "
            f"training observations across all folds"
        )
        if total_purged == 0:
            lines.append(
                "  NOTE: nothing was purged. Either labels resolve within one bar, "
                "or horizon_end is wrong. Check before trusting these scores."
            )
        return "\n".join(lines)


def train_test_by_date(
    index: pd.Index, horizon_end: pd.Series, *, split_at: pd.Timestamp, embargo_bars: int = 0
) -> Fold:
    """A single purged split at a fixed date — the final out-of-sample holdout.

    Distinct from cross-validation by intent. The CV folds are used to choose
    hyperparameters, which means every score they produce has been selected on
    and is therefore optimistic. This split is touched once, at the end, and
    the number it produces is the only one that should be quoted without a
    multiple-testing caveat.

    Keeping that discipline is a matter of not calling this function twice.
    Nothing in the code can enforce it.
    """
    if not index.is_monotonic_increasing:
        raise ValueError("index must be sorted ascending")
    if isinstance(index, pd.DatetimeIndex) and (index.tz is None) != (split_at.tz is None):
        raise ValueError(
            f"split_at={split_at!r} and the index disagree on timezone awareness. "
            "Comparing them would either raise or silently reinterpret IST as UTC."
        )
    # index.searchsorted, not np.searchsorted: converting a tz-aware index
    # through numpy drops the offset, which shifts every boundary by 5h30m.
    boundary = int(index.searchsorted(split_at))
    if boundary == 0 or boundary >= len(index):
        raise ValueError(f"split_at={split_at} leaves one side of the split empty")

    ends = horizon_end.reindex(index).to_numpy()
    test_index = index[boundary:]
    test_first = np.asarray(index)[boundary]

    candidate = np.zeros(len(index), dtype=bool)
    candidate[:boundary] = True
    overlaps = ends >= test_first
    purged = int((candidate & overlaps).sum())
    candidate &= ~overlaps

    embargoed = 0
    if embargo_bars:
        cut = max(0, boundary - purged - embargo_bars)
        embargoed = int(candidate[cut:].sum())
        candidate[cut:] = False

    return Fold(
        number=0,
        train=index[candidate],
        test=test_index,
        purged=purged,
        embargoed=embargoed,
    )
