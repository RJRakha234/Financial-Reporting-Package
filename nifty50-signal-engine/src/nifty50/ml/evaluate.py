"""Out-of-sample evaluation, reported so that the unflattering numbers are unavoidable.

Accuracy is not reported alone anywhere in this module, and that is deliberate.
On triple-barrier labels with a wide vertical barrier, "always predict flat"
scores in the mid-fifties. A model that beats it by two points has learned
almost nothing, and an accuracy figure quoted without its baseline conceals
exactly that.

What is reported instead, always together:

* **log loss**, against the majority-frequency baseline. This is the honest
  headline: it punishes confident errors, which is the failure mode that costs
  money.
* **expected calibration error**, because the decision layer sizes positions
  off these probabilities and a miscalibrated 0.8 is worse than no model.
* **a permutation p-value**, because with enough features, folds and symbols,
  something will look good by chance.
* **per-class precision and recall**, because a model can beat the baseline on
  aggregate log loss while being useless on the only class you would trade.

The permutation test answers a narrow question: given these predictions and
these labels, could the association have arisen by chance? It holds the fitted
model fixed and shuffles the test labels. It does not test whether the *whole
pipeline* — feature selection, hyperparameters, fold layout — was overfitted.
Nothing here can test that; only a holdout that has been looked at exactly once
can, which is what :func:`nifty50.ml.splits.train_test_by_date` is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nifty50.ml.model import MajorityBaseline, SignalClassifier
from nifty50.ml.splits import Fold, PurgedWalkForward

# Probabilities are clipped before taking logs. Without it a single confident
# miss returns inf and the whole score becomes uninterpretable.
_PROBABILITY_FLOOR: float = 1e-12


def multiclass_log_loss(labels: pd.Series, probabilities: pd.DataFrame) -> float:
    """Mean negative log-likelihood of the observed class."""
    aligned = probabilities.reindex(labels.index)
    if aligned.empty:
        return float("nan")
    return _log_loss_from_matrix(
        _class_positions(labels, list(aligned.columns)),
        aligned.to_numpy(dtype="float64"),
    )


def _class_positions(labels: pd.Series, columns: list[object]) -> np.ndarray:
    """Column index of each observed class; ``-1`` for a class with no column."""
    lookup = {int(str(column)): position for position, column in enumerate(columns)}
    return np.array([lookup.get(int(value), -1) for value in labels.to_numpy()], dtype="int64")


def _log_loss_from_matrix(positions: np.ndarray, matrix: np.ndarray) -> float:
    """Vectorised inner loop, so the permutation test is not O(n) Python per shuffle."""
    rows = np.arange(len(positions))
    safe = np.clip(positions, 0, matrix.shape[1] - 1)
    chosen = np.where(positions >= 0, matrix[rows, safe], _PROBABILITY_FLOOR)
    return float(-np.log(np.maximum(chosen, _PROBABILITY_FLOOR)).mean())


def expected_calibration_error(
    labels: pd.Series, probabilities: pd.DataFrame, *, bins: int = 10
) -> float:
    """Weighted mean gap between predicted confidence and observed frequency.

    Computed on the top-class prediction. 0.0 is perfect; above roughly 0.05 the
    probabilities should not be used for sizing.
    """
    aligned = probabilities.reindex(labels.index)
    if aligned.empty:
        return float("nan")
    confidence = aligned.max(axis=1).to_numpy(dtype="float64")
    predicted = aligned.idxmax(axis=1).to_numpy()
    correct = (predicted == labels.to_numpy()).astype("float64")

    edges = np.linspace(0.0, 1.0, bins + 1)
    membership = np.clip(np.digitize(confidence, edges[1:-1], right=True), 0, bins - 1)
    total = 0.0
    for bin_number in range(bins):
        selected = membership == bin_number
        count = int(selected.sum())
        if count == 0:
            continue
        gap = abs(confidence[selected].mean() - correct[selected].mean())
        total += gap * count / len(confidence)
    return float(total)


def permutation_pvalue(
    labels: pd.Series, probabilities: pd.DataFrame, *, permutations: int = 500, seed: int = 13
) -> float:
    """Share of label shufflings whose log loss is at least as good as the real one.

    Labels are shuffled in contiguous blocks rather than independently. Bar
    labels are strongly autocorrelated — neighbouring bars usually share an
    outcome — and an i.i.d. shuffle destroys that structure, producing a null
    that is far too easy to beat and a p-value that is far too small.
    """
    aligned = probabilities.reindex(labels.index)
    if aligned.empty or len(labels) < 20:
        return float("nan")
    matrix = aligned.to_numpy(dtype="float64")
    positions = _class_positions(labels, list(aligned.columns))
    observed = _log_loss_from_matrix(positions, matrix)
    if not np.isfinite(observed):
        return float("nan")

    # Shuffle the resolved column positions rather than the labels themselves:
    # identical statistic, but it skips rebuilding a Series and re-resolving
    # the class lookup on every one of the permutations.
    block = max(1, len(positions) // 50)
    blocks = [positions[start : start + block] for start in range(0, len(positions), block)]
    generator = np.random.default_rng(seed)

    at_least_as_good = 0
    for _ in range(permutations):
        order = generator.permutation(len(blocks))
        shuffled = np.concatenate([blocks[position] for position in order])[: len(positions)]
        if _log_loss_from_matrix(shuffled, matrix) <= observed:
            at_least_as_good += 1
    # +1 in both places: the observed arrangement is itself one of the possible
    # outcomes, and omitting it can report p = 0, which is never true.
    return (at_least_as_good + 1) / (permutations + 1)


@dataclass(frozen=True, slots=True)
class DirectionalSkill:
    """Does the model know *which way*, as opposed to *whether anything happens*?

    This exists because of a failure found by the control run on synthetic
    data, and it is worth stating plainly because the failure is invisible in
    every conventional metric.

    A three-class barrier model can post a large, highly significant log-loss
    improvement while carrying no directional information at all. Two features
    are enough to do it. ``bar_of_session`` predicts whether the vertical
    barrier will truncate the label — late in a session it always does — and
    any volatility measure predicts whether the barriers get hit at all. Both
    are questions about the *labelling scheme*, not about the market. On a
    geometric random walk the model reached +12% log loss at p = 0.002 on
    exactly this basis, with a directional correlation of -0.03.

    So direction is scored separately, on the subset where a directional
    barrier was actually hit, and the verdict depends on it. A model that
    cannot beat the majority direction has nothing to trade on, whatever its
    aggregate log loss says.
    """

    observations: int
    accuracy: float
    baseline_accuracy: float
    correlation: float

    @property
    def has_skill(self) -> bool:
        return bool(
            self.observations >= 250
            and np.isfinite(self.accuracy)
            and self.accuracy > self.baseline_accuracy
        )

    def describe(self) -> str:
        return "\n".join(
            [
                f"Directional skill (on the {self.observations} bars where a "
                f"directional barrier was hit)",
                f"{'  direction accuracy':30s}{self.accuracy:10.2%}",
                f"{'  majority-direction baseline':30s}{self.baseline_accuracy:10.2%}",
                f"{'  corr(edge, outcome)':30s}{self.correlation:+10.4f}",
            ]
        )


@dataclass(frozen=True, slots=True)
class ClassificationReport:
    """Out-of-sample scores, always alongside the baseline they must beat."""

    observations: int
    log_loss: float
    baseline_log_loss: float
    accuracy: float
    baseline_accuracy: float
    calibration_error: float
    p_value: float
    per_class: pd.DataFrame
    directional: DirectionalSkill
    notes: tuple[str, ...] = ()

    @property
    def log_loss_improvement(self) -> float:
        """Fraction by which the model beats the baseline. Negative means worse."""
        if not np.isfinite(self.baseline_log_loss) or self.baseline_log_loss == 0.0:
            return float("nan")
        return (self.baseline_log_loss - self.log_loss) / self.baseline_log_loss

    @property
    def beats_baseline(self) -> bool:
        return bool(np.isfinite(self.log_loss) and self.log_loss < self.baseline_log_loss)

    @property
    def is_tradeable(self) -> bool:
        """Beats the baseline, is not attributable to chance, *and* knows direction.

        All three, because the first two alone are satisfiable with no
        directional information whatsoever — see :class:`DirectionalSkill`.
        """
        return bool(
            self.beats_baseline and self.p_value < 0.05 and self.directional.has_skill
        )

    def describe(self) -> str:
        lines = [
            f"Out-of-sample classification report ({self.observations} observations)",
            "",
            f"{'':22s}{'model':>12s}{'baseline':>12s}",
            f"{'log loss':22s}{self.log_loss:12.4f}{self.baseline_log_loss:12.4f}",
            f"{'accuracy':22s}{self.accuracy:12.2%}{self.baseline_accuracy:12.2%}",
            "",
            f"log-loss improvement over baseline : {self.log_loss_improvement:+.2%}",
            f"expected calibration error         : {self.calibration_error:.4f}",
            f"permutation p-value                : {self.p_value:.4f}",
            "",
            "Per class:",
            self.per_class.to_string(float_format=lambda v: f"{v:.3f}"),
            "",
            self.directional.describe(),
        ]
        # Every failed check is reported, not just the first. The checks are
        # independent, and a model can fail two of them for unrelated reasons;
        # short-circuiting on the first would hide the directional finding
        # behind a p-value and vice versa.
        failures: list[str] = []
        if not self.beats_baseline:
            failures.append(
                "Does not beat a constant-frequency baseline out of sample. It has "
                "no demonstrated skill and must not be used to size positions."
            )
        elif self.p_value > 0.05:
            failures.append(
                "Beats the baseline, but the improvement is not distinguishable "
                f"from chance (p={self.p_value:.3f}). Treat it as unproven."
            )
        if not self.directional.has_skill:
            failures.append(
                "NO DIRECTIONAL SKILL. Whatever the aggregate log loss says, this "
                "model is predicting whether a barrier gets hit, not which one — a "
                "statement about volatility and time-of-session, and there is no "
                "trade in it."
            )

        lines.append("")
        if failures:
            lines += [f"VERDICT: {failures[0]}"]
            lines += [f"         {failure}" for failure in failures[1:]]
        else:
            lines += [
                "VERDICT: beats the baseline, survives the permutation test, and "
                "carries directional information. That is necessary, not "
                "sufficient — it says nothing about profit after costs."
            ]
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


def evaluate(
    labels: pd.Series,
    probabilities: pd.DataFrame,
    baseline: pd.DataFrame,
    *,
    permutations: int = 500,
) -> ClassificationReport:
    """Score predictions against the labels and against a baseline."""
    aligned = labels.dropna().astype("int64")
    model_probabilities = probabilities.reindex(aligned.index)
    baseline_probabilities = baseline.reindex(aligned.index)

    predicted = model_probabilities.idxmax(axis=1)
    baseline_predicted = baseline_probabilities.idxmax(axis=1)

    notes: list[str] = []
    if len(aligned) < 250:
        notes.append(
            f"{len(aligned)} out-of-sample observations is too few for these "
            "figures to be stable; treat them as directional only."
        )
    if aligned.nunique() < 2:
        notes.append("the out-of-sample period contains a single class.")

    directional = directional_skill(aligned, model_probabilities)
    if directional.observations < 250:
        notes.append(
            f"only {directional.observations} bars resolved directionally; "
            "the direction figures are not stable."
        )

    return ClassificationReport(
        observations=len(aligned),
        log_loss=multiclass_log_loss(aligned, model_probabilities),
        baseline_log_loss=multiclass_log_loss(aligned, baseline_probabilities),
        accuracy=float((predicted == aligned).mean()),
        baseline_accuracy=float((baseline_predicted == aligned).mean()),
        calibration_error=expected_calibration_error(aligned, model_probabilities),
        p_value=permutation_pvalue(aligned, model_probabilities, permutations=permutations),
        per_class=_per_class_table(aligned, predicted),
        directional=directional,
        notes=tuple(notes),
    )


def directional_skill(labels: pd.Series, probabilities: pd.DataFrame) -> DirectionalSkill:
    """Score up-versus-down on the bars where a directional barrier was hit.

    Vertical-barrier bars are excluded: the model saying "nothing will happen"
    is a volatility call, and folding it in here would reintroduce exactly the
    contamination this measurement exists to remove.

    The baseline is the majority direction, which on asymmetric barriers is
    strongly lopsided — with a 1x stop against a 2x target, the stop is hit
    first roughly four times in five. A model that predicts "down" every time
    scores 80% and knows nothing, so the comparison against this baseline is
    the whole measurement.
    """
    aligned = probabilities.reindex(labels.index)
    resolved = labels[labels != 0]
    if len(resolved) == 0 or aligned.empty:
        return DirectionalSkill(0, float("nan"), float("nan"), float("nan"))

    up = aligned[1] if 1 in aligned.columns else pd.Series(0.0, index=aligned.index)
    down = aligned[-1] if -1 in aligned.columns else pd.Series(0.0, index=aligned.index)
    edge = (up - down).reindex(resolved.index)
    truth = pd.Series(np.sign(resolved.to_numpy(dtype="float64")), index=resolved.index)

    majority = float(max((truth == 1).mean(), (truth == -1).mean()))
    # A constant edge — which a degenerate or perfectly-calibrated-to-the-prior
    # model produces — has zero variance, and correlating it warns and returns
    # NaN. NaN is the right answer; the warning is noise.
    correlation = edge.corr(truth) if edge.std(ddof=0) > 0.0 else float("nan")
    return DirectionalSkill(
        observations=len(resolved),
        accuracy=float((np.sign(edge) == truth).mean()),
        baseline_accuracy=majority,
        correlation=float(correlation) if pd.notna(correlation) else float("nan"),
    )


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Per-fold out-of-sample predictions, stitched into one continuous series.

    ``report`` scores the stitched predictions rather than averaging per-fold
    scores. Averaging would weight a 200-observation fold equally with a
    20,000-observation one, and the small folds are the early ones where the
    model has least training data — precisely the ones whose noisy scores would
    dominate an unweighted mean.
    """

    probabilities: pd.DataFrame
    labels: pd.Series
    report: ClassificationReport
    folds: tuple[Fold, ...] = ()
    fold_log_losses: tuple[float, ...] = ()
    _diagnostics: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        lines = [self.report.describe(), "", "Per-fold log loss (stability check):"]
        for fold, score in zip(self.folds, self.fold_log_losses, strict=True):
            lines.append(f"  fold {fold.number}: {score:.4f}  ({fold.test_size} observations)")
        if len(self.fold_log_losses) > 1:
            spread = max(self.fold_log_losses) - min(self.fold_log_losses)
            lines.append(f"  spread across folds: {spread:.4f}")
            if spread > 0.15:
                lines.append(
                    "  The spread is wide enough that the aggregate score is an "
                    "average of quite different regimes, not a stable estimate."
                )
        lines += [f"  {note}" for note in self._diagnostics]
        return "\n".join(lines)


def walk_forward_evaluate(
    features: pd.DataFrame,
    labels: pd.Series,
    horizon_end: pd.Series,
    splitter: PurgedWalkForward,
    *,
    sample_weight: pd.Series | None = None,
    permutations: int = 500,
) -> WalkForwardResult:
    """Fit and score across purged walk-forward folds.

    A fresh classifier is fitted per fold. Reusing one would carry the previous
    fold's fit — and therefore later data — into earlier tests, which is the
    same leak the splitter exists to prevent, arriving by a different door.
    """
    ordered = features.sort_index()
    aligned_labels = labels.reindex(ordered.index)
    labelled = ordered.index[aligned_labels.notna()]
    if len(labelled) == 0:
        raise ValueError("no labelled observations to evaluate")

    ordered = ordered.loc[labelled]
    aligned_labels = aligned_labels.loc[labelled].astype("int64")

    collected: list[pd.DataFrame] = []
    used_folds: list[Fold] = []
    fold_scores: list[float] = []
    diagnostics: list[str] = []

    for fold in splitter.split(ordered.index, horizon_end):
        train_labels = aligned_labels.loc[fold.train]
        if train_labels.nunique() < 2:
            diagnostics.append(f"fold {fold.number} skipped: training set has one class")
            continue
        classifier = SignalClassifier()
        classifier.fit(
            ordered.loc[fold.train],
            train_labels,
            sample_weight=sample_weight,
        )
        predicted = classifier.predict_proba(ordered.loc[fold.test])
        collected.append(predicted)
        used_folds.append(fold)
        fold_scores.append(multiclass_log_loss(aligned_labels.loc[fold.test], predicted))

    if not collected:
        raise ValueError("no fold produced predictions; check fold sizing and label balance")

    probabilities = pd.concat(collected).sort_index()
    scored_labels = aligned_labels.reindex(probabilities.index)
    baseline = MajorityBaseline.fit(aligned_labels).predict_proba(probabilities)

    return WalkForwardResult(
        probabilities=probabilities,
        labels=scored_labels,
        report=evaluate(scored_labels, probabilities, baseline, permutations=permutations),
        folds=tuple(used_folds),
        fold_log_losses=tuple(fold_scores),
        _diagnostics=tuple(diagnostics),
    )


def _per_class_table(labels: pd.Series, predicted: pd.Series) -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for class_value in sorted(labels.unique()):
        actual = labels == class_value
        called = predicted == class_value
        true_positive = float((actual & called).sum())
        precision = true_positive / float(called.sum()) if called.any() else float("nan")
        recall = true_positive / float(actual.sum()) if actual.any() else float("nan")
        rows.append(
            {
                "class": float(class_value),
                "support": float(actual.sum()),
                "predicted": float(called.sum()),
                "precision": precision,
                "recall": recall,
            }
        )
    return pd.DataFrame(rows).set_index("class")
