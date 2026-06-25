"""signaltrader — turn chat trading signals into (paper) exchange orders.

Pipeline::

    chat message --> parse_signal --> RiskConfig/size_order --> Broker.place_order

Quick start (no credentials, pure simulation)::

    from signaltrader import TradingEngine, PaperBroker

    engine = TradingEngine(PaperBroker(prices={"BTCUSDT": 65000}))
    outcome = engine.handle_message("BUY BTCUSDT market\\nSL 64000\\nTP 67000")
    print(outcome.result)

Safety: every broker defaults to dry-run / testnet. Live trading is only
enabled by an explicit opt-in (``dry_run=False`` or ``SIGNALTRADER_LIVE=1``).
"""

from .brokers import Broker, OrderRequest, OrderResult, PaperBroker, build_broker
from .engine import Outcome, TradingEngine
from .risk import RiskConfig, RiskRejection, size_order
from .signals import OrderType, Side, Signal, parse_signal

__all__ = [
    "TradingEngine",
    "Outcome",
    "Broker",
    "PaperBroker",
    "OrderRequest",
    "OrderResult",
    "build_broker",
    "RiskConfig",
    "RiskRejection",
    "size_order",
    "Signal",
    "Side",
    "OrderType",
    "parse_signal",
]
