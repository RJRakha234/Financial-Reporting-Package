"""Position sizing and pre-trade risk checks.

Sits between a parsed :class:`Signal` and the broker. It turns a signal into a
concrete :class:`OrderRequest` (deciding *how much* to trade) and rejects
anything that violates the configured limits. Keeping this separate from both
parsing and execution means the same rules apply no matter which exchange or
chat source is in use.
"""

from __future__ import annotations

from dataclasses import dataclass

from .brokers.base import OrderRequest
from .signals.models import OrderType, Signal


@dataclass
class RiskConfig:
    # Fraction of equity to allocate to a single trade (0.02 = 2%).
    risk_per_trade: float = 0.02
    # Hard cap on notional value of any one order.
    max_notional: float = 1_000.0
    # Reject leverage above this (None = leave as signalled).
    max_leverage: int | None = 10
    # Only trade symbols on this allow-list when it is non-empty.
    symbol_allowlist: tuple[str, ...] = ()
    # Refuse signals without a stop-loss.
    require_stop_loss: bool = False


class RiskRejection(Exception):
    """Raised when a signal fails a pre-trade check."""


def size_order(
    signal: Signal,
    equity: float,
    price: float,
    config: RiskConfig,
) -> OrderRequest:
    """Build an :class:`OrderRequest` from a signal, or raise ``RiskRejection``.

    Sizing precedence:
      1. an explicit quantity in the signal (capped by ``max_notional``);
      2. otherwise risk-based sizing from the stop-loss distance;
      3. otherwise a flat ``risk_per_trade`` slice of equity.
    """
    symbol = signal.symbol.upper()
    if config.symbol_allowlist and symbol not in {s.upper() for s in config.symbol_allowlist}:
        raise RiskRejection(f"{symbol} is not on the allow-list")

    if config.require_stop_loss and signal.stop_loss is None:
        raise RiskRejection(f"{symbol} signal has no stop-loss")

    if price <= 0:
        raise RiskRejection(f"non-positive price for {symbol}: {price}")

    leverage = signal.leverage
    if config.max_leverage is not None and leverage and leverage > config.max_leverage:
        leverage = config.max_leverage

    quantity = _quantity(signal, equity, price, config)
    notional = quantity * price
    if notional > config.max_notional:
        quantity = config.max_notional / price
        notional = quantity * price

    if quantity <= 0:
        raise RiskRejection(f"computed non-positive quantity for {symbol}")

    return OrderRequest(
        symbol=symbol,
        side=signal.side,
        quantity=quantity,
        order_type=signal.order_type,
        price=signal.entry_price if signal.order_type is OrderType.LIMIT else None,
        stop_loss=signal.stop_loss,
        take_profit=signal.targets[0] if signal.targets else None,
        leverage=leverage,
    )


def _quantity(signal: Signal, equity: float, price: float, config: RiskConfig) -> float:
    if signal.quantity is not None and signal.quantity > 0:
        return signal.quantity

    risk_capital = equity * config.risk_per_trade

    # Risk-based sizing: if we know the stop distance, size so that being
    # stopped out loses about ``risk_per_trade`` of equity.
    entry = signal.entry_price or price
    if signal.stop_loss is not None and entry > 0:
        stop_distance = abs(entry - signal.stop_loss)
        if stop_distance > 0:
            return risk_capital / stop_distance

    # Fallback: spend ``risk_per_trade`` of equity as notional.
    return risk_capital / price
