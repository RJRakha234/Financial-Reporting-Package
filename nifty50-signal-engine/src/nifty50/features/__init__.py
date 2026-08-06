"""Feature layer: trend, volatility, volume/flow and multi-timeframe context.

Every feature is backward-looking. ``tests/test_lookahead.py`` proves it by
truncating the future and asserting no value at *t* moves.
"""

from nifty50.features.pipeline import FeatureInputs, compute_features

__all__ = ["FeatureInputs", "compute_features"]
