"""Fit and evaluate the signal classifier under purged walk-forward validation.

    python -m nifty50.scripts.train_model --symbol RELIANCE --start 2019-01-01 --end 2025-12-31
    python -m nifty50.scripts.train_model --synthetic     # no broker data required

The script prints an out-of-sample report and nothing else. There is no
in-sample score in the output, no "training accuracy", and no way to ask for
one, because an in-sample number on a model with this much capacity is a
description of the training set rather than a claim about the future.

``--synthetic`` runs the whole pipeline on a geometric random walk. That mode
exists as a control: a random walk has no edge, so a run that reports one has
found a bug, not a signal. Run it whenever the feature or label code changes.

Exit codes: 0 the run completed, 1 the model showed no out-of-sample skill,
2 the run could not proceed (no data, too few labels).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from nifty50.config import Config, load_config
from nifty50.data.store import BarStore
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import IST, Exchange, Timeframe
from nifty50.features import FeatureInputs, compute_features
from nifty50.features.session import session_ordinal
from nifty50.ml.evaluate import walk_forward_evaluate
from nifty50.ml.labels import (
    assert_no_label_leakage,
    average_uniqueness,
    label_horizon_end,
    triple_barrier_labels,
)
from nifty50.ml.splits import PurgedWalkForward
from nifty50.trading_calendar import TradingCalendar

# Columns that are prices, timestamps or bookkeeping rather than signals.
# Feeding a raw price level to a tree is how a model learns "2020 was cheap".
_NON_FEATURE_COLUMNS = frozenset(
    {
        "upper_band",
        "lower_band",
        "session_vwap",
        "obv",
        "opening_range_high",
        "opening_range_low",
        "previous_session_close",
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SYNTH")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2024, 1, 1))
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2025, 12, 31))
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="run on a generated random walk instead of stored bars (a control, not a backtest)",
    )
    parser.add_argument("--permutations", type=int, default=500)
    args = parser.parse_args(argv)

    config = load_config()
    calendar = TradingCalendar.from_config(config)

    bars, index_close = _load(args, config, calendar)
    if len(bars) < 1000:
        print(f"only {len(bars)} bars available; not enough to validate anything", file=sys.stderr)
        return 2

    if args.synthetic:
        print(
            "CONTROL RUN on a geometric random walk. There is no edge in this data.\n"
            "Any result that beats the baseline with p < 0.05 is a bug in the\n"
            "pipeline, not a discovery.\n"
        )

    features = compute_features(
        FeatureInputs(
            bars=bars,
            timeframe=config.data.base_timeframe,
            index_close=index_close,
        ),
        calendar,
        config,
    )

    settings = config.ml
    volatility = (features["atr_14"] / bars["close"]).rename("atr_pct")
    labels = triple_barrier_labels(
        bars["close"],
        bars["high"],
        bars["low"],
        volatility=volatility,
        horizon_bars=int(settings["label_horizon_bars"]),
        upper_multiple=float(settings["label_upper_atr_multiple"]),
        lower_multiple=float(settings["label_lower_atr_multiple"]),
        min_barrier_pct=float(settings["label_min_barrier_pct"]),
        session_ordinal=session_ordinal(bars) if settings["label_within_session"] else None,
    )
    print(labels.describe())
    print()

    usable = labels.usable()
    matrix = _feature_matrix(features).loc[usable]
    matrix = matrix.loc[:, matrix.notna().any()]
    assert_no_label_leakage(matrix, labels)

    weights = average_uniqueness(labels)
    print(
        f"Average label uniqueness: {weights.mean():.3f}. "
        f"Effective sample size is roughly {int(len(usable) * weights.mean())} "
        f"of {len(usable)} rows."
    )
    print()

    splitter = PurgedWalkForward(
        folds=int(settings["cv_folds"]),
        embargo_fraction=float(settings["cv_embargo_fraction"]),
        min_train_size=int(settings["cv_min_train_size"]),
        max_train_size=settings["cv_max_train_size"],
    )
    horizon = label_horizon_end(labels)
    print(splitter.report(matrix.index, horizon))
    print()

    try:
        result = walk_forward_evaluate(
            matrix,
            labels.label.loc[usable],
            horizon,
            splitter,
            sample_weight=weights,
            permutations=args.permutations,
        )
    except ValueError as error:
        print(f"cannot evaluate: {error}", file=sys.stderr)
        return 2

    print(result.describe())
    print()
    print("This is a classification report, not a strategy result. It says nothing")
    print("about profit: the costs in nifty50.backtest.costs have not been applied,")
    print("and a model can be well calibrated and still lose money after them.")
    print()
    print("Not investment advice.")

    return 0 if result.report.is_tradeable else 1


def _load(
    args: argparse.Namespace, config: Config, calendar: TradingCalendar
) -> tuple[pd.DataFrame, pd.Series]:
    timeframe = config.data.base_timeframe
    if args.synthetic:
        bars = generate_session_bars(
            calendar, args.start, args.end, timeframe, start_price=1400.0, seed=101
        )
        index_close = generate_session_bars(
            calendar, args.start, args.end, timeframe, start_price=24000.0, seed=202
        )["close"]
        return bars, index_close

    store = BarStore.from_config(config)
    window = {
        "start": dt.datetime.combine(args.start, dt.time.min, tzinfo=IST),
        "end": dt.datetime.combine(args.end, dt.time.max, tzinfo=IST),
    }
    bars = store.read(Exchange.NSE, args.symbol, timeframe, **window)
    index_symbol = config.universe.index_instruments[0].symbol
    index_close = store.read(Exchange.NSE, index_symbol, timeframe, **window)["close"]
    return bars, index_close


def _feature_matrix(features: pd.DataFrame) -> pd.DataFrame:
    """Numeric, scale-free columns only.

    Raw price levels are dropped rather than differenced here. A tree splitting
    on "close > 1420" fits the sample's price range and generalises to nothing;
    the ratios and z-scores that survive are comparable across symbols and
    across time, which is what makes a single model over the whole index
    coherent.
    """
    keep = [
        column
        for column in features.columns
        if str(column) not in _NON_FEATURE_COLUMNS and features[column].dtype.kind in "fib"
    ]
    return features[keep].astype("float64")


def _timeframe_label(timeframe: Timeframe) -> str:
    return timeframe.value


if __name__ == "__main__":
    raise SystemExit(main())
