"""The look-ahead bias test.

The single most important test in this project. Everything else can be wrong
and produce a bad strategy; look-ahead bias produces a *beautiful* strategy that
loses money the moment it trades live, and it does so silently.

METHOD
------
Compute every feature on the full history. Then truncate the history at bar *t*
— discarding every bar after it, exactly as reality does — and recompute. If a
feature at bar *t* differs between the two, that feature consumed information
from after *t*.

This catches the whole family at once: ``shift(-n)``, ``center=True`` rolling
windows, global (rather than rolling) statistics, ``bfill``, unconfirmed pivot
detection, and higher-timeframe bars joined on their open rather than their
close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cse.config import Config
from cse.data.schema import interval_to_ms
from cse.data.synthetic import SyntheticMarket, aggregate_candles
from cse.features import multiframe, statistical, trend, volatility, volume
from cse.features.pipeline import compute_feature_set, compute_single_timeframe

MINUTE_MS = 60_000
FOUR_HOURS_MS = 240 * MINUTE_MS
START_MS = 1_700_000_000_000 // FOUR_HOURS_MS * FOUR_HOURS_MS
# Long enough that the slowest indicator (ATR percentile, 500 bars) is defined.
BARS_1M = 40_000

# Truncation points chosen inside the usable region, well past warmup.
TRUNCATION_POINTS = [1200, 1500, 1900]


@pytest.fixture(scope="module")
def base_candles() -> pd.DataFrame:
    """A 15m series long enough for every indicator window to be defined."""
    from cse.config import load_config

    config = load_config()
    market = SyntheticMarket(config.data.synthetic, ["BTCUSDT", "ETHUSDT"])
    frames = market.generate_1m(START_MS, START_MS + BARS_1M * MINUTE_MS)
    return aggregate_candles(frames["BTCUSDT"], 15 * MINUTE_MS).reset_index(drop=True)


@pytest.fixture(scope="module")
def reference_candles() -> pd.DataFrame:
    from cse.config import load_config

    config = load_config()
    market = SyntheticMarket(config.data.synthetic, ["BTCUSDT", "ETHUSDT"])
    frames = market.generate_1m(START_MS, START_MS + BARS_1M * MINUTE_MS)
    return aggregate_candles(frames["ETHUSDT"], 15 * MINUTE_MS).reset_index(drop=True)


def assert_prefix_stable(
    full: pd.DataFrame, truncated: pd.DataFrame, cutoff: int, *, label: str
) -> None:
    """Every value at or before ``cutoff`` must be identical in both frames."""
    assert len(truncated) == cutoff + 1, f"{label}: truncation produced the wrong length"

    mismatched: list[str] = []
    for column in full.columns:
        if column not in truncated.columns:
            mismatched.append(f"{column} (missing)")
            continue
        left = full[column].iloc[: cutoff + 1].to_numpy()
        right = truncated[column].iloc[: cutoff + 1].to_numpy()
        if left.dtype.kind in "fc" or right.dtype.kind in "fc":
            same = np.allclose(
                left.astype(np.float64), right.astype(np.float64), equal_nan=True, rtol=1e-9
            )
        else:
            same = bool(np.array_equal(left, right))
        if not same:
            first = _first_difference(left, right)
            mismatched.append(f"{column} (first differs at bar {first})")

    assert not mismatched, (
        f"{label}: {len(mismatched)} feature(s) changed when the future was removed — "
        f"these read data from after their own bar: {mismatched[:10]}"
    )


def _first_difference(left: np.ndarray, right: np.ndarray) -> int:
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        if isinstance(a, float) and np.isnan(a) and isinstance(b, float) and np.isnan(b):
            continue
        if a != b:
            return index
    return -1


# ---- per feature group ------------------------------------------------------


@pytest.mark.parametrize("cutoff", TRUNCATION_POINTS)
def test_trend_features_have_no_lookahead(
    base_candles: pd.DataFrame, config: Config, cutoff: int
) -> None:
    full = trend.compute(base_candles, config.features.trend)
    truncated = trend.compute(
        base_candles.iloc[: cutoff + 1].reset_index(drop=True), config.features.trend
    )

    assert_prefix_stable(full, truncated, cutoff, label="trend")


@pytest.mark.parametrize("cutoff", TRUNCATION_POINTS)
def test_volatility_features_have_no_lookahead(
    base_candles: pd.DataFrame, config: Config, cutoff: int
) -> None:
    full = volatility.compute(base_candles, config.features.volatility)
    truncated = volatility.compute(
        base_candles.iloc[: cutoff + 1].reset_index(drop=True), config.features.volatility
    )

    assert_prefix_stable(full, truncated, cutoff, label="volatility")


@pytest.mark.parametrize("cutoff", TRUNCATION_POINTS)
def test_volume_features_have_no_lookahead(
    base_candles: pd.DataFrame, config: Config, cutoff: int
) -> None:
    full = volume.compute(base_candles, config.features.volume)
    truncated = volume.compute(
        base_candles.iloc[: cutoff + 1].reset_index(drop=True), config.features.volume
    )

    assert_prefix_stable(full, truncated, cutoff, label="volume")


@pytest.mark.parametrize("cutoff", TRUNCATION_POINTS)
def test_statistical_features_have_no_lookahead(
    base_candles: pd.DataFrame, reference_candles: pd.DataFrame, config: Config, cutoff: int
) -> None:
    full = statistical.compute(
        base_candles, config.features.statistical, reference_close=reference_candles["close"]
    )
    truncated = statistical.compute(
        base_candles.iloc[: cutoff + 1].reset_index(drop=True),
        config.features.statistical,
        reference_close=reference_candles["close"].iloc[: cutoff + 1].reset_index(drop=True),
    )

    assert_prefix_stable(full, truncated, cutoff, label="statistical")


@pytest.mark.parametrize("cutoff", TRUNCATION_POINTS)
def test_full_single_timeframe_pipeline_has_no_lookahead(
    base_candles: pd.DataFrame, reference_candles: pd.DataFrame, config: Config, cutoff: int
) -> None:
    full = compute_single_timeframe(
        base_candles, config.features, reference_close=reference_candles["close"]
    )
    truncated = compute_single_timeframe(
        base_candles.iloc[: cutoff + 1].reset_index(drop=True),
        config.features,
        reference_close=reference_candles["close"].iloc[: cutoff + 1].reset_index(drop=True),
    )

    assert_prefix_stable(full, truncated, cutoff, label="pipeline")


# ---- multi-timeframe: the highest-risk join --------------------------------


def _truncate_all(candles: dict[str, pd.DataFrame], cutoff_time: int) -> dict[str, pd.DataFrame]:
    """Drop every bar that had not closed by ``cutoff_time`` on every timeframe."""
    out: dict[str, pd.DataFrame] = {}
    for timeframe, frame in candles.items():
        interval = interval_to_ms(timeframe)
        # A bar is available only once closed: open_time + interval <= cutoff.
        keep = frame.loc[frame["open_time"] + interval <= cutoff_time]
        out[timeframe] = keep.reset_index(drop=True)
    return out


@pytest.fixture(scope="module")
def multi_timeframe_candles() -> dict[str, pd.DataFrame]:
    from cse.config import load_config

    config = load_config()
    market = SyntheticMarket(config.data.synthetic, ["BTCUSDT", "ETHUSDT"])
    one_minute = market.generate_1m(START_MS, START_MS + BARS_1M * MINUTE_MS)["BTCUSDT"]
    return {
        "15m": aggregate_candles(one_minute, 15 * MINUTE_MS).reset_index(drop=True),
        "1h": aggregate_candles(one_minute, 60 * MINUTE_MS).reset_index(drop=True),
        "4h": aggregate_candles(one_minute, FOUR_HOURS_MS).reset_index(drop=True),
    }


@pytest.mark.parametrize("cutoff", [1000, 1400])
def test_multi_timeframe_context_has_no_lookahead(
    multi_timeframe_candles: dict[str, pd.DataFrame], config: Config, cutoff: int
) -> None:
    """The classic trap: a 15m bar must not see the 4h bar it is sitting inside.

    That 4h bar does not close for up to four more hours; its high, low and
    close are still moving. Joining on open_time instead of close time leaks a
    full higher-timeframe bar of future information into every base bar.
    """
    full = compute_feature_set("BTCUSDT", "15m", multi_timeframe_candles, config)

    cutoff_time = int(multi_timeframe_candles["15m"]["open_time"].iloc[cutoff]) + interval_to_ms(
        "15m"
    )
    truncated_candles = _truncate_all(multi_timeframe_candles, cutoff_time)
    truncated = compute_feature_set("BTCUSDT", "15m", truncated_candles, config)

    columns = [c for c in full.frame.columns if c.startswith("trend_state") or c.startswith("mtf_")]
    truncated_length = len(truncated.frame)
    assert truncated_length == cutoff + 1

    for column in columns:
        left = full.frame[column].iloc[: cutoff + 1].to_numpy(dtype=np.float64)
        right = truncated.frame[column].iloc[: cutoff + 1].to_numpy(dtype=np.float64)
        assert np.allclose(left, right, equal_nan=True), (
            f"{column} changed when future bars were removed — the higher-timeframe "
            f"join is reading bars that had not closed yet"
        )


def test_higher_timeframe_value_is_never_from_an_unclosed_bar(config: Config) -> None:
    """Directly assert the alignment rule rather than inferring it.

    The 4h bar opening at 12:00 closes at 16:00. A 15m bar at 13:15 must see the
    4h bar that closed at 12:00 — not the one it is inside.
    """
    four_hour = pd.DataFrame(
        {
            "open_time": [0, FOUR_HOURS_MS, 2 * FOUR_HOURS_MS],
            "trend_state": [1, -1, 1],
        }
    )
    base = pd.DataFrame(
        {
            "open_time": [
                FOUR_HOURS_MS,  # 4h bar 0 just closed
                FOUR_HOURS_MS + 15 * MINUTE_MS,  # inside 4h bar 1
                2 * FOUR_HOURS_MS - 15 * MINUTE_MS,  # still inside 4h bar 1
                2 * FOUR_HOURS_MS,  # 4h bar 1 just closed
            ]
        }
    )

    aligned = multiframe.align_higher_timeframe(
        base, four_hour, ["trend_state"], FOUR_HOURS_MS, suffix="4h"
    )

    values = aligned["trend_state_4h"].tolist()
    # Bars inside 4h bar 1 must still report bar 0's state (+1), never bar 1's (-1).
    assert values[0] == 1
    assert values[1] == 1, "leaked the state of the 4h bar that had not closed"
    assert values[2] == 1, "leaked the state of the 4h bar that had not closed"
    assert values[3] == -1, "should update once the 4h bar has actually closed"


# ---- guards against the specific bug patterns -------------------------------


def _code_only_lines(source: str) -> list[str]:
    """Source with every string literal and comment blanked out.

    Scanning raw text would flag this module's own documentation, which names
    each forbidden construct in order to warn against it. Only executable code
    should be searched.
    """
    import io
    import tokenize

    lines = source.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:  # pragma: no cover - only on unparseable source
        return lines

    for token in tokens:
        if token.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            index = row - 1
            if index >= len(blanked):
                continue
            line = blanked[index]
            begin = start_col if row == start_row else 0
            finish = end_col if row == end_row else len(line)
            blanked[index] = line[:begin] + " " * max(0, finish - begin) + line[finish:]
    return blanked


def test_no_feature_column_uses_a_future_reading_construct() -> None:
    """Static guard against the constructs that are always leaks.

    Cheap, and it catches the mistake at the moment it is written rather than
    after a suspiciously good backtest. Complements the truncation tests above:
    those catch leaks in what is executed, this catches them on sight.
    """
    import pathlib

    features_dir = pathlib.Path(__file__).resolve().parent.parent / "cse" / "features"
    forbidden = {
        ".shift(-": "negative shift",
        "shift(periods=-": "negative shift",
        "center=True": "centred rolling window",
        ".bfill(": "backward fill",
        'method="bfill"': "backward fill",
        ".iloc[::-1]": "reversed iteration",
    }

    offenders: list[str] = []
    for path in sorted(features_dir.glob("*.py")):
        for number, line in enumerate(_code_only_lines(path.read_text(encoding="utf-8")), start=1):
            for pattern, description in forbidden.items():
                if pattern in line:
                    offenders.append(f"{path.name}:{number} ({description})")

    assert not offenders, f"future-reading constructs found: {offenders}"


def test_the_static_guard_actually_detects_a_planted_leak() -> None:
    """A guard nobody has seen fail is not evidence of anything."""
    leaky = "value = close.shift(-1)  # peeks at tomorrow\n"
    documented = '"""Never write close.shift(-1) here."""\n'

    assert any(".shift(-" in line for line in _code_only_lines(leaky))
    assert not any(".shift(-" in line for line in _code_only_lines(documented))
