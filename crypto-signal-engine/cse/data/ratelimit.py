"""Weight-aware adaptive rate limiting for the Binance REST API.

Binance meters ``/api/*`` by request *weight* over a rolling minute and reports
consumption in the ``X-MBX-USED-WEIGHT-1M`` response header. Exceeding the
budget earns a 429; ignoring the 429 earns a 418 and a temporary IP ban, which
during a three-year backfill means losing the whole run.

The limiter therefore does three things:

1. **Predicts** — reserves weight locally before a request is sent, so a burst
   of concurrent calls cannot collectively overshoot.
2. **Corrects** — treats the server header as authoritative after every
   response. The server's accounting includes requests this process did not
   make (shared IP, another instance), so it can only ever revise usage upward.
3. **Backs off** — honours ``Retry-After`` on 429 and applies a long penalty on
   418.

The clock and sleep function are injected so tests run in microseconds instead
of minutes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cse.config import RateLimitConfig
from cse.logging import get_logger

_log = get_logger(__name__)

WINDOW_MS = 60_000

TimeFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class RateLimitExceededError(RuntimeError):
    """Raised when the exchange signals an IP ban (HTTP 418)."""


@dataclass
class WeightLimiter:
    """Rolling-minute weight budget with predictive reservation.

    The window is keyed to wall-clock minute boundaries because that is how
    Binance's ``-1M`` counter resets. That makes the accounting slightly
    conservative right after a boundary, which is the correct direction to err.
    """

    config: RateLimitConfig
    time_fn: TimeFn = time.monotonic
    sleep_fn: SleepFn = asyncio.sleep
    _used: int = field(default=0, init=False)
    _window_start: float = field(default=0.0, init=False)
    _blocked_until: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._window_start = self.time_fn()

    @property
    def limit(self) -> int:
        return self.config.weight_limit_per_minute

    @property
    def soft_limit(self) -> float:
        return self.limit * self.config.soft_threshold_pct

    @property
    def hard_limit(self) -> float:
        return self.limit * self.config.hard_threshold_pct

    @property
    def used(self) -> int:
        return self._used

    def weight_for(self, endpoint: str) -> int:
        """Documented weight for an endpoint key from ``config.yaml``."""
        try:
            return self.config.endpoint_weights[endpoint]
        except KeyError as exc:
            raise KeyError(
                f"no weight configured for endpoint {endpoint!r}; "
                f"add it to data.rate_limit.endpoint_weights"
            ) from exc

    def _roll_window(self, now: float) -> None:
        if now - self._window_start >= WINDOW_MS / 1000.0:
            self._used = 0
            self._window_start = now

    def _seconds_to_window_end(self, now: float) -> float:
        elapsed = now - self._window_start
        return max(0.0, WINDOW_MS / 1000.0 - elapsed)

    async def acquire(self, weight: int) -> None:
        """Reserve ``weight`` units, sleeping as needed to stay inside the budget."""
        async with self._lock:
            while True:
                now = self.time_fn()

                if now < self._blocked_until:
                    delay = self._blocked_until - now
                    _log.warning("rate_limit.blocked", sleep_seconds=round(delay, 3))
                    await self.sleep_fn(delay)
                    continue

                self._roll_window(now)

                if self._used + weight > self.hard_limit:
                    delay = self._seconds_to_window_end(now)
                    _log.warning(
                        "rate_limit.window_exhausted",
                        used=self._used,
                        requested=weight,
                        hard_limit=self.hard_limit,
                        sleep_seconds=round(delay, 3),
                    )
                    await self.sleep_fn(delay)
                    # Force the reset even if the injected clock is coarse.
                    self._used = 0
                    self._window_start = self.time_fn()
                    continue

                if self._used > self.soft_limit:
                    # Pace only once usage is ALREADY inside the soft band. The
                    # test is on `_used`, not on `_used + weight`: gating on the
                    # projection would stall the very first request of an idle
                    # window whenever it happened to be large, which throttles
                    # hardest exactly when there is no pressure.
                    #
                    # Delay grows as the remaining allowance shrinks, so the
                    # tail of the window is spent gradually rather than sprinted.
                    remaining_weight = max(1.0, self.hard_limit - self._used)
                    window_remaining = self._seconds_to_window_end(now)
                    delay = min(window_remaining, window_remaining * (weight / remaining_weight))
                    if delay > 0:
                        _log.info(
                            "rate_limit.pacing",
                            used=self._used,
                            soft_limit=self.soft_limit,
                            sleep_seconds=round(delay, 3),
                        )
                        await self.sleep_fn(delay)

                self._used += weight
                return

    def observe_used_weight(self, server_used: int | None) -> None:
        """Adopt the server's usage figure when it exceeds our local estimate.

        Only ever revised upward inside a window: a lower server number usually
        means the server's window rolled slightly before ours, and trusting it
        would let us overshoot right before our own reset.
        """
        if server_used is None:
            return
        now = self.time_fn()
        self._roll_window(now)
        if server_used > self._used:
            _log.debug("rate_limit.server_correction", local=self._used, server=server_used)
            self._used = server_used

    def penalize_429(self, retry_after_seconds: float | None) -> float:
        """Apply the exchange's requested cool-off after a 429."""
        delay = (
            retry_after_seconds
            if retry_after_seconds is not None
            else self.config.retry_after_default_seconds
        )
        self._blocked_until = self.time_fn() + delay
        self._used = self.limit  # assume the window is spent
        _log.warning("rate_limit.429", retry_after_seconds=delay)
        return delay

    def penalize_418(self) -> float:
        """Apply the long cool-off after an IP ban response."""
        delay = self.config.ip_ban_backoff_seconds
        self._blocked_until = self.time_fn() + delay
        self._used = self.limit
        _log.error("rate_limit.418_ip_ban", backoff_seconds=delay)
        return delay
