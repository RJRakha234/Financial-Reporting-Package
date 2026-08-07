"""The Indian cost stack, hand-computed line by line.

Every expected number below is derived from the configured rate in the test
itself, so a rate change in ``config.yaml`` shows up as a failure with an
explicit arithmetic trail rather than as a silently different backtest.
"""

from __future__ import annotations

import pytest

from nifty50.backtest.costs import CostModel, Side, TradeStyle, breakeven_move_pct
from nifty50.config import Config

# A round trip large enough that the brokerage cap actually binds.
QUANTITY = 100.0
ENTRY = 1000.0
EXIT = 1010.0
BUY_TURNOVER = ENTRY * QUANTITY  # 1,00,000
SELL_TURNOVER = EXIT * QUANTITY  # 1,01,000
TOTAL_TURNOVER = BUY_TURNOVER + SELL_TURNOVER


@pytest.fixture
def intraday(real_config: Config) -> CostModel:
    return CostModel(real_config.costs, TradeStyle.INTRADAY)


@pytest.fixture
def delivery(real_config: Config) -> CostModel:
    return CostModel(real_config.costs, TradeStyle.DELIVERY)


class TestIntradayRoundTrip:
    def test_every_line_item(self, intraday: CostModel, real_config: Config) -> None:
        rates = real_config.costs
        costs = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)

        # Brokerage: min(0.03% x turnover, Rs 20) per leg — the cap binds here.
        cap = rates.brokerage.intraday_cap_inr
        assert costs.brokerage == pytest.approx(2 * cap)
        assert BUY_TURNOVER * rates.brokerage.intraday_pct > cap  # cap really binds

        # STT: sell leg only for intraday.
        assert costs.securities_transaction_tax == pytest.approx(
            SELL_TURNOVER * rates.stt.intraday_sell_pct
        )

        # Turnover-based charges apply to both legs.
        assert costs.exchange_transaction_charge == pytest.approx(
            TOTAL_TURNOVER * rates.exchange_txn_charge_pct
        )
        assert costs.sebi_turnover_fee == pytest.approx(
            TOTAL_TURNOVER * rates.sebi_turnover_fee_pct
        )
        assert costs.investor_protection_fund_levy == pytest.approx(TOTAL_TURNOVER * rates.ipft_pct)

        # Stamp duty: buy side only.
        assert costs.stamp_duty == pytest.approx(BUY_TURNOVER * rates.stamp_duty.intraday_buy_pct)

        # GST on the service charges only — never on STT or stamp duty, which
        # are taxes themselves.
        taxable = (
            costs.brokerage
            + costs.exchange_transaction_charge
            + costs.sebi_turnover_fee
            + costs.investor_protection_fund_levy
        )
        assert costs.goods_and_services_tax == pytest.approx(rates.gst_pct * taxable)

        # No DP charge on intraday: nothing is debited from the demat account.
        assert costs.depository_participant_charge == 0.0

    def test_the_total_is_the_sum_of_its_parts(self, intraday: CostModel) -> None:
        costs = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        assert costs.total == pytest.approx(
            costs.brokerage
            + costs.statutory
            + costs.depository_participant_charge
            + costs.execution
        )

    def test_costs_eat_a_visible_share_of_a_one_percent_move(self, intraday: CostModel) -> None:
        costs = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        gross = (EXIT - ENTRY) * QUANTITY  # Rs 1,000 on a 1% move
        assert gross == pytest.approx(1000.0)
        # Roughly 8% of the gross profit on a clean 1% winner, before slippage.
        assert 0.05 < costs.total / gross < 0.12


class TestDeliveryIsADifferentAnimal:
    def test_stt_is_charged_on_both_legs_at_four_times_the_rate(
        self, delivery: CostModel, real_config: Config
    ) -> None:
        rates = real_config.costs
        costs = delivery.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        expected = (
            BUY_TURNOVER * rates.stt.delivery_buy_pct + SELL_TURNOVER * rates.stt.delivery_sell_pct
        )
        assert costs.securities_transaction_tax == pytest.approx(expected)

    def test_delivery_stt_dwarfs_intraday_stt(
        self, delivery: CostModel, intraday: CostModel
    ) -> None:
        """The line item that kills high-frequency retail delivery strategies."""
        as_delivery = delivery.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        as_intraday = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        assert as_delivery.securities_transaction_tax > (7 * as_intraday.securities_transaction_tax)

    def test_the_dp_charge_is_flat_per_scrip_not_per_share(
        self, delivery: CostModel, real_config: Config
    ) -> None:
        small = delivery.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=1)
        large = delivery.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=10_000)
        flat = real_config.costs.dp_charge_per_scrip_per_sell_inr
        assert small.depository_participant_charge == pytest.approx(flat)
        assert large.depository_participant_charge == pytest.approx(flat)

    def test_the_flat_dp_charge_punishes_small_delivery_positions(
        self, delivery: CostModel
    ) -> None:
        # A flat fee is a large percentage of a small notional; this is why tiny
        # delivery positions are uneconomic regardless of the strategy.
        small = delivery.round_trip_pct(price=100.0, quantity=5)
        large = delivery.round_trip_pct(price=100.0, quantity=5_000)
        assert small > 5 * large

    def test_delivery_costs_more_than_intraday_overall(
        self, delivery: CostModel, intraday: CostModel
    ) -> None:
        as_delivery = delivery.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        as_intraday = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY)
        assert as_delivery.total > as_intraday.total


class TestBrokerageIsRegressive:
    def test_the_cap_binds_on_large_trades_only(
        self, intraday: CostModel, real_config: Config
    ) -> None:
        rates = real_config.costs.brokerage
        # Small trade: the percentage is below the cap, so the percentage wins.
        small_turnover = 10_000.0
        small = intraday.leg(small_turnover, Side.BUY)
        assert small.brokerage == pytest.approx(small_turnover * rates.intraday_pct)
        assert small.brokerage < rates.intraday_cap_inr

        # Large trade: the cap wins.
        large = intraday.leg(10_00_000.0, Side.BUY)
        assert large.brokerage == pytest.approx(rates.intraday_cap_inr)

    def test_effective_brokerage_rate_falls_with_size(self, intraday: CostModel) -> None:
        small_rate = intraday.leg(10_000.0, Side.BUY).brokerage / 10_000.0
        large_rate = intraday.leg(10_00_000.0, Side.BUY).brokerage / 10_00_000.0
        assert small_rate > large_rate


class TestSidedCharges:
    def test_stamp_duty_is_buy_side_only(self, intraday: CostModel) -> None:
        assert intraday.leg(BUY_TURNOVER, Side.BUY).stamp_duty > 0
        assert intraday.leg(SELL_TURNOVER, Side.SELL).stamp_duty == 0.0

    def test_intraday_stt_is_sell_side_only(self, intraday: CostModel) -> None:
        assert intraday.leg(BUY_TURNOVER, Side.BUY).securities_transaction_tax == 0.0
        assert intraday.leg(SELL_TURNOVER, Side.SELL).securities_transaction_tax > 0

    def test_delivery_stt_is_charged_on_both_sides(self, delivery: CostModel) -> None:
        assert delivery.leg(BUY_TURNOVER, Side.BUY).securities_transaction_tax > 0
        assert delivery.leg(SELL_TURNOVER, Side.SELL).securities_transaction_tax > 0

    def test_gst_never_taxes_a_tax(self, intraday: CostModel, real_config: Config) -> None:
        leg = intraday.leg(BUY_TURNOVER, Side.BUY)
        taxable = (
            leg.brokerage
            + leg.exchange_transaction_charge
            + leg.sebi_turnover_fee
            + leg.investor_protection_fund_levy
        )
        assert leg.goods_and_services_tax == pytest.approx(real_config.costs.gst_pct * taxable)
        # Explicitly not including stamp duty or STT in the GST base.
        assert leg.stamp_duty > 0
        assert leg.goods_and_services_tax < real_config.costs.gst_pct * (taxable + leg.stamp_duty)


class TestBreakeven:
    def test_the_breakeven_move_is_reported_as_a_fraction(self, intraday: CostModel) -> None:
        breakeven = breakeven_move_pct(intraday, price=1000.0, quantity=100)
        # A few basis points to a fraction of a percent for a liquid large-cap.
        assert 0.0005 < breakeven < 0.005

    def test_small_intraday_trades_need_an_implausible_move_to_break_even(
        self, intraday: CostModel
    ) -> None:
        # Rs 5,000 notional: the Rs 20 brokerage floor alone is 0.4% per side.
        breakeven = breakeven_move_pct(intraday, price=100.0, quantity=50)
        assert breakeven > 0.001

    def test_execution_costs_are_carried_separately_from_statutory_ones(
        self, intraday: CostModel
    ) -> None:
        costs = intraday.round_trip(
            entry_price=ENTRY,
            exit_price=EXIT,
            quantity=QUANTITY,
            slippage=250.0,
            impact_cost=75.0,
        )
        assert costs.execution == pytest.approx(325.0)
        # Slippage is not billed, so it must not appear in the statutory bucket.
        assert costs.slippage not in (costs.statutory, costs.brokerage)
        assert costs.total == pytest.approx(
            costs.brokerage + costs.statutory + costs.depository_participant_charge + 325.0
        )


class TestValidation:
    def test_negative_turnover_is_rejected(self, intraday: CostModel) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            intraday.leg(-1.0, Side.BUY)

    def test_non_positive_quantity_is_rejected(self, intraday: CostModel) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=0)

    def test_breakdowns_add(self, intraday: CostModel) -> None:
        one = intraday.leg(BUY_TURNOVER, Side.BUY)
        two = intraday.leg(SELL_TURNOVER, Side.SELL)
        combined = one + two
        assert combined.total == pytest.approx(one.total + two.total)
        assert combined.brokerage == pytest.approx(one.brokerage + two.brokerage)

    def test_describe_lists_the_charges(self, intraday: CostModel) -> None:
        text = intraday.round_trip(entry_price=ENTRY, exit_price=EXIT, quantity=QUANTITY).describe()
        assert "STT" in text
        assert "TOTAL" in text
