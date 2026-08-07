"""Supervised layer: labelling, purged validation, a calibrated classifier.

Import direction is one-way — this package imports from :mod:`nifty50.features`
and never the reverse — so a label can never become a feature by accident.
"""

from nifty50.ml.evaluate import (
    ClassificationReport,
    DirectionalSkill,
    directional_skill,
    walk_forward_evaluate,
)
from nifty50.ml.labels import TripleBarrierLabels, average_uniqueness, triple_barrier_labels
from nifty50.ml.model import MajorityBaseline, SignalClassifier
from nifty50.ml.splits import Fold, PurgedWalkForward

__all__ = [
    "ClassificationReport",
    "DirectionalSkill",
    "Fold",
    "MajorityBaseline",
    "PurgedWalkForward",
    "SignalClassifier",
    "TripleBarrierLabels",
    "average_uniqueness",
    "directional_skill",
    "triple_barrier_labels",
    "walk_forward_evaluate",
]
