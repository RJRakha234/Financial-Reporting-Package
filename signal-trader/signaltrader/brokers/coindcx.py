"""CoinDCX adapter — scaffold.

CoinDCX exposes a REST trading API but has no public sandbox, so like the
Zerodha adapter this one refuses to send real orders while ``dry_run`` is true
and returns a simulated result instead. The live path signs requests with
HMAC-SHA256 per the CoinDCX spec.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from ..signals.models import OrderType, Side
from .base import Broker, OrderRequest, OrderResult

_BASE_URL = "https://api.coindcx.com"


class CoinDCXBroker(Broker):
    name = "coindcx"

    def __init__(self, api_key: str, api_secret: str, dry_run: bool = True) -> None:
        super().__init__(dry_run=dry_run)
        self._api_key = api_key
        self._api_secret = api_secret

    def _signed_post(self, path: str, body: dict) -> dict:  # pragma: no cover - network
        import requests  # lazy: keep the dependency optional

        payload = json.dumps(body, separators=(",", ":"))
        signature = hmac.new(
            self._api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self._api_key,
            "X-AUTH-SIGNATURE": signature,
        }
        resp = requests.post(_BASE_URL + path, data=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_price(self, symbol: str) -> float:  # pragma: no cover - network
        import requests

        resp = requests.get(_BASE_URL + "/exchange/ticker", timeout=10)
        resp.raise_for_status()
        for row in resp.json():
            if row.get("market") == symbol.upper():
                return float(row["last_price"])
        raise KeyError(f"CoinDCX has no ticker for {symbol}")

    def place_order(self, order: OrderRequest) -> OrderResult:
        if self.dry_run:
            return OrderResult(
                accepted=True, broker=self.name, symbol=order.symbol,
                side=order.side, quantity=order.quantity,
                order_type=order.order_type, price=order.price, dry_run=True,
                message="dry-run: CoinDCX order not sent (no sandbox available)",
            )
        # pragma: no cover - network/live path
        body = {
            "side": "buy" if order.side is Side.BUY else "sell",
            "order_type": "market_order" if order.order_type is OrderType.MARKET else "limit_order",
            "market": order.symbol.upper(),
            "total_quantity": order.quantity,
            "timestamp": int(time.time() * 1000),
        }
        if order.order_type is OrderType.LIMIT:
            body["price_per_unit"] = order.price
        raw = self._signed_post("/exchange/v1/orders/create", body)
        orders = raw.get("orders") or [{}]
        return OrderResult(
            accepted=True, broker=self.name, symbol=order.symbol,
            side=order.side, quantity=order.quantity,
            order_type=order.order_type, price=order.price,
            order_id=str(orders[0].get("id", "")), dry_run=False,
            message="placed", raw=raw,
        )
