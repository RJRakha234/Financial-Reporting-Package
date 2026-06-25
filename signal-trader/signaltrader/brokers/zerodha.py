"""Zerodha (Kite Connect) adapter — scaffold.

Kite Connect has no sandbox: every order hits the live market, and the API
also requires a fresh access token generated through an interactive login each
trading day. Because of that this adapter refuses to place orders while
``dry_run`` is true (the default) and instead returns a simulated result, so
you can wire up and test the pipeline without risking real trades.

Flip ``dry_run=False`` and supply a valid ``access_token`` only once you have
completed the Kite login flow and genuinely want live orders.
"""

from __future__ import annotations

from ..signals.models import OrderType, Side
from .base import Broker, OrderRequest, OrderResult


class ZerodhaBroker(Broker):
    name = "zerodha"

    def __init__(
        self,
        api_key: str,
        access_token: str | None = None,
        dry_run: bool = True,
        exchange: str = "NSE",
        product: str = "MIS",
    ) -> None:
        super().__init__(dry_run=dry_run)
        self.exchange = exchange
        self.product = product
        self._kite = None
        if not dry_run:
            try:
                from kiteconnect import KiteConnect  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dep
                raise ImportError(
                    "ZerodhaBroker live mode requires 'kiteconnect'. "
                    "Install it with: pip install kiteconnect"
                ) from exc
            if not access_token:
                raise ValueError(
                    "Live Zerodha trading requires an access_token from the "
                    "daily Kite login flow."
                )
            self._kite = KiteConnect(api_key=api_key)
            self._kite.set_access_token(access_token)

    def get_price(self, symbol: str) -> float:  # pragma: no cover - network
        if self._kite is None:
            raise RuntimeError("get_price needs a live Kite session (dry_run=False).")
        instrument = f"{self.exchange}:{symbol.upper()}"
        quote = self._kite.ltp([instrument])
        return float(quote[instrument]["last_price"])

    def place_order(self, order: OrderRequest) -> OrderResult:
        if self.dry_run or self._kite is None:
            return OrderResult(
                accepted=True, broker=self.name, symbol=order.symbol,
                side=order.side, quantity=order.quantity,
                order_type=order.order_type, price=order.price, dry_run=True,
                message="dry-run: Kite order not sent (no sandbox available)",
            )
        # pragma: no cover - network/live path
        kite = self._kite
        txn = kite.TRANSACTION_TYPE_BUY if order.side is Side.BUY else kite.TRANSACTION_TYPE_SELL
        otype = kite.ORDER_TYPE_LIMIT if order.order_type is OrderType.LIMIT else kite.ORDER_TYPE_MARKET
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=self.exchange,
            tradingsymbol=order.symbol.upper(),
            transaction_type=txn,
            quantity=int(order.quantity),
            product=self.product,
            order_type=otype,
            price=order.price,
        )
        return OrderResult(
            accepted=True, broker=self.name, symbol=order.symbol,
            side=order.side, quantity=order.quantity,
            order_type=order.order_type, price=order.price,
            order_id=str(order_id), dry_run=False, message="placed",
        )
