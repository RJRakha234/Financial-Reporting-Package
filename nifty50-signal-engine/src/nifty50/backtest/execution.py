"""Fill modelling: slippage, impact, circuit limits and settlement.

A backtest's fill assumptions decide its results more than its signal logic
does. Four rules are enforced here, each corresponding to a way a naive engine
manufactures profit that was never available:

**Fills happen at the NEXT bar's open, never at the signal bar's close.** A
decision made from a bar can only be acted on after that bar has closed.

**Nothing fills through a circuit limit.** When a stock is frozen at its band
there is no counterparty. An engine that fills at the limit price on a
limit-up day is buying from nobody.

**Slippage is modelled from spread and size, not assumed flat.** A flat 5bps is
optimistic for a small-cap at the open and pessimistic for RELIANCE at midday.

**Delivery capital is locked until T+1.** Proceeds from a sale are not available
to buy again the same session.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from nifty50.backtest.costs import Side
from nifty50.config import CostsConfig

_BPS: float = 1e-4


class FillRejection(StrEnum):
    """Why an order could not be filled. Rejections are recorded, not silent."""

    CIRCUIT_LIMIT = "circuit_limit"
    NO_LIQUIDITY = "no_liquidity"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    NOT_IN_UNIVERSE = "not_in_universe"
    MARKET_CLOSED = "market_closed"


@dataclass(frozen=True, slots=True)
class PriceBand:
    """The circuit limits in force for a symbol on a session.

    NSE applies 2/5/10/20% bands depending on the security. Inside a band a
    stock trades normally; at the band it freezes.
    """

    lower: float
    upper: float

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    @classmethod
    def from_previous_close(cls, previous_close: float, band_pct: float) -> PriceBand:
        return cls(
            lower=previous_close * (1.0 - band_pct),
            upper=previous_close * (1.0 + band_pct),
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """An executed order, or the reason there wasn't one."""

    timestamp: dt.datetime
    symbol: str
    side: Side
    quantity: float
    price: float
    slippage: float
    impact_cost: float
    rejected: FillRejection | None = None

    @property
    def filled(self) -> bool:
        return self.rejected is None and self.quantity > 0

    @property
    def turnover(self) -> float:
        return self.price * self.quantity


class SlippageModel:
    """Spread- and size-aware slippage.

    Two components: half the bid-ask spread, always paid when crossing, plus an
    impact term that grows with the size of the order relative to the bar's
    traded volume. The impact term is what stops a backtest assuming it can put
    ₹50 lakh through a stock that traded ₹2 lakh that minute.
    """

    def __init__(
        self,
        config: CostsConfig,
        *,
        participation_impact_coefficient: float = 0.1,
        max_participation: float = 0.1,
    ) -> None:
        self._config = config
        self._impact_coefficient = participation_impact_coefficient
        self._max_participation = max_participation

    @property
    def max_participation(self) -> float:
        """Largest share of a bar's volume an order may take."""
        return self._max_participation

    def half_spread(self, price: float, spread: float | None) -> float:
        """Cost per share of crossing the spread.

        Falls back to the configured flat estimate only when no spread is
        available — which for historical bars is always, and is recorded as an
        assumption in the README rather than hidden here.
        """
        if spread is not None and spread > 0:
            return spread / 2.0
        return price * self._config.slippage.fallback_bps * _BPS

    def impact_per_share(self, price: float, quantity: float, bar_volume: float | None) -> float:
        """Price concession from consuming a share of the bar's liquidity.

        Square-root of participation, the standard shape: taking 1% of a bar's
        volume costs materially less per share than taking 10%.
        """
        if bar_volume is None or bar_volume <= 0 or quantity <= 0:
            return 0.0
        participation = min(quantity / bar_volume, 1.0)
        return price * self._impact_coefficient * float(participation**0.5)

    def executable_quantity(self, desired: float, bar_volume: float | None) -> float:
        """Cap an order at a plausible share of the bar's traded volume.

        Without this, a backtest silently assumes infinite liquidity and books
        profits on size that could never have been executed.
        """
        if bar_volume is None or bar_volume <= 0:
            return 0.0
        return min(desired, bar_volume * self._max_participation)


class ExecutionSimulator:
    """Turns an intended trade into a fill, or a recorded rejection."""

    def __init__(self, slippage: SlippageModel) -> None:
        self._slippage = slippage

    def execute(
        self,
        *,
        timestamp: dt.datetime,
        symbol: str,
        side: Side,
        desired_quantity: float,
        reference_price: float,
        bar_volume: float | None,
        band: PriceBand | None = None,
        spread: float | None = None,
    ) -> Fill:
        """Fill at ``reference_price`` adjusted for slippage, or reject."""
        if desired_quantity <= 0:
            return self._reject(timestamp, symbol, side, FillRejection.NO_LIQUIDITY)

        quantity = self._slippage.executable_quantity(desired_quantity, bar_volume)
        if quantity <= 0:
            return self._reject(timestamp, symbol, side, FillRejection.NO_LIQUIDITY)

        half_spread = self._slippage.half_spread(reference_price, spread)
        impact = self._slippage.impact_per_share(reference_price, quantity, bar_volume)
        # Both costs always move against the trader: pay up to buy, sell down.
        direction = 1.0 if side is Side.BUY else -1.0
        fill_price = reference_price + direction * (half_spread + impact)

        if band is not None and not band.contains(fill_price):
            # Frozen at the band means there is no counterparty at that price.
            # Refusing the fill is the whole point; clamping it to the limit
            # would book a trade that could not have happened.
            return self._reject(timestamp, symbol, side, FillRejection.CIRCUIT_LIMIT)

        return Fill(
            timestamp=timestamp,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=fill_price,
            slippage=half_spread * quantity,
            impact_cost=impact * quantity,
        )

    @staticmethod
    def _reject(timestamp: dt.datetime, symbol: str, side: Side, reason: FillRejection) -> Fill:
        return Fill(
            timestamp=timestamp,
            symbol=symbol,
            side=side,
            quantity=0.0,
            price=0.0,
            slippage=0.0,
            impact_cost=0.0,
            rejected=reason,
        )


@dataclass(slots=True)
class SettlementLedger:
    """Tracks cash locked in unsettled sales.

    Under T+1 the proceeds of today's delivery sale are not available to buy
    with until tomorrow. Ignoring this lets a backtest recycle the same rupee
    several times a day and overstate both turnover and returns.
    """

    settlement_days: int
    pending: dict[dt.date, float]

    def __init__(self, settlement_days: int) -> None:
        self.settlement_days = settlement_days
        self.pending = {}

    def record_sale(self, proceeds: float, available_on: dt.date) -> None:
        self.pending[available_on] = self.pending.get(available_on, 0.0) + proceeds

    def release(self, today: dt.date) -> float:
        """Cash that has settled as of ``today``, removed from the ledger."""
        due = [day for day in self.pending if day <= today]
        released = sum(self.pending.pop(day) for day in due)
        return float(released)

    @property
    def unsettled(self) -> float:
        return float(sum(self.pending.values()))
