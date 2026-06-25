"""Structured representation of a trading signal."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class Signal:
    """A parsed trading instruction extracted from a chat message.

    Only ``symbol`` and ``side`` are required for a signal to be actionable.
    Everything else is optional context that callers (risk engine, broker) may
    use or ignore.
    """

    symbol: str
    side: Side
    order_type: OrderType = OrderType.MARKET
    # A single entry price, or the low/high of an entry zone (entry_high set).
    entry: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    targets: list[float] = field(default_factory=list)
    leverage: int | None = None
    # Explicit quantity from the signal, if the author specified one.
    quantity: float | None = None
    raw: str = ""

    def __post_init__(self) -> None:
        # Normalise to enums so callers can rely on the type regardless of how
        # the object was constructed.
        self.symbol = self.symbol.upper().strip()
        if not isinstance(self.side, Side):
            self.side = Side(str(self.side).upper())
        if not isinstance(self.order_type, OrderType):
            self.order_type = OrderType(str(self.order_type).upper())

    @property
    def entry_price(self) -> float | None:
        """Single representative entry price (midpoint of a zone)."""
        if self.entry is None:
            return None
        if self.entry_high is None:
            return self.entry
        return (self.entry + self.entry_high) / 2
