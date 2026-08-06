"""Indicator correctness against reference values.

Two kinds of assertion are used, deliberately:

*Analytic cases* — inputs whose correct output is known from the definition
(an EMA of a constant is that constant; RSI of a monotonic rise is 100). These
pin the arithmetic exactly.

*Independent reimplementation* — a slow, obviously-correct version written in
the test and compared against the vectorised one. This is the honest way to
assert "known-good" without transcribing numbers from a chart nobody can check.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from nifty50.domain import IST
from nifty50.features import core, trend, volatility

PERIOD = 14


def series(values: list[float]) -> pd.Series:
    index = pd.date_range("2025-08-04 09:15", periods=len(values), freq="15min", tz=IST)
    return pd.Series(values, index=index, dtype="float64")


def ohlc(highs: list[float], lows: list[float], closes: list[float]) -> tuple[pd.Series, ...]:
    return series(highs), series(lows), series(closes)


class TestMovingAverages:
    def test_sma_of_a_constant_is_that_constant(self) -> None:
        result = core.sma(series([5.0] * 10), 4)
        assert result.iloc[-1] == pytest.approx(5.0)
        assert result.iloc[:3].isna().all()  # warm-up is NaN, not zero

    def test_sma_matches_a_hand_computed_window(self) -> None:
        result = core.sma(series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
        assert result.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
        assert result.iloc[4] == pytest.approx(4.0)  # (3+4+5)/3

    def test_ema_of_a_constant_is_that_constant(self) -> None:
        assert core.ema(series([7.0] * 30), 9).iloc[-1] == pytest.approx(7.0)

    def test_ema_matches_the_recursive_definition(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        span = 3
        alpha = 2.0 / (span + 1)
        expected = values[0]
        for value in values[1:]:
            expected = alpha * value + (1 - alpha) * expected
        assert core.ema(series(values), span).iloc[-1] == pytest.approx(expected)

    def test_wilder_ema_uses_one_over_n_not_two_over_n_plus_one(self) -> None:
        # The distinction that makes a home-built RSI disagree with every chart.
        values = [float(v) for v in range(1, 21)]
        wilder = core.wilder_ema(series(values), PERIOD).iloc[-1]
        conventional = core.ema(series(values), PERIOD).iloc[-1]
        assert wilder != pytest.approx(conventional)

        alpha = 1.0 / PERIOD
        expected = values[0]
        for value in values[1:]:
            expected = alpha * value + (1 - alpha) * expected
        assert wilder == pytest.approx(expected)


class TestTrueRangeAndAtr:
    def test_true_range_takes_the_widest_of_the_three_spans(self) -> None:
        high, low, close = ohlc([10.0, 12.0, 11.0], [9.0, 11.0, 8.0], [9.5, 11.5, 9.0])
        result = core.true_range(high, low, close)
        assert np.isnan(result.iloc[0])  # no previous close
        # bar 1: H-L=1.0, |H-pc|=|12-9.5|=2.5, |L-pc|=|11-9.5|=1.5 -> 2.5
        assert result.iloc[1] == pytest.approx(2.5)
        # bar 2: H-L=3.0, |11-11.5|=0.5, |8-11.5|=3.5 -> 3.5
        assert result.iloc[2] == pytest.approx(3.5)

    def test_true_range_accounts_for_the_overnight_gap(self) -> None:
        # A bar that gaps far above the prior close has a true range much larger
        # than its own high-low, which is the entire point of the "true" range.
        high, low, close = ohlc([100.0, 130.0], [99.0, 129.0], [100.0, 130.0])
        result = core.true_range(high, low, close)
        assert result.iloc[1] == pytest.approx(30.0)
        assert (high - low).iloc[1] == pytest.approx(1.0)

    def test_atr_of_a_constant_range_is_that_range(self) -> None:
        n = 60
        high, low, close = ohlc([11.0] * n, [9.0] * n, [10.0] * n)
        assert volatility.atr(high, low, close, PERIOD).iloc[-1] == pytest.approx(2.0)


class TestRsi:
    def test_a_monotonic_rise_is_one_hundred(self) -> None:
        values = [float(v) for v in range(1, 40)]
        assert trend.rsi(series(values), PERIOD).iloc[-1] == pytest.approx(100.0)

    def test_a_monotonic_fall_is_zero(self) -> None:
        values = [float(v) for v in range(40, 1, -1)]
        assert trend.rsi(series(values), PERIOD).iloc[-1] == pytest.approx(0.0)

    def test_a_flat_series_is_undefined_not_overbought(self) -> None:
        # No gains and no losses: RSI has no value. Returning 100 would read as
        # maximally overbought on a stock that has not moved.
        assert np.isnan(trend.rsi(series([50.0] * 40), PERIOD).iloc[-1])

    def test_rsi_stays_within_bounds_on_a_random_walk(self) -> None:
        rng = np.random.default_rng(0)
        walk = series(list(100 + np.cumsum(rng.normal(size=500))))
        values = trend.rsi(walk, PERIOD).dropna()
        assert len(values) > 400
        assert values.between(0.0, 100.0).all()

    def test_rsi_matches_an_independent_implementation(self) -> None:
        rng = np.random.default_rng(7)
        prices = list(100 + np.cumsum(rng.normal(size=200)))
        fast = trend.rsi(series(prices), PERIOD)

        # Slow reference: explicit Wilder recursion, no pandas.
        gains, losses = [], []
        for previous, current in pairwise(prices):
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        alpha = 1.0 / PERIOD
        average_gain, average_loss = gains[0], losses[0]
        for gain, loss in zip(gains[1:], losses[1:], strict=False):
            average_gain = alpha * gain + (1 - alpha) * average_gain
            average_loss = alpha * loss + (1 - alpha) * average_loss
        expected = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
        assert fast.iloc[-1] == pytest.approx(expected, rel=1e-9)


class TestMacd:
    def test_macd_of_a_constant_is_zero(self) -> None:
        frame = trend.macd(series([25.0] * 120), 12, 26, 9)
        assert frame["macd"].iloc[-1] == pytest.approx(0.0)
        assert frame["macd_hist"].iloc[-1] == pytest.approx(0.0)

    def test_macd_is_the_difference_of_its_emas(self) -> None:
        rng = np.random.default_rng(3)
        prices = series(list(100 + np.cumsum(rng.normal(size=200))))
        frame = trend.macd(prices, 12, 26, 9)
        expected = core.ema(prices, 12) - core.ema(prices, 26)
        pd.testing.assert_series_equal(frame["macd"], expected, check_names=False, rtol=1e-12)

    def test_macd_is_positive_in_a_rising_market(self) -> None:
        frame = trend.macd(series([float(v) for v in range(1, 200)]), 12, 26, 9)
        assert frame["macd"].iloc[-1] > 0


class TestBollingerAndKeltner:
    def test_bands_of_a_constant_collapse_onto_the_middle(self) -> None:
        frame = volatility.bollinger(series([10.0] * 40), 20, 2.0)
        assert frame["bb_upper"].iloc[-1] == pytest.approx(10.0)
        assert frame["bb_lower"].iloc[-1] == pytest.approx(10.0)
        assert frame["bb_bandwidth"].iloc[-1] == pytest.approx(0.0)
        # Zero width means %B has no meaning; NaN rather than a fabricated 0.5.
        assert np.isnan(frame["bb_percent_b"].iloc[-1])

    def test_percent_b_locates_price_within_the_bands(self) -> None:
        rng = np.random.default_rng(11)
        prices = series(list(100 + np.cumsum(rng.normal(size=200))))
        frame = volatility.bollinger(prices, 20, 2.0)
        at_upper = (prices - frame["bb_lower"]) / (frame["bb_upper"] - frame["bb_lower"])
        pd.testing.assert_series_equal(
            frame["bb_percent_b"], at_upper, check_names=False, rtol=1e-12
        )

    def test_bandwidth_widens_when_volatility_rises(self) -> None:
        calm = series([100.0 + 0.01 * (i % 2) for i in range(60)])
        wild = series([100.0 + 5.0 * (i % 2) for i in range(60)])
        calm_bw = volatility.bollinger(calm, 20, 2.0)["bb_bandwidth"].iloc[-1]
        wild_bw = volatility.bollinger(wild, 20, 2.0)["bb_bandwidth"].iloc[-1]
        assert wild_bw > calm_bw

    def test_keltner_is_centred_on_its_ema(self) -> None:
        n = 80
        high, low, close = ohlc([11.0] * n, [9.0] * n, [10.0] * n)
        frame = volatility.keltner(high, low, close, ema_period=20, atr_period=10, multiplier=1.5)
        assert frame["keltner_middle"].iloc[-1] == pytest.approx(10.0)
        # ATR is 2.0 on a constant 2-point range, so the bands sit +/- 3.0.
        assert frame["keltner_upper"].iloc[-1] == pytest.approx(13.0)
        assert frame["keltner_lower"].iloc[-1] == pytest.approx(7.0)


class TestAdxAndSupertrend:
    def test_adx_is_high_in_a_clean_trend(self) -> None:
        n = 120
        closes = [100.0 + i for i in range(n)]
        high, low, close = ohlc([c + 0.5 for c in closes], [c - 0.5 for c in closes], closes)
        frame = trend.adx(high, low, close, PERIOD)
        assert frame["adx"].iloc[-1] > 50
        assert frame["plus_di"].iloc[-1] > frame["minus_di"].iloc[-1]

    def test_adx_measures_strength_not_direction(self) -> None:
        n = 120
        rising = [100.0 + i for i in range(n)]
        falling = [100.0 + n - i for i in range(n)]
        up = trend.adx(
            series([c + 0.5 for c in rising]),
            series([c - 0.5 for c in rising]),
            series(rising),
            PERIOD,
        )
        down = trend.adx(
            series([c + 0.5 for c in falling]),
            series([c - 0.5 for c in falling]),
            series(falling),
            PERIOD,
        )
        # Equally strong trends in opposite directions read the same on ADX.
        assert up["adx"].iloc[-1] == pytest.approx(down["adx"].iloc[-1], rel=0.05)
        assert down["minus_di"].iloc[-1] > down["plus_di"].iloc[-1]

    def test_supertrend_follows_a_sustained_uptrend(self) -> None:
        n = 120
        closes = [100.0 + i for i in range(n)]
        high, low, close = ohlc([c + 1.0 for c in closes], [c - 1.0 for c in closes], closes)
        atr_values = volatility.atr(high, low, close, PERIOD)
        frame = trend.supertrend(high, low, close, atr_values, 3.0)
        assert frame["supertrend_direction"].iloc[-1] == 1
        # In an uptrend the line is a trailing stop below price.
        assert frame["supertrend"].iloc[-1] < close.iloc[-1]

    def test_supertrend_flips_on_a_reversal(self) -> None:
        closes = [100.0 + i for i in range(80)] + [180.0 - 4.0 * i for i in range(40)]
        high, low, close = ohlc([c + 1.0 for c in closes], [c - 1.0 for c in closes], closes)
        atr_values = volatility.atr(high, low, close, PERIOD)
        direction = trend.supertrend(high, low, close, atr_values, 3.0)["supertrend_direction"]
        assert direction.iloc[79] == 1
        assert direction.iloc[-1] == -1


class TestIchimoku:
    def test_chikou_is_not_emitted(self) -> None:
        """Chikou is the close displaced backwards — reading it is reading ahead."""
        rng = np.random.default_rng(5)
        prices = series(list(100 + np.cumsum(rng.normal(size=200))))
        frame = trend.ichimoku(
            prices + 1, prices - 1, tenkan=9, kijun=26, senkou_b=52, displacement=26
        )
        assert not any("chikou" in column for column in frame.columns)

    def test_senkou_spans_carry_older_data_not_newer(self) -> None:
        rng = np.random.default_rng(6)
        prices = series(list(100 + np.cumsum(rng.normal(size=200))))
        frame = trend.ichimoku(
            prices + 1, prices - 1, tenkan=9, kijun=26, senkou_b=52, displacement=26
        )
        tenkan, kijun = frame["ichimoku_tenkan"], frame["ichimoku_kijun"]
        expected = ((tenkan + kijun) / 2.0).iloc[-27]
        assert frame["ichimoku_senkou_a"].iloc[-1] == pytest.approx(expected)

    def test_cloud_position(self) -> None:
        above = trend.cloud_position(
            series([100.0, 100.0]), series([90.0, 90.0]), series([80.0, 80.0])
        )
        assert (above == 1).all()
        below = trend.cloud_position(
            series([70.0, 70.0]), series([90.0, 90.0]), series([80.0, 80.0])
        )
        assert (below == -1).all()
        inside = trend.cloud_position(series([85.0]), series([90.0]), series([80.0]))
        assert inside.iloc[0] == 0


class TestCrossovers:
    def test_state_and_events(self) -> None:
        fast = series([1.0, 3.0, 3.0, 1.0, 1.0])
        slow = series([2.0, 2.0, 2.0, 2.0, 2.0])
        state = core.crossover_state(fast, slow)
        assert list(state) == [-1, 1, 1, -1, -1]
        events = core.crossover_events(fast, slow)
        # The event lands on the bar the relationship changed, not the one before.
        assert list(events) == [0, 1, 0, -1, 0]

    def test_ema_stack_score_is_full_when_perfectly_ordered(self) -> None:
        index = series([0.0, 0.0]).index
        ribbon = pd.DataFrame(
            {
                "ema_9": [4.0, 1.0],
                "ema_21": [3.0, 2.0],
                "ema_50": [2.0, 3.0],
                "ema_200": [1.0, 4.0],
            },
            index=index,
        )
        score = trend.ema_stack_score(ribbon, (9, 21, 50, 200))
        assert score.iloc[0] == pytest.approx(1.0)  # fully stacked
        assert score.iloc[1] == pytest.approx(-1.0)  # fully inverted


class TestStatisticalHelpers:
    def test_percentile_rank_of_a_rising_series_is_one(self) -> None:
        result = core.rolling_percentile_rank(series([float(v) for v in range(50)]), 20)
        assert result.iloc[-1] == pytest.approx(1.0)

    def test_percentile_rank_of_a_falling_series_is_at_the_bottom(self) -> None:
        result = core.rolling_percentile_rank(series([float(50 - v) for v in range(50)]), 20)
        assert result.iloc[-1] == pytest.approx(1.0 / 20)

    def test_slope_recovers_a_known_gradient(self) -> None:
        result = core.rolling_slope(series([3.0 * v + 10 for v in range(40)]), 10)
        assert result.iloc[-1] == pytest.approx(3.0)

    def test_slope_of_a_flat_series_is_zero(self) -> None:
        assert core.rolling_slope(series([5.0] * 40), 10).iloc[-1] == pytest.approx(0.0)

    def test_zscore_of_a_constant_is_undefined(self) -> None:
        # Zero dispersion: there is no "how unusual is this", so NaN not zero.
        assert np.isnan(core.rolling_zscore(series([5.0] * 40), 10).iloc[-1])

    def test_zscore_matches_the_definition(self) -> None:
        values = series([1.0, 2.0, 3.0, 4.0, 10.0])
        result = core.rolling_zscore(values, 5)
        window = np.array([1.0, 2.0, 3.0, 4.0, 10.0])
        expected = (10.0 - window.mean()) / window.std(ddof=0)
        assert result.iloc[-1] == pytest.approx(expected)

    def test_bars_since(self) -> None:
        flags = pd.Series([False, True, False, False, True, False], index=series([0.0] * 6).index)
        assert list(core.bars_since(flags).iloc[1:]) == [0.0, 1.0, 2.0, 0.0, 1.0]


class TestRealizedVolAndRegime:
    def test_realized_vol_of_a_constant_is_zero(self) -> None:
        result = volatility.realized_volatility(series([100.0] * 60), 20, bars_per_year=250)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_realized_vol_scales_with_the_square_root_of_time(self) -> None:
        rng = np.random.default_rng(9)
        prices = series(list(100 * np.exp(np.cumsum(rng.normal(scale=0.01, size=400)))))
        daily = volatility.realized_volatility(prices, 100, bars_per_year=250).iloc[-1]
        quadrupled = volatility.realized_volatility(prices, 100, bars_per_year=1000).iloc[-1]
        assert quadrupled == pytest.approx(2.0 * daily)

    def test_regime_labels_the_extremes(self) -> None:
        rising = series([float(v) for v in range(300)])
        regime = volatility.volatility_regime(rising, 250, low_quantile=0.25, high_quantile=0.75)
        assert regime.iloc[-1] == 1  # always at the top of its own history

    def test_vix_gate_flags_panic(self) -> None:
        vix = series([12.0, 18.0, 30.0])
        frame = volatility.vix_gate(vix, vix.index, panic_level=25.0)
        assert list(frame["india_vix_panic"]) == [False, False, True]

    def test_vix_gate_emits_no_fabricated_term_structure(self) -> None:
        # India VIX is a single 30-day index; NSE publishes no term structure.
        vix = series([12.0, 18.0])
        frame = volatility.vix_gate(vix, vix.index, panic_level=25.0)
        assert set(frame.columns) == {"india_vix", "india_vix_panic"}
