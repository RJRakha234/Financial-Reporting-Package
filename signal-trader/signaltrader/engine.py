"""The trading engine: signal text in, order result out.

``TradingEngine`` is the seam everything else plugs into. A signal source calls
:meth:`handle_message` with raw chat text; the engine parses it, sizes it
through the risk layer, and routes the order to the configured broker. It is
fully synchronous and side-effect-free apart from the broker call, which makes
it trivial to unit-test against :class:`PaperBroker`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .brokers.base import Broker, OrderResult
from .brokers.paper import PaperBroker
from .risk import RiskConfig, RiskRejection, size_order
from .signals.models import Signal
from .signals.parser import parse_signal

log = logging.getLogger("signaltrader")


@dataclass
class Outcome:
    """What the engine did with one message."""

    parsed: Signal | None
    result: OrderResult | None
    skipped_reason: str | None = None

    @property
    def acted(self) -> bool:
        return self.result is not None and self.result.accepted


class TradingEngine:
    def __init__(
        self,
        broker: Broker,
        risk: RiskConfig | None = None,
        equity: float | None = None,
    ) -> None:
        self.broker = broker
        self.risk = risk or RiskConfig()
        # Equity used for sizing. Paper broker reports its own; others must be
        # told (we cannot infer real account balances safely here).
        self._equity = equity

    def equity(self) -> float:
        if self._equity is not None:
            return self._equity
        if isinstance(self.broker, PaperBroker):
            return self.broker.equity()
        return 10_000.0  # conservative default when balance is unknown

    def handle_message(self, text: str) -> Outcome:
        signal = parse_signal(text)
        if signal is None:
            return Outcome(parsed=None, result=None, skipped_reason="not a signal")

        log.info("parsed signal: %s %s", signal.side.value, signal.symbol)

        price = self._reference_price(signal)
        if price is None:
            return Outcome(signal, None, skipped_reason="no price available")

        try:
            order = size_order(signal, self.equity(), price, self.risk)
        except RiskRejection as exc:
            log.warning("risk rejection: %s", exc)
            return Outcome(signal, None, skipped_reason=f"risk: {exc}")

        result = self.broker.place_order(order)
        log.info(
            "%s order %s %s qty=%.6g @ %s (dry_run=%s) -> %s",
            self.broker.name, order.side.value, order.symbol, order.quantity,
            result.price, result.dry_run, result.message,
        )
        return Outcome(signal, result)

    def _reference_price(self, signal: Signal) -> float | None:
        """Price to size against: the signal entry, else a broker quote."""
        if signal.entry_price is not None:
            return signal.entry_price
        try:
            return self.broker.get_price(signal.symbol)
        except Exception as exc:  # pragma: no cover - depends on broker
            log.warning("could not fetch price for %s: %s", signal.symbol, exc)
            return None
