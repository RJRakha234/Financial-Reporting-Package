"""The classifier, and the two tests that actually matter.

A model that finds an edge in a planted signal proves the plumbing works. A
model that finds *no* edge in pure noise proves the evaluation is honest, and
that is the harder and more important property: a pipeline with a leak passes
the first test easily and fails the second, which is why the noise test is
here and why its assertions are on the out-of-sample report rather than on the
fit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nifty50.ml.evaluate import (
    directional_skill,
    evaluate,
    expected_calibration_error,
    multiclass_log_loss,
    permutation_pvalue,
    walk_forward_evaluate,
)
from nifty50.ml.labels import label_horizon_end, triple_barrier_labels
from nifty50.ml.model import MajorityBaseline, SignalClassifier
from nifty50.ml.splits import PurgedWalkForward

SIZE = 3000
INDEX = pd.date_range("2024-01-01 09:15", periods=SIZE, freq="15min", tz="Asia/Kolkata")


def noise_dataset(seed: int = 0) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Features and labels with no relationship whatsoever."""
    rng = np.random.default_rng(seed)
    features = pd.DataFrame(
        rng.normal(size=(SIZE, 8)),
        index=INDEX,
        columns=[f"feature_{i}" for i in range(8)],
    )
    labels = pd.Series(rng.choice([-1, 0, 1], size=SIZE, p=[0.25, 0.5, 0.25]), index=INDEX)
    # Labels resolve 10 bars later, as a triple-barrier label would.
    horizon = pd.Series(INDEX[np.minimum(np.arange(SIZE) + 10, SIZE - 1)], index=INDEX)
    return features, labels, horizon


def signal_dataset(seed: int = 1, strength: float = 1.6) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """One informative feature buried among seven irrelevant ones."""
    rng = np.random.default_rng(seed)
    informative = rng.normal(size=SIZE)
    features = pd.DataFrame(
        rng.normal(size=(SIZE, 8)),
        index=INDEX,
        columns=[f"feature_{i}" for i in range(8)],
    )
    features["feature_0"] = informative

    logits = strength * informative + rng.normal(scale=0.5, size=SIZE)
    labels = pd.Series(
        np.where(logits > 0.8, 1, np.where(logits < -0.8, -1, 0)), index=INDEX
    )
    horizon = pd.Series(INDEX[np.minimum(np.arange(SIZE) + 10, SIZE - 1)], index=INDEX)
    return features, labels, horizon


class TestClassifier:
    def test_it_learns_a_planted_signal(self) -> None:
        features, labels, _ = signal_dataset()
        train, test = slice(0, 2000), slice(2100, SIZE)
        model = SignalClassifier().fit(features.iloc[train], labels.iloc[train])

        probabilities = model.predict_proba(features.iloc[test])
        baseline = MajorityBaseline.fit(labels.iloc[train]).predict_proba(features.iloc[test])
        assert multiclass_log_loss(labels.iloc[test], probabilities) < multiclass_log_loss(
            labels.iloc[test], baseline
        )

    def test_probabilities_sum_to_one(self) -> None:
        features, labels, _ = signal_dataset()
        model = SignalClassifier().fit(features.iloc[:2000], labels.iloc[:2000])
        totals = model.predict_proba(features.iloc[2000:]).sum(axis=1)
        assert totals.max() == pytest.approx(1.0)
        assert totals.min() == pytest.approx(1.0)

    def test_directional_edge_separates_conviction_from_a_coin_flip(self) -> None:
        features, labels, _ = signal_dataset()
        model = SignalClassifier().fit(features.iloc[:2000], labels.iloc[:2000])
        edge = model.directional_edge(features.iloc[2000:])
        assert edge.between(-1.0, 1.0).all()
        # The edge should track the informative feature.
        assert edge.corr(features["feature_0"].iloc[2000:]) > 0.5

    def test_a_reordered_feature_matrix_is_refused_not_silently_accepted(self) -> None:
        features, labels, _ = signal_dataset()
        model = SignalClassifier().fit(features.iloc[:2000], labels.iloc[:2000])
        shuffled = features.iloc[2000:][list(reversed(features.columns))]
        with pytest.raises(ValueError, match="does not match"):
            model.predict_proba(shuffled)

    def test_a_single_class_training_set_is_refused(self) -> None:
        features, _, _ = signal_dataset()
        constant = pd.Series(np.zeros(SIZE, dtype="int64"), index=INDEX)
        with pytest.raises(ValueError, match="single class"):
            SignalClassifier().fit(features.iloc[:2000], constant.iloc[:2000])

    def test_predicting_before_fitting_raises(self) -> None:
        features, _, _ = signal_dataset()
        with pytest.raises(RuntimeError, match="not been fitted"):
            SignalClassifier().predict_proba(features)

    def test_nan_features_are_handled_rather_than_dropped(self) -> None:
        features, labels, _ = signal_dataset()
        # Simulate indicator warm-up: the first 200 rows have missing columns.
        holed = features.copy()
        holed.iloc[:200, 1:4] = np.nan
        model = SignalClassifier().fit(holed.iloc[:2000], labels.iloc[:2000])
        predictions = model.predict_proba(holed.iloc[2000:])
        assert len(predictions) == len(holed.iloc[2000:])
        assert predictions.notna().all().all()

    def test_permutation_importance_finds_the_informative_feature(self) -> None:
        features, labels, _ = signal_dataset()
        model = SignalClassifier().fit(features.iloc[:2000], labels.iloc[:2000])
        importance = model.permutation_importance(
            features.iloc[2000:], labels.iloc[2000:], repeats=3
        )
        assert importance.index[0] == "feature_0"
        assert importance.iloc[0]["log_loss_increase"] > 0.0


class TestHonestEvaluation:
    def test_pure_noise_does_not_beat_the_baseline(self) -> None:
        features, labels, horizon = noise_dataset()
        splitter = PurgedWalkForward(folds=4, embargo_fraction=0.01, min_train_size=300)
        result = walk_forward_evaluate(
            features, labels, horizon, splitter, permutations=200
        )
        # This is the assertion that would catch a leak anywhere upstream.
        assert not result.report.beats_baseline
        assert "no demonstrated skill" in result.report.describe()
        assert not result.report.is_tradeable

    def test_a_planted_signal_does_beat_the_baseline(self) -> None:
        features, labels, horizon = signal_dataset()
        splitter = PurgedWalkForward(folds=4, embargo_fraction=0.01, min_train_size=300)
        result = walk_forward_evaluate(
            features, labels, horizon, splitter, permutations=200
        )
        assert result.report.beats_baseline
        assert result.report.log_loss_improvement > 0.05
        assert result.report.p_value < 0.05

    def test_the_report_shows_the_baseline_alongside_every_score(self) -> None:
        features, labels, horizon = signal_dataset()
        splitter = PurgedWalkForward(folds=3, embargo_fraction=0.01, min_train_size=300)
        text = walk_forward_evaluate(features, labels, horizon, splitter, permutations=50).describe()
        # Accuracy must never appear without the number it has to beat.
        assert "baseline" in text
        assert "accuracy" in text
        assert "permutation p-value" in text
        assert "Per-fold log loss" in text

    def test_a_model_that_only_predicts_volatility_is_called_out(self) -> None:
        """The failure the control run on synthetic data actually produced.

        Labels here are asymmetric (mostly ``-1``) and the "model" predicts the
        class frequencies exactly — it separates "something happened" from
        "nothing happened" perfectly and knows nothing about direction. Log
        loss can beat a cruder baseline on that alone, so the report must
        refuse to call it tradeable.
        """
        rng = np.random.default_rng(21)
        size = 2000
        index = INDEX[:size]
        labels = pd.Series(rng.choice([-1, 0, 1], size=size, p=[0.62, 0.20, 0.18]), index=index)
        # Confident about "not up", agnostic between down and flat's direction.
        probabilities = pd.DataFrame(
            {-1: np.full(size, 0.62), 0: np.full(size, 0.20), 1: np.full(size, 0.18)},
            index=index,
        )
        cruder = pd.DataFrame(
            {-1: np.full(size, 1 / 3), 0: np.full(size, 1 / 3), 1: np.full(size, 1 / 3)},
            index=index,
        )
        report = evaluate(labels, probabilities, cruder, permutations=100)

        assert report.beats_baseline  # it genuinely does, on log loss
        assert not report.directional.has_skill
        assert not report.is_tradeable
        assert "NO DIRECTIONAL SKILL" in report.describe()

    def test_directional_skill_ignores_vertical_barrier_bars(self) -> None:
        size = 1000
        index = INDEX[:size]
        rng = np.random.default_rng(22)
        truth = rng.choice([-1, 1], size=size)
        labels = pd.Series(truth, index=index)
        labels.iloc[:500] = 0  # half the sample never resolved directionally

        # A model that is right whenever direction was resolved.
        probabilities = pd.DataFrame(
            {
                -1: np.where(truth < 0, 0.8, 0.1),
                0: np.full(size, 0.1),
                1: np.where(truth > 0, 0.8, 0.1),
            },
            index=index,
        )
        skill = directional_skill(labels, probabilities)
        assert skill.observations == 500
        assert skill.accuracy == pytest.approx(1.0)
        assert skill.has_skill

    def test_a_thin_out_of_sample_period_is_flagged(self) -> None:
        rng = np.random.default_rng(3)
        small = pd.Index(INDEX[:100])
        labels = pd.Series(rng.choice([-1, 0, 1], size=100), index=small)
        probabilities = pd.DataFrame(
            np.full((100, 3), 1 / 3), index=small, columns=[-1, 0, 1]
        )
        report = evaluate(labels, probabilities, probabilities, permutations=50)
        assert any("too few" in note for note in report.notes)

    def test_a_perfectly_calibrated_forecast_scores_near_zero(self) -> None:
        rng = np.random.default_rng(4)
        # Ground truth: P(class 1) = 0.7 everywhere, and the model says 0.7.
        labels = pd.Series(rng.choice([0, 1], size=2000, p=[0.3, 0.7]), index=INDEX[:2000])
        probabilities = pd.DataFrame(
            {0: np.full(2000, 0.3), 1: np.full(2000, 0.7)}, index=INDEX[:2000]
        )
        assert expected_calibration_error(labels, probabilities) < 0.03

    def test_an_overconfident_forecast_is_penalised(self) -> None:
        rng = np.random.default_rng(5)
        labels = pd.Series(rng.choice([0, 1], size=2000, p=[0.5, 0.5]), index=INDEX[:2000])
        overconfident = pd.DataFrame(
            {0: np.full(2000, 0.02), 1: np.full(2000, 0.98)}, index=INDEX[:2000]
        )
        assert expected_calibration_error(labels, overconfident) > 0.4

    def test_the_permutation_pvalue_is_uninformative_on_a_constant_forecast(self) -> None:
        rng = np.random.default_rng(6)
        labels = pd.Series(rng.choice([-1, 0, 1], size=1000), index=INDEX[:1000])
        flat = pd.DataFrame(np.full((1000, 3), 1 / 3), index=INDEX[:1000], columns=[-1, 0, 1])
        # A forecast that says nothing cannot be beaten or beat; every shuffle
        # scores identically, so p should be 1.
        assert permutation_pvalue(labels, flat, permutations=100) == pytest.approx(1.0)

    def test_a_p_value_is_never_reported_as_zero(self) -> None:
        features, labels, horizon = signal_dataset(strength=4.0)
        splitter = PurgedWalkForward(folds=3, embargo_fraction=0.01, min_train_size=300)
        result = walk_forward_evaluate(features, labels, horizon, splitter, permutations=100)
        assert result.report.p_value > 0.0


class TestEndToEndOnBars:
    def test_labels_features_and_folds_compose(self) -> None:
        rng = np.random.default_rng(7)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, SIZE)))
        bars = pd.DataFrame(
            {"close": closes, "high": closes * 1.002, "low": closes * 0.998}, index=INDEX
        )
        volatility = pd.Series(
            bars["close"].pct_change().rolling(20).std().fillna(0.004), index=INDEX
        )
        labels = triple_barrier_labels(
            bars["close"], bars["high"], bars["low"],
            volatility=volatility, horizon_bars=12,
            upper_multiple=2.0, lower_multiple=1.0, min_barrier_pct=0.002,
        )
        usable = labels.usable()
        features = pd.DataFrame(
            {
                "momentum": bars["close"].pct_change(10),
                "volatility": volatility,
                "noise": rng.normal(size=SIZE),
            },
            index=INDEX,
        ).loc[usable]

        splitter = PurgedWalkForward(folds=3, embargo_fraction=0.01, min_train_size=300)
        result = walk_forward_evaluate(
            features,
            labels.label.loc[usable],
            label_horizon_end(labels),
            splitter,
            sample_weight=None,
            permutations=100,
        )
        # A geometric random walk has no edge to find. Anything that claims
        # one here is a leak, not a discovery.
        assert not result.report.beats_baseline or result.report.p_value > 0.05
        assert result.report.observations > 0
