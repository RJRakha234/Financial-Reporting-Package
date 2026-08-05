"""Binance public REST client.

Scope is deliberately tiny: market data only. The alert-only guarantee in the
spec is enforced *structurally* here rather than by convention —
:data:`PUBLIC_PATHS` is an allowlist, :meth:`BinanceRestClient._request` refuses
anything outside it, and only GET is ever issued. There is no code path in this
package that can reach ``/api/v3/order``, so no API key with trading permission
is ever required (none is required at all: every endpoint used is public).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx
import pandas as pd

from cse.config import RateLimitConfig, RestConfig
from cse.data.ratelimit import RateLimitExceededError, WeightLimiter
from cse.data.schema import candles_from_rest
from cse.logging import get_logger

_log = get_logger(__name__)

# Every endpoint this package is permitted to call. Read-only market data.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/v3/ping",
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/klines",
        "/api/v3/depth",
        "/api/v3/ticker/24hr",
        "/api/v3/trades",
        "/api/v3/aggTrades",
    }
)

USED_WEIGHT_HEADER: Final[str] = "X-MBX-USED-WEIGHT-1M"
RETRY_AFTER_HEADER: Final[str] = "Retry-After"

HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_IP_BANNED: Final[int] = 418
HTTP_SERVER_ERROR_FLOOR: Final[int] = 500


class ForbiddenEndpointError(RuntimeError):
    """Raised if any caller attempts an endpoint outside the public allowlist."""


class BinanceRestError(RuntimeError):
    """A non-retryable error response from Binance."""

    def __init__(self, status_code: int, payload: object) -> None:
        super().__init__(f"binance rest error {status_code}: {payload!r}")
        self.status_code = status_code
        self.payload = payload


class EgressBlockedError(RuntimeError):
    """Outbound access to the exchange is blocked by network policy.

    Raised when an egress proxy answers a CONNECT with 403/407. This is a
    *decision*, not an outage: the same request will be refused every time, so
    retrying wastes the backoff budget and buries the real cause under transport
    errors. Fail immediately and say what is actually wrong.
    """


def _is_policy_denial(exc: Exception) -> bool:
    """True if the failure is a proxy refusing the tunnel on policy grounds."""
    if not isinstance(exc, httpx.ProxyError):
        return False
    message = str(exc)
    return "403" in message or "407" in message


@dataclass
class _Attempt:
    """Bookkeeping for the retry loop."""

    number: int = 0
    delay: float = 0.0


class BinanceRestClient:
    """Async client for Binance public market-data endpoints.

    Handles weight accounting, adaptive backoff, and transparent failover to the
    ``data-api.binance.vision`` mirror, which serves identical public market data
    and is useful when the primary host is geo-blocked.
    """

    def __init__(
        self,
        rest_config: RestConfig,
        rate_limit_config: RateLimitConfig,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: WeightLimiter | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = rest_config
        self._limiter = limiter or WeightLimiter(rate_limit_config)
        self._rng = rng or random.Random()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=rest_config.timeout_seconds,
            headers={"User-Agent": "crypto-signal-engine/0.1 (alert-only)"},
            follow_redirects=True,
        )
        self._base_url = rest_config.base_url
        self._using_fallback = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def limiter(self) -> WeightLimiter:
        return self._limiter

    @property
    def base_url(self) -> str:
        return self._base_url

    def _jittered(self, delay: float) -> float:
        """Apply symmetric jitter so concurrent workers do not retry in lockstep."""
        jitter = self._config.backoff_jitter
        if jitter <= 0.0:
            return delay
        return max(0.0, delay * (1.0 + self._rng.uniform(-jitter, jitter)))

    def _next_delay(self, attempt: _Attempt) -> float:
        base = self._config.backoff_initial_seconds * (
            self._config.backoff_multiplier ** max(0, attempt.number - 1)
        )
        return self._jittered(min(base, self._config.backoff_max_seconds))

    def _switch_to_fallback(self) -> bool:
        """Move to the mirror host once; returns False if already there."""
        if self._using_fallback or self._config.fallback_base_url == self._base_url:
            return False
        _log.warning(
            "rest.failover",
            from_host=self._base_url,
            to_host=self._config.fallback_base_url,
        )
        self._base_url = self._config.fallback_base_url
        self._using_fallback = True
        return True

    async def _request(
        self,
        path: str,
        *,
        endpoint_key: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path not in PUBLIC_PATHS:
            raise ForbiddenEndpointError(
                f"{path!r} is not in the public market-data allowlist. This package is "
                f"alert-only and must never call trading endpoints."
            )

        weight = self._limiter.weight_for(endpoint_key)
        attempt = _Attempt()

        while True:
            attempt.number += 1
            await self._limiter.acquire(weight)
            url = f"{self._base_url}{path}"
            try:
                response = await self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if _is_policy_denial(exc):
                    _log.error(
                        "rest.egress_blocked",
                        url=url,
                        error=str(exc),
                        note="proxy refused CONNECT on policy grounds; not retrying",
                    )
                    raise EgressBlockedError(
                        f"Outbound access to {self._base_url} is blocked by network policy "
                        f"({exc}). This is not a transient failure and will not be retried. "
                        f"Allow the host, or set data.mode to 'synthetic'/'replay' to work offline."
                    ) from exc
                if attempt.number > self._config.max_retries:
                    if self._switch_to_fallback():
                        attempt = _Attempt()
                        continue
                    raise
                delay = self._next_delay(attempt)
                _log.warning(
                    "rest.transport_error",
                    url=url,
                    error=str(exc),
                    attempt=attempt.number,
                    sleep_seconds=round(delay, 3),
                )
                await asyncio.sleep(delay)
                continue

            self._limiter.observe_used_weight(_header_int(response, USED_WEIGHT_HEADER))

            if response.status_code == HTTP_IP_BANNED:
                delay = self._limiter.penalize_418()
                raise RateLimitExceededError(
                    f"Binance returned 418 (IP banned) for {url}; backing off {delay}s. "
                    f"Do not retry in a tight loop."
                )

            if response.status_code == HTTP_TOO_MANY_REQUESTS:
                retry_after = _header_float(response, RETRY_AFTER_HEADER)
                delay = self._limiter.penalize_429(retry_after)
                if attempt.number > self._config.max_retries:
                    raise BinanceRestError(response.status_code, _safe_json(response))
                _log.warning("rest.rate_limited", url=url, sleep_seconds=delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= HTTP_SERVER_ERROR_FLOOR:
                if attempt.number > self._config.max_retries:
                    if self._switch_to_fallback():
                        attempt = _Attempt()
                        continue
                    raise BinanceRestError(response.status_code, _safe_json(response))
                delay = self._next_delay(attempt)
                _log.warning(
                    "rest.server_error",
                    url=url,
                    status=response.status_code,
                    attempt=attempt.number,
                    sleep_seconds=round(delay, 3),
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                # 4xx other than 429 means a bad request; retrying cannot help.
                raise BinanceRestError(response.status_code, _safe_json(response))

            return response.json()

    # ---- public market data -------------------------------------------------

    async def ping(self) -> bool:
        await self._request("/api/v3/ping", endpoint_key="klines")
        return True

    async def server_time_ms(self) -> int:
        payload = await self._request("/api/v3/time", endpoint_key="klines")
        return int(payload["serverTime"])

    async def exchange_info(self, symbols: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if symbols:
            # Binance wants a JSON array literal for the multi-symbol form.
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        payload: dict[str, Any] = await self._request(
            "/api/v3/exchangeInfo", endpoint_key="exchange_info", params=params
        )
        return payload

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """One page of klines as a canonical candle frame.

        ``start_time``/``end_time`` are epoch milliseconds and are inclusive on
        both ends in Binance's implementation.
        """
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        rows = await self._request("/api/v3/klines", endpoint_key="klines", params=params)
        return candles_from_rest(rows)

    async def depth(self, symbol: str, limit: int) -> dict[str, Any]:
        payload: dict[str, Any] = await self._request(
            "/api/v3/depth", endpoint_key="depth_100", params={"symbol": symbol, "limit": limit}
        )
        return payload

    async def ticker_24hr(self, symbol: str) -> dict[str, Any]:
        payload: dict[str, Any] = await self._request(
            "/api/v3/ticker/24hr", endpoint_key="ticker_24hr", params={"symbol": symbol}
        )
        return payload

    async def verify_rate_limits(self) -> int | None:
        """Cross-check the configured weight budget against the live exchange.

        Returns the exchange's advertised per-minute request weight, or None if
        it could not be read. A configured budget above the real one is the kind
        of thing that only surfaces as a 418 three hours into a backfill.
        """
        try:
            info = await self.exchange_info()
        except (BinanceRestError, httpx.HTTPError) as exc:
            _log.warning("rest.rate_limit_verify_failed", error=str(exc))
            return None
        for entry in info.get("rateLimits", []):
            if entry.get("rateLimitType") == "REQUEST_WEIGHT" and entry.get("interval") == "MINUTE":
                advertised = int(entry["limit"]) * int(entry.get("intervalNum", 1))
                if advertised < self._limiter.limit:
                    _log.error(
                        "rest.rate_limit_misconfigured",
                        configured=self._limiter.limit,
                        advertised=advertised,
                        action="lower data.rate_limit.weight_limit_per_minute",
                    )
                return advertised
        return None


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _header_float(response: httpx.Response, name: str) -> float | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
