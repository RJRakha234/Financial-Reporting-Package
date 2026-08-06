"""Multi-timeframe confirmation.

Every base-timeframe feature row carries the trend context of the higher
timeframes. Per the spec, a conflict **downgrades** a signal rather than
suppressing it, and the conflict is recorded explicitly so it survives into the
signal's ``contributing_factors``.

THE ALIGNMENT TRAP
------------------
Joining a 4h series onto 15m bars is where look-ahead bias most often enters a
multi-timeframe system, and it is easy to get wrong in a way that looks right.

The 4h bar opening at 12:00 does not *close* until 16:00. A 15m bar at 13:15
must therefore see the 4h bar that closed at 12:00 — not the one it currently
sits inside, whose high, low and close are still moving and will only be known
in the future.

:func:`align_higher_timeframe` enforces this by mapping each higher-timeframe
bar to its own **close** time and then taking the last value strictly available
at or before the base bar's close. Merging on ``open_time`` instead — the
obvious thing to write — leaks up to one full higher-timeframe bar of future
information into every base bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cse.config import MultiframeFeatureConfig
from cse.data.schema import interval_to_ms

NEUTRAL = 0
BULLISH = 1
BEARISH = -1


def align_higher_timeframe(
    base: pd.DataFrame,
    higher: pd.DataFrame,
    columns: list[str],
    higher_interval_ms: int,
    *,
    suffix: str,
) -> pd.DataFrame:
    """Attach higher-timeframe columns to base bars without look-ahead.

    A higher-timeframe value becomes visible only once its bar has closed, so
    each higher bar is keyed by ``open_time + interval`` (its close instant) and
    matched to the latest base bar at or after that point.
    """
    if higher.empty or base.empty:
        return pd.DataFrame(index=base.index)

    missing = [c for c in columns if c not in higher.columns]
    if missing:
        raise KeyError(f"higher timeframe frame is missing {missing}")

    available_at = higher["open_time"].astype("int64") + higher_interval_ms
    source = higher.loc[:, columns].copy()
    source["_available_at"] = available_at
    source = source.sort_values("_available_at", kind="mergesort")

    target = pd.DataFrame({"_base_open_time": base["open_time"].astype("int64")})
    target = target.sort_values("_base_open_time", kind="mergesort")

    merged = pd.merge_asof(
        target,
        source,
        left_on="_base_open_time",
        right_on="_available_at",
        direction="backward",
        allow_exact_matches=True,
    )
    merged.index = target.index
    merged = merged.sort_index()

    renamed = merged.loc[:, columns].rename(columns={c: f"{c}_{suffix}" for c in columns})
    renamed.index = base.index
    return renamed


def trend_state(features: pd.DataFrame) -> pd.Series[int]:
    """Reduce a timeframe's trend features to one bullish/bearish/neutral state.

    A simple majority of three independent reads — EMA stack alignment,
    Supertrend direction, and Ichimoku cloud position — so no single indicator
    decides the higher-timeframe context on its own.
    """
    votes = pd.DataFrame(index=features.index)
    votes["ema"] = features.get("ema_alignment", pd.Series(0, index=features.index))
    votes["supertrend"] = features.get("supertrend_direction", pd.Series(0, index=features.index))
    votes["ichimoku"] = features.get("ichimoku_cloud_position", pd.Series(0, index=features.index))

    # Count votes by SIGN rather than summing them. Summing conflates two
    # different situations at the same total: "two bearish and one bullish"
    # (real disagreement, majority bearish) and "one bearish, two neutral"
    # (weak, no majority) both sum to -1. Reading the former as neutral would
    # hide exactly the higher-timeframe conflicts this feature exists to expose.
    signs = np.sign(votes.to_numpy(dtype=np.float64))
    bullish = (signs > 0).sum(axis=1)
    bearish = (signs < 0).sum(axis=1)

    state = pd.Series(NEUTRAL, index=features.index, dtype="int64")
    state[bullish >= 2] = BULLISH
    state[bearish >= 2] = BEARISH
    return state


def compute(
    base_features: pd.DataFrame,
    base_frame: pd.DataFrame,
    higher: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    config: MultiframeFeatureConfig,
) -> pd.DataFrame:
    """Attach higher-timeframe trend context to the base-timeframe features.

    ``higher`` maps a timeframe label to ``(candle_frame, feature_frame)``.
    Emits, per configured context timeframe, that timeframe's trend state, plus
    an overall agreement score and an explicit conflict flag.
    """
    out = pd.DataFrame(index=base_features.index)
    base_state = trend_state(base_features)
    out["trend_state"] = base_state

    attached: list[str] = []
    for timeframe in config.context_timeframes:
        if timeframe not in higher:
            continue
        higher_frame, higher_features = higher[timeframe]
        state_frame = higher_features.copy()
        state_frame["trend_state"] = trend_state(higher_features)
        state_frame["open_time"] = higher_frame["open_time"].to_numpy()

        aligned = align_higher_timeframe(
            base_frame,
            state_frame,
            ["trend_state"],
            interval_to_ms(timeframe),
            suffix=timeframe,
        )
        column = f"trend_state_{timeframe}"
        out[column] = aligned[column].fillna(NEUTRAL).astype("int64")
        attached.append(column)

    if not attached:
        out["mtf_agreement"] = 0.0
        out["mtf_conflict"] = 0
        out["mtf_confidence_multiplier"] = 1.0
        return out

    context = out[attached]
    # Agreement in [-1, 1]: the mean higher-timeframe state seen from the base
    # signal's direction. Positive means the higher timeframes back it.
    directional = context.multiply(base_state, axis=0)
    agreement = directional.where(base_state != NEUTRAL).mean(axis=1)
    out["mtf_agreement"] = agreement.fillna(0.0)

    conflict = (directional < 0).any(axis=1) & (base_state != NEUTRAL)
    out["mtf_conflict"] = conflict.astype("int64")

    # Downgrade, never suppress: the signal still fires, at reduced confidence,
    # and the conflict is recorded for the reasoning payload.
    multiplier = pd.Series(1.0, index=out.index)
    multiplier[conflict] = config.conflict_downgrade
    out["mtf_confidence_multiplier"] = multiplier
    return out
