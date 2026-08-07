"""The Indian equity cost stack, modelled per trade and per side.

This is the module that decides whether a backtest is honest. Nine separate
charges apply to an NSE equity round trip, they differ between intraday and
delivery, several are one-sided, and GST compounds on a subset of the others.
Approximating the lot as "0.1% per side" is the single most common way a losing
retail strategy backtests as a winner.

The two facts that matter most:

**STT is charged on both legs for delivery and on the sell leg only for
intraday**, at four times the rate. On a ₹1,00,000 round trip that is ₹201 of
STT for delivery against ₹25 for intraday — which is why a strategy that looks
marginal intraday can be hopeless as a delivery strategy, and why the two must
never share a cost assumption.

**Brokerage is capped, so it is regressive.** ``min(0.03%, ₹20)`` means a
₹10,000 trade pays 0.03% and a ₹10,00,000 trade pays 0.002%. Modelling it as a
flat percentage overstates costs for large trades and understates them for
small ones.

Every rate lives in ``config.yaml`` with an ``effective_from`` date. Rates move
by circular; applying 2026 rates to 2019 fills is its own species of look-ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nifty50.config import CostsConfig


class TradeStyle(StrEnum):
    """Which cost schedule applies.

    Not a cosmetic label: it changes STT by a factor of four, adds or removes DP
    charges, and changes stamp duty by five times.
    """

    INTRADAY = "intraday"
    DELIVERY = "delivery"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Every charge on a round trip, itemised so it can be audited line by line."""

    brokerage: float = 0.0
    securities_transaction_tax: float = 0.0
    exchange_transaction_charge: float = 0.0
    sebi_turnover_fee: float = 0.0
    investor_protection_fund_levy: float = 0.0
    stamp_duty: float = 0.0
    goods_and_services_tax: float = 0.0
    depository_participant_charge: float = 0.0
    slippage: float = 0.0
    impact_cost: float = 0.0

    @property
    def statutory(self) -> float:
        """Charges that go to the government or the exchange, not the broker."""
        return (
            self.securities_transaction_tax
            + self.exchange_transaction_charge
            + self.sebi_turnover_fee
            + self.investor_protection_fund_levy
            + self.stamp_duty
            + self.goods_and_services_tax
        )

    @property
    def execution(self) -> float:
        """What the market takes: not billed, but every bit as real."""
        return self.slippage + self.impact_cost

    @property
    def total(self) -> float:
        return self.brokerage + self.statutory + self.depository_participant_charge + self.execution

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            securities_transaction_tax=self.securities_transaction_tax
            + other.securities_transaction_tax,
            exchange_transaction_charge=self.exchange_transaction_charge
            + other.exchange_transaction_charge,
            sebi_turnover_fee=self.sebi_turnover_fee + other.sebi_turnover_fee,
            investor_protection_fund_levy=self.investor_protection_fund_levy
            + other.investor_protection_fund_levy,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            goods_and_services_tax=self.goods_and_services_tax + other.goods_and_services_tax,
            depository_participant_charge=self.depository_participant_charge
            + other.depository_participant_charge,
            slippage=self.slippage + other.slippage,
            impact_cost=self.impact_cost + other.impact_cost,
        )

    def describe(self) -> str:
        rows = [
            ("Brokerage", self.brokerage),
            ("STT", self.securities_transaction_tax),
            ("Exchange txn", self.exchange_transaction_charge),
            ("SEBI turnover fee", self.sebi_turnover_fee),
            ("IPFT", self.investor_protection_fund_levy),
            ("Stamp duty", self.stamp_duty),
            ("GST", self.goods_and_services_tax),
            ("DP charges", self.depository_participant_charge),
            ("Slippage", self.slippage),
            ("Impact cost", self.impact_cost),
        ]
        lines = [f"  {name:<20s} {value:>12,.2f}" for name, value in rows if value]
        lines.append(f"  {'TOTAL':<20s} {self.total:>12,.2f}")
        return "\n".join(lines)


class CostModel:
    """Computes the Indian cost stack from configured rates."""

    def __init__(self, config: CostsConfig, style: TradeStyle) -> None:
        self._config = config
        self._style = style

    @property
    def style(self) -> TradeStyle:
        return self._style

    # ------------------------------------------------------------- one leg

    def leg(self, turnover: float, side: Side) -> CostBreakdown:
        """Charges on a single executed order.

        ``turnover`` is price x quantity for that leg. Sign is carried by
        ``side``, not by the number, so a negative turnover is a bug rather than
        a short.
        """
        if turnover < 0:
            raise ValueError("turnover must be non-negative; use `side` for direction")
        config = self._config
        delivery = self._style is TradeStyle.DELIVERY

        brokerage = self._brokerage(turnover)
        stt = self._securities_transaction_tax(turnover, side)
        exchange = turnover * config.exchange_txn_charge_pct
        sebi = turnover * config.sebi_turnover_fee_pct
        ipft = turnover * config.ipft_pct

        stamp = 0.0
        if side is Side.BUY:
            # Stamp duty is a buy-side charge only, and the delivery rate is
            # five times the intraday one.
            rate = (
                config.stamp_duty.delivery_buy_pct
                if delivery
                else config.stamp_duty.intraday_buy_pct
            )
            stamp = turnover * rate

        # GST applies to the service charges, not to STT or stamp duty — those
        # are themselves taxes and are not taxed again.
        gst = config.gst_pct * (brokerage + exchange + sebi + ipft)

        return CostBreakdown(
            brokerage=brokerage,
            securities_transaction_tax=stt,
            exchange_transaction_charge=exchange,
            sebi_turnover_fee=sebi,
            investor_protection_fund_levy=ipft,
            stamp_duty=stamp,
            goods_and_services_tax=gst,
        )

    def _brokerage(self, turnover: float) -> float:
        config = self._config.brokerage
        if self._style is TradeStyle.DELIVERY:
            return config.delivery_flat_inr + turnover * config.delivery_pct
        # Capped percentage: regressive, so it cannot be modelled as a flat rate.
        return min(turnover * config.intraday_pct, config.intraday_cap_inr)

    def _securities_transaction_tax(self, turnover: float, side: Side) -> float:
        config = self._config.stt
        if self._style is TradeStyle.DELIVERY:
            # Charged on BOTH legs for delivery. This one line item is what kills
            # most high-frequency retail delivery strategies.
            rate = config.delivery_buy_pct if side is Side.BUY else config.delivery_sell_pct
            return turnover * rate
        return turnover * config.intraday_sell_pct if side is Side.SELL else 0.0

    # ---------------------------------------------------------- round trip

    def round_trip(
        self,
        *,
        entry_price: float,
        exit_price: float,
        quantity: float,
        slippage: float = 0.0,
        impact_cost: float = 0.0,
        scrips_sold: int = 1,
    ) -> CostBreakdown:
        """Total charges for entering and exiting one position.

        ``scrips_sold`` exists because the DP charge is levied per scrip per
        sell day regardless of quantity — a flat fee that makes small delivery
        positions disproportionately expensive.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        buy_leg = self.leg(entry_price * quantity, Side.BUY)
        sell_leg = self.leg(exit_price * quantity, Side.SELL)
        combined = buy_leg + sell_leg

        depository = 0.0
        if self._style is TradeStyle.DELIVERY:
            depository = self._config.dp_charge_per_scrip_per_sell_inr * scrips_sold

        return combined + CostBreakdown(
            depository_participant_charge=depository,
            slippage=slippage,
            impact_cost=impact_cost,
        )

    def round_trip_pct(self, price: float, quantity: float) -> float:
        """Round-trip cost as a fraction of notional, at a flat price.

        This is the number the decision engine must show next to a target: if
        the expected move is smaller than this, the signal is not worth acting
        on however confident the model is.
        """
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        notional = price * quantity
        costs = self.round_trip(entry_price=price, exit_price=price, quantity=quantity)
        return costs.total / notional


def breakeven_move_pct(model: CostModel, price: float, quantity: float) -> float:
    """The price move required just to cover costs.

    Named separately because it is the honest headline for any intraday Indian
    strategy: a system whose average winner is smaller than this loses money
    while appearing to be right more often than not.
    """
    return model.round_trip_pct(price, quantity)
