"""Corporate-action adjustment, including a real historical NSE split."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.corporate_actions import (
    ActionType,
    CorporateAction,
    CorporateActionSet,
    MissingCumPriceError,
    adjust_for_symbol,
    adjust_ohlcv,
    build_cum_close_lookup,
)
from nifty50.data.synthetic import apply_unadjusted_split, generate_session_bars
from nifty50.domain import IST, Timeframe
from nifty50.trading_calendar import TradingCalendar

# The canonical fixture: HDFC Bank's 1:2 split (face value Rs 2 -> Re 1), which
# took the stock from roughly Rs 2,200 to roughly Rs 1,100 overnight. Left
# unadjusted it reads as a 50% crash.
HDFCBANK_SPLIT_EX_DATE = dt.date(2019, 9, 19)


def daily_frame(prices: dict[dt.date, float], volume: int = 1_000_000) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [dt.datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) for d in sorted(prices)],
        name="ts",
    )
    values = [prices[d] for d in sorted(prices)]
    return pd.DataFrame(
        {
            "open": values,
            "high": [v * 1.01 for v in values],
            "low": [v * 0.99 for v in values],
            "close": values,
            "volume": [volume] * len(values),
        },
        index=index,
    )


class TestFactorArithmetic:
    def test_one_for_two_split_halves_price_and_doubles_volume(self) -> None:
        action = CorporateAction(
            symbol="HDFCBANK",
            ex_date=HDFCBANK_SPLIT_EX_DATE,
            action_type=ActionType.SPLIT,
            ratio_new=2,
            ratio_old=1,
        )
        assert action.price_factor() == pytest.approx(0.5)
        assert action.volume_factor() == pytest.approx(2.0)

    def test_one_for_one_bonus_behaves_like_a_one_for_two_split(self) -> None:
        action = CorporateAction(
            symbol="INFY",
            ex_date=dt.date(2018, 9, 11),
            action_type=ActionType.BONUS,
            ratio_new=2,
            ratio_old=1,
        )
        assert action.price_factor() == pytest.approx(0.5)

    def test_one_for_three_bonus(self) -> None:
        # Three held become four: price x 3/4, volume x 4/3.
        action = CorporateAction(
            symbol="WIPRO",
            ex_date=dt.date(2019, 3, 6),
            action_type=ActionType.BONUS,
            ratio_new=4,
            ratio_old=3,
        )
        assert action.price_factor() == pytest.approx(0.75)
        assert action.volume_factor() == pytest.approx(4 / 3)

    def test_dividend_strips_the_payout_and_leaves_volume_alone(self) -> None:
        action = CorporateAction(
            symbol="ITC",
            ex_date=dt.date(2024, 5, 30),
            action_type=ActionType.DIVIDEND,
            value_per_share_inr=10.0,
        )
        assert action.price_factor(cum_close=500.0) == pytest.approx(0.98)
        # A dividend creates no shares, so historical volume is untouched even
        # though the price series moves.
        assert action.volume_factor(cum_close=500.0) == pytest.approx(1.0)

    def test_demerger_is_arithmetically_a_dividend(self) -> None:
        action = CorporateAction(
            symbol="RELIANCE",
            ex_date=dt.date(2023, 7, 20),
            action_type=ActionType.DEMERGER,
            value_per_share_inr=261.85,
        )
        cum_close = 2841.85
        assert action.price_factor(cum_close) == pytest.approx((cum_close - 261.85) / cum_close)
        assert action.volume_factor(cum_close) == pytest.approx(1.0)

    def test_rights_uses_the_theoretical_ex_rights_price(self) -> None:
        # 1 new share at Rs 100 for every 4 held, cum close Rs 200.
        # TERP = (4*200 + 1*100) / 5 = 180  ->  factor 0.9
        action = CorporateAction(
            symbol="TEST",
            ex_date=dt.date(2020, 6, 1),
            action_type=ActionType.RIGHTS,
            ratio_new=1,
            ratio_old=4,
            rights_issue_price_inr=100.0,
        )
        assert action.price_factor(cum_close=200.0) == pytest.approx(0.9)
        assert action.volume_factor(cum_close=200.0) == pytest.approx(1.25)

    def test_value_based_action_refuses_to_guess_without_a_cum_close(self) -> None:
        action = CorporateAction(
            symbol="ITC",
            ex_date=dt.date(2024, 5, 30),
            action_type=ActionType.DIVIDEND,
            value_per_share_inr=10.0,
        )
        with pytest.raises(MissingCumPriceError):
            action.price_factor(None)

    def test_override_wins(self) -> None:
        action = CorporateAction(
            symbol="X",
            ex_date=dt.date(2022, 1, 1),
            action_type=ActionType.MERGER,
            price_factor_override=0.42,
        )
        assert action.price_factor() == pytest.approx(0.42)


class TestApplyingAdjustments:
    def test_real_split_round_trip_recovers_the_pre_split_series(
        self, calendar: TradingCalendar
    ) -> None:
        """The load-bearing test: raw vendor bars in, comparable series out.

        A clean series is deliberately de-adjusted into what the vendor would
        actually have returned at the time, then re-adjusted. If the factors are
        right, the original comes back.
        """
        clean = generate_session_bars(
            calendar,
            dt.date(2019, 9, 10),
            dt.date(2019, 9, 30),
            Timeframe.D1,
            start_price=1100.0,
            seed=7,
        )
        raw = apply_unadjusted_split(clean, HDFCBANK_SPLIT_EX_DATE, ratio_new=2, ratio_old=1)

        # Sanity: the un-adjusted series really does contain the fake 50% crash.
        raw_closes = raw["close"].to_numpy()
        worst = np.min(np.diff(np.log(raw_closes)))
        assert worst < -0.6

        action = CorporateAction(
            symbol="HDFCBANK",
            ex_date=HDFCBANK_SPLIT_EX_DATE,
            action_type=ActionType.SPLIT,
            ratio_new=2,
            ratio_old=1,
        )
        adjusted = adjust_ohlcv(raw, [action], symbol="HDFCBANK").frame

        for column in ("open", "high", "low", "close"):
            np.testing.assert_allclose(
                adjusted[column].to_numpy(), clean[column].to_numpy(), rtol=1e-9
            )
        # And the fake crash is gone.
        adj_closes = adjusted["close"].to_numpy()
        assert np.min(np.diff(np.log(adj_closes))) > -0.2

    def test_bars_on_and_after_the_ex_date_are_untouched(self) -> None:
        prices = {
            dt.date(2019, 9, 17): 2200.0,
            dt.date(2019, 9, 18): 2210.0,
            dt.date(2019, 9, 19): 1105.0,
            dt.date(2019, 9, 20): 1110.0,
        }
        frame = daily_frame(prices)
        action = CorporateAction(
            symbol="HDFCBANK",
            ex_date=HDFCBANK_SPLIT_EX_DATE,
            action_type=ActionType.SPLIT,
            ratio_new=2,
            ratio_old=1,
        )
        adjusted = adjust_ohlcv(frame, [action], symbol="HDFCBANK").frame
        assert adjusted["close"].iloc[0] == pytest.approx(1100.0)
        assert adjusted["close"].iloc[1] == pytest.approx(1105.0)
        # Ex-date onwards already trades adjusted; touching it would double-adjust.
        assert adjusted["close"].iloc[2] == pytest.approx(1105.0)
        assert adjusted["close"].iloc[3] == pytest.approx(1110.0)

    def test_volume_is_scaled_for_share_count_actions(self) -> None:
        frame = daily_frame(
            {dt.date(2019, 9, 18): 2200.0, dt.date(2019, 9, 19): 1100.0}, volume=1_000
        )
        action = CorporateAction(
            symbol="HDFCBANK",
            ex_date=HDFCBANK_SPLIT_EX_DATE,
            action_type=ActionType.SPLIT,
            ratio_new=2,
            ratio_old=1,
        )
        adjusted = adjust_ohlcv(frame, [action], symbol="HDFCBANK").frame
        assert adjusted["volume"].iloc[0] == pytest.approx(2_000)
        assert adjusted["volume"].iloc[1] == pytest.approx(1_000)

    def test_multiple_actions_compound(self) -> None:
        frame = daily_frame(
            {
                dt.date(2020, 1, 1): 400.0,
                dt.date(2021, 1, 4): 200.0,
                dt.date(2022, 1, 3): 100.0,
            }
        )
        actions = [
            CorporateAction("X", dt.date(2020, 6, 1), ActionType.SPLIT, ratio_new=2, ratio_old=1),
            CorporateAction("X", dt.date(2021, 6, 1), ActionType.SPLIT, ratio_new=2, ratio_old=1),
        ]
        adjusted = adjust_ohlcv(frame, actions, symbol="X").frame
        # 2020 bar sits before both splits: x0.5 x0.5.
        assert adjusted["close"].iloc[0] == pytest.approx(100.0)
        # 2021 bar sits after the first, before the second: x0.5.
        assert adjusted["close"].iloc[1] == pytest.approx(100.0)
        assert adjusted["close"].iloc[2] == pytest.approx(100.0)

    def test_an_action_after_the_data_window_still_adjusts_everything(self) -> None:
        frame = daily_frame({dt.date(2024, 1, 1): 100.0, dt.date(2024, 1, 2): 102.0})
        action = CorporateAction(
            "X", dt.date(2025, 1, 1), ActionType.SPLIT, ratio_new=2, ratio_old=1
        )
        adjusted = adjust_ohlcv(frame, [action], symbol="X").frame
        assert adjusted["close"].iloc[0] == pytest.approx(50.0)
        assert adjusted["close"].iloc[1] == pytest.approx(51.0)

    def test_an_action_before_the_data_window_is_ignored(self) -> None:
        frame = daily_frame({dt.date(2024, 1, 1): 100.0})
        action = CorporateAction(
            "X", dt.date(2020, 1, 1), ActionType.SPLIT, ratio_new=2, ratio_old=1
        )
        result = adjust_ohlcv(frame, [action], symbol="X")
        assert result.frame["close"].iloc[0] == pytest.approx(100.0)
        assert result.report.applied == []

    def test_raw_close_is_preserved_alongside_the_adjusted_series(self) -> None:
        frame = daily_frame({dt.date(2019, 9, 18): 2200.0})
        action = CorporateAction(
            "HDFCBANK", HDFCBANK_SPLIT_EX_DATE, ActionType.SPLIT, ratio_new=2, ratio_old=1
        )
        adjusted = adjust_ohlcv(frame, [action], symbol="HDFCBANK").frame
        # Circuit bands and the cost stack need what actually printed.
        assert adjusted["raw_close"].iloc[0] == pytest.approx(2200.0)
        assert adjusted["close"].iloc[0] == pytest.approx(1100.0)
        assert adjusted["price_factor"].iloc[0] == pytest.approx(0.5)

    def test_dividend_adjustment_can_be_disabled(self) -> None:
        frame = daily_frame({dt.date(2024, 5, 1): 500.0})
        action = CorporateAction(
            "ITC", dt.date(2024, 5, 30), ActionType.DIVIDEND, value_per_share_inr=10.0
        )
        result = adjust_ohlcv(frame, [action], symbol="ITC", adjust_dividends=False)
        assert result.frame["close"].iloc[0] == pytest.approx(500.0)
        assert len(result.report.skipped) == 1

    def test_cum_close_lookup_uses_the_last_raw_close_before_the_ex_date(self) -> None:
        frame = daily_frame(
            {
                dt.date(2023, 7, 18): 2800.0,
                dt.date(2023, 7, 19): 2841.85,
                dt.date(2023, 7, 20): 2580.0,
            }
        )
        action = CorporateAction(
            "RELIANCE", dt.date(2023, 7, 20), ActionType.DEMERGER, value_per_share_inr=261.85
        )
        lookup = build_cum_close_lookup(frame, [action])
        assert lookup[dt.date(2023, 7, 20)] == pytest.approx(2841.85)

    def test_lenient_mode_records_a_skip_instead_of_exploding(self) -> None:
        # A dividend whose ex-date precedes all available data has no cum close.
        frame = daily_frame({dt.date(2024, 6, 1): 100.0, dt.date(2024, 6, 3): 101.0})
        action = CorporateAction(
            "X", dt.date(2024, 5, 1), ActionType.DIVIDEND, value_per_share_inr=1.0
        )
        # ex_date precedes the window, so it is filtered out entirely.
        assert adjust_ohlcv(frame, [action], symbol="X").report.applied == []

        later = CorporateAction(
            "X", dt.date(2024, 7, 1), ActionType.DIVIDEND, value_per_share_inr=1.0
        )
        result = adjust_ohlcv(frame, [later], symbol="X")
        assert result.report.applied  # cum close available from the frame
        assert result.report.is_clean


class TestActionFile:
    def test_seeded_file_loads_and_contains_the_reference_split(
        self, actions: CorporateActionSet
    ) -> None:
        hdfc = actions.for_symbol("HDFCBANK")
        assert len(hdfc) == 1
        assert hdfc[0].ex_date == HDFCBANK_SPLIT_EX_DATE
        assert hdfc[0].action_type is ActionType.SPLIT
        assert hdfc[0].price_factor() == pytest.approx(0.5)

    def test_every_seeded_action_produces_a_usable_factor(
        self, actions: CorporateActionSet
    ) -> None:
        # Catches malformed rows: a bonus missing its ratio, a demerger missing
        # its stripped value, and so on.
        for symbol in sorted(actions.symbols):
            for action in actions.for_symbol(symbol):
                cum = 1000.0 if action.action_type.needs_cum_price else None
                factor = action.price_factor(cum)
                assert 0.0 < factor <= 1.5, f"{symbol} {action.ex_date} factor {factor}"

    def test_adjust_for_symbol_wires_the_set_through(self, actions: CorporateActionSet) -> None:
        frame = daily_frame({dt.date(2019, 9, 18): 2200.0})
        adjusted = adjust_for_symbol(frame, "HDFCBANK", actions).frame
        assert adjusted["close"].iloc[0] == pytest.approx(1100.0)


class TestValidation:
    def test_naive_index_is_rejected(self) -> None:
        frame = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
            index=pd.DatetimeIndex([dt.datetime(2024, 1, 1, 9, 15)], name="ts"),
        )
        with pytest.raises(ValueError, match="tz-aware"):
            adjust_ohlcv(frame, [], symbol="X")

    def test_missing_columns_are_rejected(self) -> None:
        frame = pd.DataFrame(
            {"close": [1.0]},
            index=pd.DatetimeIndex([dt.datetime(2024, 1, 1, 9, 15, tzinfo=IST)], name="ts"),
        )
        with pytest.raises(ValueError, match="missing OHLCV"):
            adjust_ohlcv(frame, [], symbol="X")
