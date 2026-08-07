"""The classifier, and the guard rails around it.

Model choice: gradient-boosted trees, specifically scikit-learn's
``HistGradientBoostingClassifier``. Three reasons, in order of importance.

*It handles NaN natively.* The feature matrix is roughly a fifth NaN by
construction — every indicator has a warm-up, and higher-timeframe columns are
undefined until the first higher bar closes. Imputing those would invent data;
dropping the rows would discard the early part of every symbol's history.
Boosted trees learn a default direction for missing values, which is the only
option here that neither fabricates nor discards.

*It is invariant to monotone feature transforms.* Roughly half the feature set
is bounded (RSI, percentile ranks, band positions) and half is unbounded (ATR,
OBV slope, volume z-scores). A linear model would need those on a common scale,
and every scaling choice is another parameter fitted on data.

*It is the model whose failure modes are best understood.* This is not a
problem where a larger model helps: the signal-to-noise ratio in 15-minute
equity returns is low enough that extra capacity buys memorisation, not skill.

What is deliberately absent: no neural network, no stacking, no automated
hyperparameter search over hundreds of configurations. Each of those multiplies
the number of effective trials, and every trial spent is Sharpe that has to be
deflated back out later. The default configuration below is close to
scikit-learn's, chosen once and left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

# Deliberately conservative: shallow trees, strong regularisation, early
# stopping off (it would need a validation split that purging must respect,
# and doing that wrong is worse than not doing it).
_DEFAULT_PARAMS: dict[str, Any] = {
    "max_depth": 3,
    "max_iter": 200,
    "learning_rate": 0.05,
    "min_samples_leaf": 100,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 7,
}


@dataclass(slots=True)
class SignalClassifier:
    """A calibrated three-class classifier over the feature matrix.

    Classes are the triple-barrier labels: ``-1`` (stop hit first), ``0``
    (timed out), ``+1`` (target hit first).

    Calibration is not decoration. The decision layer converts a probability
    into a confidence score and a position size, so a model that says 0.8 had
    better be right about 80% of the time. Boosted trees are systematically
    overconfident out of the box; isotonic regression on a held-out inner slice
    corrects that, at the cost of some training data.
    """

    params: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    calibration_fraction: float = 0.2
    feature_names: tuple[str, ...] = ()
    classes: tuple[int, ...] = ()
    _model: HistGradientBoostingClassifier | None = None
    _calibrators: dict[int, IsotonicRegression] = field(default_factory=dict)

    @property
    def is_fitted(self) -> bool:
        return self._model is not None

    def fit(
        self,
        features: pd.DataFrame,
        labels: pd.Series,
        *,
        sample_weight: pd.Series | None = None,
    ) -> SignalClassifier:
        """Fit on ``features``/``labels``, holding out a tail slice for calibration.

        The calibration slice is the *most recent* fraction of the training
        data, not a random one. A random slice would be interleaved with the
        training rows and share their label windows, so the calibrator would be
        fitted on predictions the model had effectively already seen — the same
        overlap problem purging exists to solve, reappearing one level down.
        """
        if features.empty:
            raise ValueError("cannot fit on an empty feature matrix")
        aligned_labels = labels.reindex(features.index)
        usable = aligned_labels.notna()
        if not usable.any():
            raise ValueError("no labelled rows in the training set")

        matrix = features[usable]
        targets = aligned_labels[usable].astype("int64")
        weights = (
            sample_weight.reindex(matrix.index).fillna(0.0).to_numpy(dtype="float64")
            if sample_weight is not None
            else None
        )
        if len(np.unique(targets)) < 2:
            raise ValueError(
                f"training labels contain a single class ({targets.iloc[0]}); "
                "there is nothing to learn"
            )

        boundary = int(len(matrix) * (1.0 - self.calibration_fraction))
        boundary = max(boundary, 1)
        fit_slice = slice(0, boundary)
        calibrate_slice = slice(boundary, len(matrix))

        model = HistGradientBoostingClassifier(**self.params)
        model.fit(
            matrix.iloc[fit_slice].to_numpy(dtype="float64"),
            targets.iloc[fit_slice].to_numpy(),
            sample_weight=weights[fit_slice] if weights is not None else None,
        )

        self._model = model
        self.feature_names = tuple(str(column) for column in features.columns)
        self.classes = tuple(int(value) for value in model.classes_)
        self._calibrators = {}

        held_out = matrix.iloc[calibrate_slice]
        if len(held_out) >= 50 and len(np.unique(targets.iloc[calibrate_slice])) >= 2:
            self._fit_calibrators(held_out, targets.iloc[calibrate_slice])
        return self

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        """Calibrated class probabilities, indexed like ``features``."""
        model = self._require_model()
        self._check_columns(features)
        raw = model.predict_proba(features.to_numpy(dtype="float64"))
        frame = pd.DataFrame(raw, index=features.index, columns=list(self.classes))

        if self._calibrators:
            for class_value, calibrator in self._calibrators.items():
                frame[class_value] = calibrator.predict(frame[class_value].to_numpy())
            # Isotonic is fitted per class and does not preserve the simplex;
            # renormalising is the standard fix and keeps the output a
            # probability distribution rather than three loose scores.
            row_totals = frame.sum(axis=1).replace(0.0, np.nan)
            frame = frame.div(row_totals, axis=0)
        return frame

    def predict(self, features: pd.DataFrame) -> pd.Series:
        """Most likely class. Rarely what you want — see the note.

        The decision layer should use :meth:`predict_proba`, not this. Taking
        the argmax discards exactly the information that distinguishes a 0.9
        conviction from a 0.35 three-way tie, and on a low-signal problem most
        predictions are near-ties.
        """
        probabilities = self.predict_proba(features)
        return probabilities.idxmax(axis=1).astype("int64").rename("prediction")

    def directional_edge(self, features: pd.DataFrame) -> pd.Series:
        """``P(up) - P(down)``, in [-1, 1].

        The single number the decision layer actually consumes. It is
        deliberately *not* ``P(up)``: a setup with 40% up, 20% down, 40% flat
        is a real long edge, and one with 40% up, 40% down, 20% flat is a
        coin flip with wide barriers. Only the difference distinguishes them.
        """
        probabilities = self.predict_proba(features)
        zeros = pd.Series(0.0, index=features.index)
        up = probabilities[1] if 1 in probabilities.columns else zeros
        down = probabilities[-1] if -1 in probabilities.columns else zeros
        edge: pd.Series = up - down
        return edge.rename("directional_edge")

    def permutation_importance(
        self,
        features: pd.DataFrame,
        labels: pd.Series,
        *,
        repeats: int = 5,
        seed: int = 11,
    ) -> pd.DataFrame:
        """Out-of-sample permutation importance, in log-loss units.

        Measured on data the model has not seen, because in-sample importance
        answers "what did the model use" rather than "what actually helps",
        and on a noisy problem those diverge sharply.

        Correlated features share credit and each will look unimportant alone —
        the EMA ribbon columns are near-collinear by construction, so read this
        as a group, not a ranking.
        """
        from nifty50.ml.evaluate import multiclass_log_loss

        self._require_model()
        aligned = labels.reindex(features.index)
        usable = aligned.notna()
        matrix, targets = features[usable], aligned[usable].astype("int64")
        if matrix.empty:
            raise ValueError("no labelled rows to measure importance on")

        baseline = multiclass_log_loss(targets, self.predict_proba(matrix))
        generator = np.random.default_rng(seed)
        records: list[dict[str, float | str]] = []
        for column in matrix.columns:
            deltas = np.empty(repeats, dtype="float64")
            original = matrix[column].to_numpy(copy=True)
            for repeat in range(repeats):
                shuffled = matrix.copy()
                shuffled[column] = generator.permutation(original)
                deltas[repeat] = (
                    multiclass_log_loss(targets, self.predict_proba(shuffled)) - baseline
                )
            records.append(
                {
                    "feature": str(column),
                    "log_loss_increase": float(deltas.mean()),
                    "std": float(deltas.std(ddof=1)) if repeats > 1 else 0.0,
                }
            )
        frame = pd.DataFrame(records).set_index("feature")
        return frame.sort_values("log_loss_increase", ascending=False)

    def _fit_calibrators(self, features: pd.DataFrame, targets: pd.Series) -> None:
        model = self._require_model()
        raw = model.predict_proba(features.to_numpy(dtype="float64"))
        for position, class_value in enumerate(self.classes):
            observed = (targets.to_numpy() == class_value).astype("float64")
            calibrator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
            calibrator.fit(raw[:, position], observed)
            self._calibrators[class_value] = calibrator

    def _require_model(self) -> HistGradientBoostingClassifier:
        if self._model is None:
            raise RuntimeError("classifier has not been fitted")
        return self._model

    def _check_columns(self, features: pd.DataFrame) -> None:
        incoming = tuple(str(column) for column in features.columns)
        if incoming != self.feature_names:
            missing = set(self.feature_names) - set(incoming)
            extra = set(incoming) - set(self.feature_names)
            raise ValueError(
                "feature matrix does not match the fitted one "
                f"(missing={sorted(missing)}, unexpected={sorted(extra)}). "
                "Silently reordering columns here would produce predictions "
                "that look plausible and mean nothing."
            )


@dataclass(frozen=True, slots=True)
class MajorityBaseline:
    """Always predicts the training-set class frequencies.

    Every model score in this project is reported against this. A three-class
    classifier that is 55% accurate sounds respectable until you notice the
    vertical barrier accounts for 54% of labels, at which point it has learned
    nothing at all.
    """

    frequencies: dict[int, float]

    @classmethod
    def fit(cls, labels: pd.Series) -> MajorityBaseline:
        counts = labels.dropna().astype("int64").value_counts(normalize=True)
        return cls(frequencies={int(str(k)): float(v) for k, v in counts.items()})

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        columns = sorted(self.frequencies)
        data = {column: np.full(len(features), self.frequencies[column]) for column in columns}
        return pd.DataFrame(data, index=features.index)
