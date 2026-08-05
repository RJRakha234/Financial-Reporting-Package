"""REST client: endpoint allowlist, retries, rate-limit responses, failover.

All HTTP is mocked with respx — the suite never touches the network.
"""

from __future__ import annotations

import random

import httpx
import pytest
import respx

from cse.config import Config
from cse.data.ratelimit import RateLimitExceededError, WeightLimiter
from cse.data.rest import (
    BinanceRestClient,
    BinanceRestError,
    EgressBlockedError,
    ForbiddenEndpointError,
)
from tests.conftest import make_candles, to_rest_rows

BASE = "https://api.binance.com"
KLINES = f"{BASE}/api/v3/klines"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fast_config(config: Config) -> Config:
    """Shrink backoff so retry paths execute instantly."""
    rest = config.data.rest.model_copy(
        update={
            "backoff_initial_seconds": 0.001,
            "backoff_max_seconds": 0.002,
            "backoff_jitter": 0.0,
            "max_retries": 3,
        }
    )
    return config.model_copy(update={"data": config.data.model_copy(update={"rest": rest})})


@pytest.fixture
def client(fast_config: Config) -> BinanceRestClient:
    clock = FakeClock()
    limiter = WeightLimiter(fast_config.data.rate_limit, time_fn=clock.time, sleep_fn=clock.sleep)
    return BinanceRestClient(
        fast_config.data.rest,
        fast_config.data.rate_limit,
        limiter=limiter,
        rng=random.Random(0),
    )


@respx.mock
async def test_klines_returns_canonical_frame(client: BinanceRestClient) -> None:
    expected = make_candles(5)
    respx.get(KLINES).mock(return_value=httpx.Response(200, json=to_rest_rows(expected)))

    frame = await client.klines("BTCUSDT", "15m", limit=5)
    await client.aclose()

    assert list(frame.columns) == list(expected.columns)
    assert frame["open_time"].dtype == "int64"
    assert frame["close"].dtype == "float64"
    assert frame["close"].tolist() == pytest.approx(expected["close"].tolist())


@respx.mock
async def test_klines_passes_time_bounds(client: BinanceRestClient) -> None:
    route = respx.get(KLINES).mock(return_value=httpx.Response(200, json=[]))

    await client.klines("BTCUSDT", "15m", start_time=111, end_time=222, limit=500)
    await client.aclose()

    request = route.calls[0].request
    assert request.url.params["startTime"] == "111"
    assert request.url.params["endTime"] == "222"
    assert request.url.params["limit"] == "500"
    assert request.url.params["symbol"] == "BTCUSDT"


@respx.mock
async def test_empty_response_yields_empty_frame(client: BinanceRestClient) -> None:
    respx.get(KLINES).mock(return_value=httpx.Response(200, json=[]))

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert frame.empty
    assert list(frame.columns) == list(make_candles(0).columns)


async def test_trading_endpoints_are_structurally_unreachable(
    client: BinanceRestClient,
) -> None:
    """The alert-only guarantee is enforced by an allowlist, not by convention."""
    with pytest.raises(ForbiddenEndpointError, match="alert-only"):
        await client._request("/api/v3/order", endpoint_key="klines")

    await client.aclose()


@respx.mock
async def test_used_weight_header_is_observed(client: BinanceRestClient) -> None:
    respx.get(KLINES).mock(
        return_value=httpx.Response(200, json=[], headers={"X-MBX-USED-WEIGHT-1M": "1234"})
    )

    await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert client.limiter.used == 1234


@respx.mock
async def test_429_is_retried_after_the_requested_delay(client: BinanceRestClient) -> None:
    expected = make_candles(2)
    respx.get(KLINES).mock(
        side_effect=[
            httpx.Response(429, json={"code": -1003}, headers={"Retry-After": "1"}),
            httpx.Response(200, json=to_rest_rows(expected)),
        ]
    )

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert len(frame) == 2


@respx.mock
async def test_418_raises_instead_of_hammering(client: BinanceRestClient) -> None:
    """An IP ban must stop the run, not trigger a retry storm that extends it."""
    respx.get(KLINES).mock(return_value=httpx.Response(418, json={"code": -1003}))

    with pytest.raises(RateLimitExceededError, match="418"):
        await client.klines("BTCUSDT", "15m")
    await client.aclose()


@respx.mock
async def test_server_errors_are_retried_then_succeed(client: BinanceRestClient) -> None:
    expected = make_candles(3)
    respx.get(KLINES).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json=to_rest_rows(expected)),
        ]
    )

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert len(frame) == 3


@respx.mock
async def test_client_errors_are_not_retried(client: BinanceRestClient) -> None:
    """A 400 means a malformed request; retrying just wastes weight."""
    route = respx.get(KLINES).mock(
        return_value=httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
    )

    with pytest.raises(BinanceRestError, match="400"):
        await client.klines("NOTREAL", "15m")
    await client.aclose()

    assert route.call_count == 1


@respx.mock
async def test_persistent_failure_fails_over_to_the_mirror(
    client: BinanceRestClient, fast_config: Config
) -> None:
    """data-api.binance.vision serves the same public data when the primary is out."""
    expected = make_candles(4)
    respx.get(KLINES).mock(return_value=httpx.Response(500, text="down"))
    mirror = respx.get(f"{fast_config.data.rest.fallback_base_url}/api/v3/klines").mock(
        return_value=httpx.Response(200, json=to_rest_rows(expected))
    )

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert mirror.called
    assert len(frame) == 4
    assert client.base_url == fast_config.data.rest.fallback_base_url


@respx.mock
async def test_proxy_policy_denial_fails_fast(client: BinanceRestClient) -> None:
    """A proxy 403 is a decision, not an outage: retrying it only wastes time.

    Regression: this used to be caught as a generic transport error and retried
    5 times against the primary, then 5 more against the mirror — 50 seconds to
    reach a conclusion that was available immediately.
    """
    route = respx.get(KLINES).mock(side_effect=httpx.ProxyError("403 Forbidden"))

    with pytest.raises(EgressBlockedError, match="blocked by network policy"):
        await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert route.call_count == 1, "a policy denial must not be retried"


@respx.mock
async def test_proxy_auth_required_also_fails_fast(client: BinanceRestClient) -> None:
    route = respx.get(KLINES).mock(
        side_effect=httpx.ProxyError("407 Proxy Authentication Required")
    )

    with pytest.raises(EgressBlockedError):
        await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert route.call_count == 1


@respx.mock
async def test_ordinary_proxy_failure_is_still_retried(client: BinanceRestClient) -> None:
    """Only policy denials short-circuit; a flaky proxy still gets its retries."""
    expected = make_candles(1)
    respx.get(KLINES).mock(
        side_effect=[
            httpx.ProxyError("connection reset"),
            httpx.Response(200, json=to_rest_rows(expected)),
        ]
    )

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert len(frame) == 1


@respx.mock
async def test_transport_errors_are_retried(client: BinanceRestClient) -> None:
    expected = make_candles(1)
    respx.get(KLINES).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=to_rest_rows(expected)),
        ]
    )

    frame = await client.klines("BTCUSDT", "15m")
    await client.aclose()

    assert len(frame) == 1


@respx.mock
async def test_verify_rate_limits_flags_a_budget_above_the_exchange_cap(
    client: BinanceRestClient,
) -> None:
    """Config claiming more weight than Binance grants ends as a 418 mid-backfill."""
    respx.get(f"{BASE}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "rateLimits": [
                    {
                        "rateLimitType": "REQUEST_WEIGHT",
                        "interval": "MINUTE",
                        "intervalNum": 1,
                        "limit": 1200,
                    }
                ]
            },
        )
    )

    advertised = await client.verify_rate_limits()
    await client.aclose()

    assert advertised == 1200
    assert advertised < client.limiter.limit  # the misconfiguration this catches
