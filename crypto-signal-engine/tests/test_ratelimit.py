"""Weight-aware rate limiter, driven by a fake clock so tests take microseconds."""

from __future__ import annotations

import pytest

from cse.config import Config
from cse.data.ratelimit import WeightLimiter


class FakeClock:
    """Monotonic clock whose only way to advance is an awaited sleep."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(config: Config, clock: FakeClock) -> WeightLimiter:
    return WeightLimiter(config.data.rate_limit, time_fn=clock.time, sleep_fn=clock.sleep)


async def test_requests_below_soft_limit_never_sleep(
    limiter: WeightLimiter, clock: FakeClock
) -> None:
    for _ in range(100):
        await limiter.acquire(2)

    assert clock.sleeps == []
    assert limiter.used == 200


async def test_weight_lookup_uses_config(limiter: WeightLimiter) -> None:
    assert limiter.weight_for("klines") == 2
    assert limiter.weight_for("exchange_info") == 20


def test_unknown_endpoint_weight_is_an_error(limiter: WeightLimiter) -> None:
    """Better a loud failure than silently metering a call at weight zero."""
    with pytest.raises(KeyError, match="endpoint_weights"):
        limiter.weight_for("place_order")


async def test_soft_band_paces_requests(limiter: WeightLimiter, clock: FakeClock) -> None:
    """Once usage is inside the soft band, later requests are spread out."""
    await limiter.acquire(int(limiter.soft_limit) + 1)
    # The request that *enters* the band is not itself delayed: throttling on
    # the projection would stall a large first request in an idle window.
    assert clock.sleeps == []

    await limiter.acquire(2)

    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] > 0.0


async def test_large_first_request_is_not_stalled(limiter: WeightLimiter, clock: FakeClock) -> None:
    """Regression: gating pacing on `used + weight` slept ~47s on an idle window."""
    await limiter.acquire(int(limiter.soft_limit) + 1)

    assert clock.sleeps == []


async def test_hard_limit_sleeps_to_window_end_then_resets(
    limiter: WeightLimiter, clock: FakeClock
) -> None:
    await limiter.acquire(int(limiter.hard_limit))
    clock.sleeps.clear()

    await limiter.acquire(100)

    assert len(clock.sleeps) >= 1
    assert max(clock.sleeps) == pytest.approx(60.0, abs=1.0)
    assert limiter.used == 100  # window rolled, only the new request is counted


async def test_window_rolls_after_a_minute(limiter: WeightLimiter, clock: FakeClock) -> None:
    await limiter.acquire(1000)
    assert limiter.used == 1000

    clock.now += 61.0
    await limiter.acquire(2)

    assert limiter.used == 2


async def test_server_header_revises_usage_upward(limiter: WeightLimiter) -> None:
    """Another process on the same IP spends weight we did not account for."""
    await limiter.acquire(10)

    limiter.observe_used_weight(4_000)

    assert limiter.used == 4_000


async def test_server_header_never_revises_downward(limiter: WeightLimiter) -> None:
    """A lower server figure usually means its window rolled before ours."""
    await limiter.acquire(1_000)

    limiter.observe_used_weight(5)

    assert limiter.used == 1_000


def test_observe_ignores_missing_header(limiter: WeightLimiter) -> None:
    limiter.observe_used_weight(None)

    assert limiter.used == 0


async def test_429_blocks_until_retry_after_elapses(
    limiter: WeightLimiter, clock: FakeClock
) -> None:
    delay = limiter.penalize_429(30.0)
    assert delay == 30.0

    await limiter.acquire(2)

    assert pytest.approx(30.0) == clock.sleeps[0]


async def test_429_without_header_uses_configured_default(
    limiter: WeightLimiter, config: Config
) -> None:
    delay = limiter.penalize_429(None)

    assert delay == config.data.rate_limit.retry_after_default_seconds


async def test_418_applies_the_long_backoff(
    limiter: WeightLimiter, clock: FakeClock, config: Config
) -> None:
    delay = limiter.penalize_418()
    assert delay == config.data.rate_limit.ip_ban_backoff_seconds

    await limiter.acquire(2)

    assert clock.sleeps[0] == pytest.approx(config.data.rate_limit.ip_ban_backoff_seconds)


async def test_concurrent_acquires_cannot_overshoot(
    limiter: WeightLimiter, clock: FakeClock
) -> None:
    """Serialised reservation is what stops a burst from collectively overshooting."""
    import asyncio

    await asyncio.gather(*(limiter.acquire(2) for _ in range(50)))

    assert limiter.used == 100
