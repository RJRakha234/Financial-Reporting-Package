"""Broker adapters and a small factory keyed by name."""

from __future__ import annotations

from .base import Broker, OrderRequest, OrderResult
from .paper import PaperBroker

__all__ = [
    "Broker",
    "OrderRequest",
    "OrderResult",
    "PaperBroker",
    "build_broker",
]


def build_broker(name: str, config: dict, dry_run: bool = True) -> Broker:
    """Construct a broker by ``name`` using values from ``config``.

    Adapters with heavy or optional dependencies are imported lazily so the
    paper path never pays for them.
    """
    name = name.lower()
    if name == "paper":
        return PaperBroker(
            starting_cash=config.get("starting_cash", 10_000.0),
            prices=config.get("prices"),
        )
    if name == "binance":
        from .binance import BinanceBroker

        return BinanceBroker(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            dry_run=dry_run,
        )
    if name == "coindcx":
        from .coindcx import CoinDCXBroker

        return CoinDCXBroker(
            api_key=config["api_key"],
            api_secret=config["api_secret"],
            dry_run=dry_run,
        )
    if name == "zerodha":
        from .zerodha import ZerodhaBroker

        return ZerodhaBroker(
            api_key=config["api_key"],
            access_token=config.get("access_token"),
            dry_run=dry_run,
            exchange=config.get("exchange", "NSE"),
            product=config.get("product", "MIS"),
        )
    raise ValueError(f"unknown broker: {name!r}")
