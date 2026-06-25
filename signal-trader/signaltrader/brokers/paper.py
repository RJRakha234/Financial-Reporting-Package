"""A pure-simulation broker. No network, no credentials, no risk.

The paper broker tracks a virtual cash balance and open positions so you can
exercise the full signal -> risk -> order pipeline end to end and inspect the
resulting fills. It is the default broker and the one the test-suite uses.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..signals.models import OrderType, Side
from .base import Broker, OrderRequest, OrderResult


@dataclass
class Position:
    symbol: str
    quantity: float  # signed: positive = long, negative = short
    avg_price: float


class PaperBroker(Broker):
    name = "paper"

    def __init__(
        self,
        starting_cash: float = 10_000.0,
        prices: dict[str, float] | None = None,
    ) -> None:
        super().__init__(dry_run=True)
        self.cash = starting_cash
        self.starting_cash = starting_cash
        # A static price book used when no live price is supplied. Real signal
        # entries override these via OrderRequest.price.
        self._prices = {k.upper(): v for k, v in (prices or {}).items()}
        self.positions: dict[str, Position] = {}
        self.fills: list[OrderResult] = []
        self._ids = itertools.count(1)

    def get_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol not in self._prices:
            raise KeyError(
                f"PaperBroker has no price for {symbol}; pass one via "
                f"OrderRequest.price or seed it in the constructor."
            )
        return self._prices[symbol]

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol.upper()] = price

    def place_order(self, order: OrderRequest) -> OrderResult:
        fill_price = order.price
        if fill_price is None:
            try:
                fill_price = self.get_price(order.symbol)
            except KeyError as exc:
                return self._reject(order, str(exc))

        cost = fill_price * order.quantity
        if order.side is Side.BUY and cost > self.cash:
            return self._reject(
                order,
                f"insufficient paper cash: need {cost:.2f}, have {self.cash:.2f}",
            )

        self._apply_fill(order, fill_price)

        result = OrderResult(
            accepted=True,
            broker=self.name,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            price=fill_price,
            order_id=f"paper-{next(self._ids)}",
            dry_run=True,
            message="simulated fill",
        )
        self.fills.append(result)
        return result

    def _apply_fill(self, order: OrderRequest, price: float) -> None:
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        self.cash -= price * signed

        pos = self.positions.get(order.symbol)
        if pos is None:
            self.positions[order.symbol] = Position(order.symbol, signed, price)
            return

        new_qty = pos.quantity + signed
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            # Same direction (or opening): blend the average price.
            total = pos.quantity + signed
            if total != 0:
                pos.avg_price = (
                    pos.avg_price * pos.quantity + price * signed
                ) / total
        pos.quantity = new_qty
        if abs(pos.quantity) < 1e-12:
            del self.positions[order.symbol]

    def _reject(self, order: OrderRequest, message: str) -> OrderResult:
        result = OrderResult(
            accepted=False,
            broker=self.name,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            price=order.price,
            dry_run=True,
            message=message,
        )
        self.fills.append(result)
        return result

    def equity(self) -> float:
        """Cash plus marked-to-market value of open positions."""
        value = self.cash
        for pos in self.positions.values():
            try:
                value += pos.quantity * self.get_price(pos.symbol)
            except KeyError:
                value += pos.quantity * pos.avg_price
        return value
