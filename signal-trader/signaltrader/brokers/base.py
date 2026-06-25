"""Broker abstraction shared by every exchange adapter."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..signals.models import OrderType, Side


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    price: float | None = None  # required for LIMIT orders
    stop_loss: float | None = None
    take_profit: float | None = None
    leverage: int | None = None


@dataclass
class OrderResult:
    accepted: bool
    broker: str
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType
    price: float | None = None
    order_id: str | None = None
    dry_run: bool = True
    message: str = ""
    raw: dict = field(default_factory=dict)


class Broker(abc.ABC):
    """Interface every exchange adapter implements.

    Implementations must honour ``dry_run``: when true they may talk to a
    *testnet* but must never place an order against real funds.
    """

    name: str = "broker"

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run

    @abc.abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:  # pragma: no cover - interface
        ...

    @abc.abstractmethod
    def get_price(self, symbol: str) -> float:  # pragma: no cover - interface
        """Return the latest traded price for ``symbol``."""
        ...
