"""Indicators checked against hand-computed reference values.

Hand-computed rather than diffed against a TA library on purpose. A library
comparison only proves we match *someone else's* convention — and TA libraries
disagree with each other on seeding, on ddof, and on whether ADX is smoothed
once or twice. Every expected number below is derived from the formula in the
docstring, so a failure means the arithmetic is wrong, not that a dependency
changed its mind.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from cse.config import Config
from cse.features.base import (
    confirmed_pivots,
    crossover,
    ema,
    rolling_percentile_rank,
    rolling_slope,
    rolling_zscore,
    safe_divide,
    true_range,
    wilder_smooth,
)
from cse.features.statistical import (
    classify_hurst_regime,
    hurst_exponent,
    hurst_null_distribution,
    ou_half_life,
    rolling_hurst,
)
from cse.features.trend import adx, ichimoku, macd, rsi, supertrend
from cse.features.volatility import bollinger, realized_volatility, volatility_regime
from cse.features.volume import on_balance_volume, order_book_imbalance, volume_profile

NAN = float("nan")


def series(values: Sequence[float] | np.ndarray) -> pd.Series[float]:
    return pd.Series(values, dtype="float64")


# ---- smoothing primitives ---------------------------------------------------


def test_ema_matches_hand_computation() -> None:
    """SMA-seeded EMA, alpha = 2/(n+1).

    period=3 over [1,2,3,4,5]: seed = mean(1,2,3) = 2, alpha = 0.5
      i=3: 2 + 0.5*(4-2) = 3
      i=4: 3 + 0.5*(5-3) = 4
    """
    result = ema(series([1, 2, 3, 4, 5]), 3)

    assert np.isnan(result.iloc[0]) and np.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_wilder_smoothing_matches_hand_computation() -> None:
    """RMA, alpha = 1/n.

    period=3 over [1,2,3,4,5]: seed = 2
      i=3: 2 + (1/3)*(4-2) = 2.666667
      i=4: 2.666667 + (1/3)*(5-2.666667) = 3.444444
    """
    result = wilder_smooth(series([1, 2, 3, 4, 5]), 3)

    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(2.666667, abs=1e-6)
    assert result.iloc[4] == pytest.approx(3.444444, abs=1e-6)


def test_smoothers_seed_from_the_first_valid_value() -> None:
    """Regression: a leading NaN (from .diff()) must not poison the seed.

    Averaging position 0 into the seed makes it NaN, and the recursion then
    stays NaN forever — RSI would silently never produce a value.
    """
    with_leading_nan = series([NAN, 1, 2, 3, 4])

    result = wilder_smooth(with_leading_nan, 2)

    assert np.isnan(result.iloc[0])
    assert result.iloc[2] == pytest.approx(1.5)  # mean(1, 2)
    assert result.iloc[3] == pytest.approx(2.25)  # 1.5 + 0.5*(3-1.5)
    assert not result.iloc[2:].isna().any()


def test_ema_is_nan_before_it_has_enough_data() -> None:
    """A partially-seeded EMA(200) over the first 200 bars is silently wrong."""
    result = ema(series([1, 2, 3]), 5)

    assert result.isna().all()


# ---- true range and ATR -----------------------------------------------------


def test_true_range_matches_hand_computation() -> None:
    """TR = max(h-l, |h-prev_close|, |l-prev_close|).

    Bar 1: h=12, l=9, prev_close=10 -> max(3, 2, 1) = 3
    Bar 2: h=15, l=14, prev_close=11 -> max(1, 4, 3) = 4  (a gap up)
    """
    high = series([11, 12, 15])
    low = series([9, 9, 14])
    close = series([10, 11, 14])

    result = true_range(high, low, close)

    assert result.iloc[0] == pytest.approx(2.0)  # no previous close: h - l
    assert result.iloc[1] == pytest.approx(3.0)
    assert result.iloc[2] == pytest.approx(4.0)


# ---- RSI --------------------------------------------------------------------


def test_rsi_matches_hand_computation() -> None:
    """RSI(2) over [10, 11, 10, 11, 12].

    gains  = [-, 1, 0, 1, 1]   losses = [-, 0, 1, 0, 0]
    seed at i=2: avg gain = 0.5, avg loss = 0.5  -> RS 1   -> RSI 50
    i=3: gain 0.5+0.5*(1-0.5)=0.75, loss 0.5+0.5*(0-0.5)=0.25 -> RS 3 -> 75
    i=4: gain 0.75+0.5*(1-0.75)=0.875, loss 0.25+0.5*(0-0.25)=0.125 -> RS 7 -> 87.5
    """
    result = rsi(series([10, 11, 10, 11, 12]), 2)

    assert result.iloc[2] == pytest.approx(50.0)
    assert result.iloc[3] == pytest.approx(75.0)
    assert result.iloc[4] == pytest.approx(87.5)


def test_rsi_is_bounded_and_saturates() -> None:
    rising = rsi(series([float(i) for i in range(1, 30)]), 14)
    falling = rsi(series([float(i) for i in range(30, 1, -1)]), 14)

    assert rising.dropna().max() == pytest.approx(100.0)
    assert falling.dropna().min() == pytest.approx(0.0)
    assert rising.dropna().between(0, 100).all()


def test_flat_series_gives_neutral_rsi_not_a_division_blowup() -> None:
    """Zero gains and zero losses is 0/0; defined as neutral, not 100."""
    result = rsi(series([100.0] * 20), 14)

    assert result.dropna().eq(50.0).all()


# ---- MACD -------------------------------------------------------------------


def test_macd_line_is_the_ema_difference() -> None:
    close = series([float(i) for i in range(1, 60)])

    result = macd(close, fast=12, slow=26, signal=9)
    expected = ema(close, 12) - ema(close, 26)

    pd.testing.assert_series_equal(result["macd"].dropna(), expected.dropna(), check_names=False)


def test_macd_histogram_is_line_minus_signal() -> None:
    close = series([float(i) for i in range(1, 80)])

    result = macd(close, fast=12, slow=26, signal=9)

    difference = (result["macd"] - result["macd_signal"]).dropna()
    pd.testing.assert_series_equal(result["macd_histogram"].dropna(), difference, check_names=False)


# ---- ADX --------------------------------------------------------------------


def test_adx_is_bounded_and_rises_in_a_strong_trend() -> None:
    """ADX measures strength, not direction: it must rise in EITHER direction."""
    n = 80
    up = pd.DataFrame(
        {
            "high": [100 + i * 2 + 1 for i in range(n)],
            "low": [100 + i * 2 - 1 for i in range(n)],
            "close": [100 + i * 2 for i in range(n)],
        },
        dtype="float64",
    )
    down = pd.DataFrame(
        {
            "high": [100 - i * 2 + 1 for i in range(n)],
            "low": [100 - i * 2 - 1 for i in range(n)],
            "close": [100 - i * 2 for i in range(n)],
        },
        dtype="float64",
    )

    up_adx = adx(up["high"], up["low"], up["close"], 14)
    down_adx = adx(down["high"], down["low"], down["close"], 14)

    assert up_adx["adx"].dropna().iloc[-1] > 50.0
    assert down_adx["adx"].dropna().iloc[-1] > 50.0
    assert up_adx["adx"].dropna().between(0, 100).all()
    # Direction comes from the DIs, not from ADX.
    assert up_adx["plus_di"].dropna().iloc[-1] > up_adx["minus_di"].dropna().iloc[-1]
    assert down_adx["minus_di"].dropna().iloc[-1] > down_adx["plus_di"].dropna().iloc[-1]


# ---- Ichimoku ---------------------------------------------------------------


def test_ichimoku_conversion_line_is_the_midpoint_of_its_window(config: Config) -> None:
    n = 60
    high = series([float(100 + i) for i in range(n)])
    low = series([float(90 + i) for i in range(n)])
    close = series([float(95 + i) for i in range(n)])
    trend_config = config.features.trend

    result = ichimoku(high, low, close, trend_config)

    window = trend_config.ichimoku_conversion
    at = 40
    expected = (
        high.iloc[at - window + 1 : at + 1].max() + low.iloc[at - window + 1 : at + 1].min()
    ) / 2
    assert result["ichimoku_conversion"].iloc[at] == pytest.approx(expected)


def test_ichimoku_does_not_expose_the_chikou_span(config: Config) -> None:
    """Chikou is close shifted BACKWARD: reading it at t returns t+26.

    Any column carrying it would be a direct future leak, so it must not exist.
    """
    n = 80
    high = series([float(100 + i) for i in range(n)])
    low = series([float(90 + i) for i in range(n)])
    close = series([float(95 + i) for i in range(n)])

    result = ichimoku(high, low, close, config.features.trend)

    assert not any("chikou" in column for column in result.columns)


def test_ichimoku_spans_are_displaced_forward_only(config: Config) -> None:
    """Span A at bar t must equal the undisplaced value from t - displacement."""
    n = 100
    rng = np.random.default_rng(0)
    close = series(100 + np.cumsum(rng.standard_normal(n)))
    high = close + 1.0
    low = close - 1.0
    trend_config = config.features.trend

    result = ichimoku(high, low, close, trend_config)

    displacement = trend_config.ichimoku_displacement
    undisplaced = (result["ichimoku_conversion"] + result["ichimoku_base"]) / 2.0
    at = 90
    assert result["ichimoku_span_a"].iloc[at] == pytest.approx(undisplaced.iloc[at - displacement])


# ---- Supertrend -------------------------------------------------------------


def test_supertrend_flips_direction_and_brackets_price() -> None:
    n = 60
    rising = [100.0 + i for i in range(n)]
    falling = [100.0 + n - i for i in range(n)]
    close = series(rising + falling)
    high = close + 1.0
    low = close - 1.0

    result = supertrend(high, low, close, period=10, multiplier=3.0)

    directions = result["supertrend_direction"]
    assert directions.iloc[n - 5] == 1, "should be long during the rise"
    assert directions.iloc[-5] == -1, "should flip short during the fall"
    # The line sits below price when long and above it when short.
    long_bars = result.loc[directions == 1].dropna(subset=["supertrend"])
    assert (long_bars["supertrend"] <= close.loc[long_bars.index] + 1e-9).all()


# ---- Bollinger --------------------------------------------------------------


def test_bollinger_matches_hand_computation() -> None:
    """period=3, 2 std over [1,2,3,4,5]; ddof=0.

    At i=2: mean(1,2,3)=2, pop std = sqrt(2/3) = 0.8164966
    upper = 2 + 2*0.8164966 = 3.6329932
    """
    result = bollinger(series([1, 2, 3, 4, 5]), 3, 2.0)

    assert result["bb_middle"].iloc[2] == pytest.approx(2.0)
    assert result["bb_upper"].iloc[2] == pytest.approx(3.6329932, abs=1e-6)
    assert result["bb_lower"].iloc[2] == pytest.approx(0.3670068, abs=1e-6)


def test_percent_b_locates_price_within_the_bands() -> None:
    close = series([10, 11, 12, 11, 10, 11, 12, 13, 12, 11])

    result = bollinger(close, 5, 2.0)

    at_upper = result["bb_percent_b"].dropna()
    assert at_upper.between(-1.0, 2.0).all()


def test_zero_width_band_yields_nan_not_infinity() -> None:
    """A flat window has zero stdev; %B is 0/0 and must not become inf."""
    result = bollinger(series([100.0] * 10), 5, 2.0)

    assert result["bb_percent_b"].dropna().empty or result["bb_percent_b"].isna().all()
    assert not np.isinf(result["bb_percent_b"].to_numpy()).any()


# ---- volatility regime ------------------------------------------------------


def test_volatility_regime_uses_rolling_not_global_quantiles(config: Config) -> None:
    """The classification at bar t must not know about later volatility.

    A calm stretch followed by a violent one. Judged against ROLLING quantiles,
    the calm head is compared with itself and so spans low/normal/high. Judged
    against GLOBAL quantiles it would be uniformly 'low' — purely because of
    volatility that had not happened yet.
    """
    rng = np.random.default_rng(5)
    calm = 0.001 + rng.random(600) * 0.0002
    violent = 0.05 + rng.random(600) * 0.01
    volatility = pd.Series(np.concatenate([calm, violent]), dtype="float64")
    volatility_config = config.features.volatility.model_copy(update={"regime_window": 100})

    result = volatility_regime(volatility, volatility_config)

    head = result["volatility_regime"].iloc[200:500]
    assert set(head.unique()) == {-1, 0, 1}, (
        "the calm stretch must be ranked against itself, not against the violent tail"
    )


def test_flat_volatility_is_normal_not_high(config: Config) -> None:
    """Regression: equal quantiles made every comparison true and HIGH won.

    A dead-flat stretch — an illiquid period or a halted market — was reported
    as maximum volatility.
    """
    volatility = pd.Series(np.full(400, 0.002), dtype="float64")
    volatility_config = config.features.volatility.model_copy(update={"regime_window": 100})

    result = volatility_regime(volatility, volatility_config)

    assert (result["volatility_regime"] == 0).all()


def test_realized_volatility_is_zero_for_a_flat_series() -> None:
    result = realized_volatility(series([100.0] * 40), 20)

    assert result.dropna().eq(0.0).all()


# ---- volume -----------------------------------------------------------------


def test_obv_accumulates_signed_volume() -> None:
    """OBV adds volume on an up-close and subtracts it on a down-close.

    closes [10, 11, 10, 12], volumes [5, 3, 4, 6]
      i=0: direction 0 -> 0
      i=1: up   -> +3  = 3
      i=2: down -> -4  = -1
      i=3: up   -> +6  = 5
    """
    result = on_balance_volume(series([10, 11, 10, 12]), series([5, 3, 4, 6]))

    assert result.tolist() == pytest.approx([0.0, 3.0, -1.0, 5.0])


def test_order_book_imbalance_signs_correctly() -> None:
    heavy_bids = order_book_imbalance([[100.0, 10.0]], [[101.0, 2.0]], levels=20)
    heavy_asks = order_book_imbalance([[100.0, 2.0]], [[101.0, 10.0]], levels=20)
    balanced = order_book_imbalance([[100.0, 5.0]], [[101.0, 5.0]], levels=20)

    assert heavy_bids == pytest.approx((10 - 2) / 12)
    assert heavy_asks == pytest.approx((2 - 10) / 12)
    assert balanced == pytest.approx(0.0)


def test_order_book_imbalance_respects_the_level_cap() -> None:
    bids = [[100.0, 1.0]] * 30
    asks = [[101.0, 1.0]] * 30

    result = order_book_imbalance(bids, asks[:1], levels=2)

    # 2 levels of bids (2.0) vs 1 level of asks (1.0)
    assert result == pytest.approx((2.0 - 1.0) / 3.0)


def test_empty_book_yields_nan() -> None:
    assert np.isnan(order_book_imbalance([], [], levels=20))


def test_volume_profile_finds_the_busiest_price() -> None:
    """Most volume transacts near 100, so the POC must land there."""
    n = 60
    prices = [100.0] * 50 + [120.0] * 10
    frame = pd.DataFrame(
        {
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "close": prices,
            "volume": [10.0] * 50 + [1.0] * 10,
        },
        dtype="float64",
    )

    result = volume_profile(frame, window=n, bins=20, value_area_fraction=0.7)

    assert result["volume_poc"].iloc[-1] == pytest.approx(100.0, abs=1.5)
    assert result["value_area_low"].iloc[-1] <= 100.0 <= result["value_area_high"].iloc[-1]


# ---- statistical ------------------------------------------------------------


def test_hurst_separates_trending_from_mean_reverting() -> None:
    """H > 0.5 for a persistent trend, H < 0.5 for a mean-reverting series."""
    rng = np.random.default_rng(21)
    trending = np.log(np.cumsum(np.full(2000, 1.0)) + 100.0)

    # AR(1) with phi = 0.4: strongly mean-reverting around a constant level.
    n = 2000
    reverting_levels = np.zeros(n)
    for i in range(1, n):
        reverting_levels[i] = 0.4 * reverting_levels[i - 1] + rng.standard_normal() * 0.01
    reverting = np.log(100.0) + reverting_levels

    assert hurst_exponent(trending, 2, 32) > 0.5
    assert hurst_exponent(reverting, 2, 32) < 0.5


def test_hurst_estimator_is_downward_biased_on_short_windows() -> None:
    """The bias that makes a naive H < 0.5 test almost useless.

    On data that is a random walk BY CONSTRUCTION (true H = 0.5), a 256-bar
    window lands near 0.46, not 0.50. This test documents the bias so that if a
    future change removes it, the calibration is revisited rather than silently
    left over-conservative.
    """
    rng = np.random.default_rng(0)
    estimates = []
    for _ in range(150):
        walk = np.log(100.0 + np.cumsum(rng.standard_normal(256)) * 0.01)
        estimates.append(hurst_exponent(walk, 2, 32))
    values = np.asarray(estimates)

    assert np.median(values) < 0.5, "the small-sample bias is the whole reason for calibration"
    assert np.mean(values < 0.5) > 0.55, "a naive H < 0.5 test mislabels most random walks"


def test_hurst_null_calibration_matches_the_estimator(config: Config) -> None:
    statistical_config = config.features.statistical

    centre, spread = hurst_null_distribution(
        statistical_config.hurst_window,
        statistical_config.hurst_min_lag,
        statistical_config.hurst_max_lag,
        statistical_config.hurst_null_samples,
        statistical_config.hurst_null_seed,
    )

    # Calibrated to the estimator's own behaviour, not the textbook 0.5.
    assert 0.40 < centre < 0.50
    assert 0.0 < spread < 0.2


def test_random_walk_is_mostly_classified_as_random_walk(config: Config) -> None:
    """Regression: the naive threshold called a pure random walk mean-reverting
    about two thirds of the time.

    Phase 5 gates mean-reversion signals on this label, so a biased classifier
    would have let them fire on data with no mean reversion in it at all.
    """
    rng = np.random.default_rng(99)
    statistical_config = config.features.statistical
    walk = pd.Series(np.log(100.0 + np.cumsum(rng.standard_normal(6000)) * 0.01), dtype="float64")
    hurst = rolling_hurst(
        walk,
        statistical_config.hurst_window,
        statistical_config.hurst_min_lag,
        statistical_config.hurst_max_lag,
    )

    regime = classify_hurst_regime(hurst, statistical_config)
    defined = regime.loc[hurst.notna()]
    random_walk_share = float((defined == 0).mean())

    assert random_walk_share > 0.75, (
        f"only {random_walk_share:.0%} of a known random walk was left unclassified; "
        f"the deadband is not absorbing estimator noise"
    )


def test_a_genuinely_trending_series_still_gets_classified(config: Config) -> None:
    """The deadband must not be so wide that nothing is ever classified."""
    statistical_config = config.features.statistical
    trending = pd.Series(np.log(np.cumsum(np.full(3000, 1.0)) + 100.0), dtype="float64")
    hurst = rolling_hurst(
        trending,
        statistical_config.hurst_window,
        statistical_config.hurst_min_lag,
        statistical_config.hurst_max_lag,
    )

    regime = classify_hurst_regime(hurst, statistical_config)
    defined = regime.loc[hurst.notna()]

    assert float((defined == 1).mean()) > 0.9


def test_hurst_of_a_zero_variation_window_is_nan_not_a_number() -> None:
    """A perfectly flat window has undefined scaling; NaN is the honest answer.

    Returning a plausible-looking 0.5 would silently classify a halted market as
    a random walk and let regime-gated signals fire on it.
    """
    assert np.isnan(hurst_exponent(np.full(200, np.log(100.0)), 2, 32))


def test_hurst_of_a_random_walk_is_near_one_half() -> None:
    rng = np.random.default_rng(7)
    walk = np.log(100.0 + np.cumsum(rng.standard_normal(4000)) * 0.01)

    result = hurst_exponent(walk, 2, 32)

    assert result == pytest.approx(0.5, abs=0.1)


def test_half_life_is_short_for_a_fast_reverting_series() -> None:
    """An AR(1) with phi=0.5 has half-life -ln2/ln(0.5) = 1 bar."""
    rng = np.random.default_rng(3)
    n = 2000
    values = np.zeros(n)
    for i in range(1, n):
        values[i] = 0.5 * values[i - 1] + rng.standard_normal() * 0.01

    result = ou_half_life(values, max_bars=500.0)

    assert result == pytest.approx(1.0, abs=0.25)


def test_half_life_of_a_random_walk_is_capped_not_infinite() -> None:
    """A non-reverting series has infinite half-life; an inf would poison scalers."""
    rng = np.random.default_rng(11)
    walk = np.cumsum(rng.standard_normal(500))

    result = ou_half_life(walk, max_bars=500.0)

    assert np.isfinite(result)
    assert result <= 500.0


# ---- base helpers -----------------------------------------------------------


def test_crossover_reports_on_the_completing_bar() -> None:
    fast = series([1, 2, 3, 2, 1])
    slow = series([2, 2, 2, 2, 2])

    result = crossover(fast, slow)

    assert result.tolist() == [0, 0, 1, 0, -1]


def test_rolling_zscore_matches_hand_computation() -> None:
    """window=3 at i=2 over [1,2,3]: mean 2, pop std 0.8164966 -> (3-2)/0.816 = 1.2247"""
    result = rolling_zscore(series([1, 2, 3, 4, 5]), 3)

    assert result.iloc[2] == pytest.approx(1.2247449, abs=1e-6)


def test_rolling_percentile_rank_is_a_fraction() -> None:
    result = rolling_percentile_rank(series([1, 2, 3, 4, 5]), 5)

    assert result.iloc[4] == pytest.approx(1.0)  # the largest so far


def test_rolling_slope_recovers_a_known_gradient() -> None:
    result = rolling_slope(series([0, 2, 4, 6, 8]), 5)

    assert result.iloc[4] == pytest.approx(2.0)


def test_safe_divide_turns_zero_denominators_into_nan() -> None:
    result = safe_divide(series([1, 2, 3]), series([1, 0, 3]))

    assert result.iloc[0] == pytest.approx(1.0)
    assert np.isnan(result.iloc[1])
    assert not np.isinf(result.to_numpy()).any()


def test_pivots_are_reported_only_once_confirmable() -> None:
    """A pivot high at index 3 needs 2 bars either side, so it is knowable at 5."""
    values = series([1, 2, 3, 9, 3, 2, 1])

    result = confirmed_pivots(values, window=2, kind="high")

    assert result.tolist() == [False, False, False, False, False, True, False]
