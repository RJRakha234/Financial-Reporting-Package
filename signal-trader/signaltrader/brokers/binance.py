"""Binance spot adapter.

Uses the official ``python-binance`` client when available. With ``dry_run``
enabled it points at the Binance **testnet**, so orders execute against fake
balances. Only when ``dry_run=False`` *and* live credentials are supplied does
it touch real funds.

This module imports ``binance`` lazily so the rest of the package (and the
test-suite) works without the dependency installed.
"""

from __future__ import annotations

from ..signals.models import OrderType, Side
from .base import Broker, OrderRequest, OrderResult


class BinanceBroker(Broker):
    name = "binance"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        dry_run: bool = True,
    ) -> None:
        super().__init__(dry_run=dry_run)
        try:
            from binance.client import Client  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise ImportError(
                "BinanceBroker requires 'python-binance'. "
                "Install it with: pip install python-binance"
            ) from exc

        # testnet=True keeps dry-run orders off the real exchange.
        self._client = Client(api_key, api_secret, testnet=dry_run)

    def get_price(self, symbol: str) -> float:  # pragma: no cover - network
        ticker = self._client.get_symbol_ticker(symbol=symbol.upper())
        return float(ticker["price"])

    def place_order(self, order: OrderRequest) -> OrderResult:  # pragma: no cover - network
        from binance.enums import (  # type: ignore
            ORDER_TYPE_LIMIT,
            ORDER_TYPE_MARKET,
            SIDE_BUY,
            SIDE_SELL,
            TIME_IN_FORCE_GTC,
        )

        side = SIDE_BUY if order.side is Side.BUY else SIDE_SELL
        params: dict = {
            "symbol": order.symbol.upper(),
            "side": side,
            "quantity": order.quantity,
        }
        if order.order_type is OrderType.LIMIT:
            if order.price is None:
                return OrderResult(
                    accepted=False, broker=self.name, symbol=order.symbol,
                    side=order.side, quantity=order.quantity,
                    order_type=order.order_type, dry_run=self.dry_run,
                    message="LIMIT order requires a price",
                )
            params.update(
                type=ORDER_TYPE_LIMIT,
                price=str(order.price),
                timeInForce=TIME_IN_FORCE_GTC,
            )
        else:
            params["type"] = ORDER_TYPE_MARKET

        raw = self._client.create_order(**params)
        fills = raw.get("fills") or []
        price = float(fills[0]["price"]) if fills else order.price
        return OrderResult(
            accepted=raw.get("status") in ("FILLED", "NEW", "PARTIALLY_FILLED"),
            broker=self.name,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            price=price,
            order_id=str(raw.get("orderId")),
            dry_run=self.dry_run,
            message=raw.get("status", ""),
            raw=raw,
        )
