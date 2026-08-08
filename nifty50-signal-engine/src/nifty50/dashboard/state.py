"""Live state the dashboard reads, written by the streaming thread.

Two threads touch this: the stream supervisor pushes candles in as they close,
and the HTTP handler reads snapshots out. Everything below is guarded by one
lock, and every read returns a *copy* — handing the HTTP thread a reference to
a list the stream thread is still appending to would produce charts that flicker
between states mid-serialisation.

Three decisions worth stating.

**Unclosed bars are marked, not hidden and not silently drawn as closed.** A
15-minute bar stamped 10:15 is not finished until 10:30. Plotting it as a
completed candle makes the last bar on every chart a lie that redraws itself —
and it is exactly the lie that makes a live chart disagree with a backtest.
:attr:`CandleSeries.forming` carries it separately.

**Memory is bounded.** A trading day is 25 bars at 15m but 375 at 1m, and a
process left running for a week across fifty symbols will exhaust a laptop if
each series grows without limit. Series are ring buffers.

**Estimated volume is propagated.** The aggregator flags bars whose volume was
inferred without a known cumulative baseline — typically the first bars after a
mid-session reconnect. The dashboard shows that rather than quietly presenting
a guess as a measurement.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from nifty50.data.stream import StreamHealth
from nifty50.domain import Candle, Timeframe, now_ist

# Bars retained per (symbol, timeframe). 1500 is six sessions of 1-minute data
# or sixty of 15-minute — enough context to chart, bounded enough that fifty
# symbols across three timeframes stays comfortably inside a few hundred MB.
_MAX_BARS: int = 1500


@dataclass(slots=True)
class CandleSeries:
    """Closed bars plus, separately, the one still forming."""

    symbol: str
    timeframe: Timeframe
    closed: deque[Candle] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    forming: Candle | None = None

    def accept(self, candle: Candle) -> None:
        if candle.closed:
            # A re-emitted bar replaces rather than duplicates: the aggregator
            # can flush the same interval twice around a reconnect.
            if self.closed and self.closed[-1].ts == candle.ts:
                self.closed[-1] = candle
            else:
                self.closed.append(candle)
            if self.forming is not None and self.forming.ts <= candle.ts:
                self.forming = None
        else:
            self.forming = candle

    def to_json(self, *, limit: int | None = None) -> dict[str, Any]:
        bars = list(self.closed)
        if limit is not None:
            bars = bars[-limit:]
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "closed": [_candle_json(bar) for bar in bars],
            "forming": _candle_json(self.forming) if self.forming else None,
        }


def _candle_json(candle: Candle) -> dict[str, Any]:
    return {
        # Seconds since epoch: what Lightweight Charts expects, and it keeps
        # the timezone question on this side rather than in the browser.
        "time": int(candle.ts.timestamp()),
        "iso": candle.ts.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "partial": candle.partial,
        "volume_estimated": getattr(candle, "volume_estimated", False),
    }


@dataclass(frozen=True, slots=True)
class RiskContext:
    """What the operator needs to see next to a price, every time.

    Deliberately not a signal. The session that produced this project found no
    tradeable edge across five signal families, and a panel showing BUY arrows
    it cannot justify would undo that. What it *can* honestly show is the
    arithmetic: what a round trip costs, what the stop implies for size, and
    whether the current move even clears the hurdle.
    """

    capital_inr: float
    risk_fraction: float
    breakeven_pct: float
    atr_pct: float | None = None

    @property
    def risk_inr(self) -> float:
        return self.capital_inr * self.risk_fraction

    def position(self, price: float, stop_distance: float) -> dict[str, Any]:
        """Shares, notional, and the leverage that a tight stop quietly implies.

        The leverage line is the point. A stop of two 15-minute ATRs on a
        1% risk budget came out at 165% of capital in this project's own
        worked example — the classic "tight stops mean low risk" error, which
        is backwards: a tighter stop buys more shares for the same rupee risk.
        """
        if price <= 0 or stop_distance <= 0:
            return {"quantity": 0, "notional": 0.0, "exposure_pct": 0.0}
        quantity = int(self.risk_inr / stop_distance)
        notional = quantity * price
        return {
            "quantity": quantity,
            "notional": notional,
            "exposure_pct": notional / self.capital_inr if self.capital_inr else 0.0,
            "stop_distance": stop_distance,
            "risk_inr": self.risk_inr,
        }


@dataclass(slots=True)
class DashboardState:
    """Everything the browser polls for. Thread-safe."""

    risk: RiskContext
    health: StreamHealth = field(default_factory=StreamHealth)
    _series: dict[tuple[str, Timeframe], CandleSeries] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _regime: dict[str, float | None] = field(default_factory=dict)
    _started_at: dt.datetime = field(default_factory=now_ist)

    # -------------------------------------------------------------- writes

    def on_candle(self, candle: Candle) -> None:
        """CandleSink-compatible. Called from the streaming thread."""
        key = (candle.instrument_key, candle.timeframe)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = CandleSeries(symbol=candle.instrument_key, timeframe=candle.timeframe)
                self._series[key] = series
            series.accept(candle)

    def set_health(self, health: StreamHealth) -> None:
        with self._lock:
            self.health = health

    def set_regime(self, **values: float | None) -> None:
        with self._lock:
            self._regime.update(values)

    # --------------------------------------------------------------- reads

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted({symbol for symbol, _ in self._series})

    def timeframes(self, symbol: str) -> list[str]:
        with self._lock:
            return sorted(
                timeframe.value for sym, timeframe in self._series if sym == symbol
            )

    def series_json(
        self, symbol: str, timeframe: Timeframe, *, limit: int | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            series = self._series.get((symbol, timeframe))
            return series.to_json(limit=limit) if series else None

    def health_json(self) -> dict[str, Any]:
        with self._lock:
            health = self.health
            now = now_ist()
            token_left = health.token_seconds_remaining(now)
            return {
                "state": health.state.value,
                "session_phase": health.session_phase.value,
                "now": now.isoformat(),
                "uptime_seconds": (now - self._started_at).total_seconds(),
                "ticks_received": health.ticks_received,
                "candles_emitted": health.candles_emitted,
                "subscribed_instruments": health.subscribed_instruments,
                "reconnect_count": health.reconnect_count,
                "error_count": health.error_count,
                "last_error": health.last_error,
                "data_age_seconds": health.data_age_seconds(now),
                "last_tick_lag_ms": health.last_tick_lag_ms,
                "token_seconds_remaining": token_left,
                # Surfaced explicitly because there is no refresh token: a Kite
                # session dies around 06:00 IST and someone has to walk the
                # OAuth redirect by hand. A dashboard that discovers this at
                # 09:15 has already cost you the open.
                "token_expires_soon": token_left is not None and token_left < 3600,
                "regime": dict(self._regime),
            }

    def risk_json(self, symbol: str, timeframe: Timeframe) -> dict[str, Any]:
        """Cost hurdle and position sizing against the latest bar."""
        with self._lock:
            series = self._series.get((symbol, timeframe))
            last = None
            if series is not None:
                last = series.forming or (series.closed[-1] if series.closed else None)
        payload: dict[str, Any] = {
            "capital_inr": self.risk.capital_inr,
            "risk_fraction": self.risk.risk_fraction,
            "risk_inr": self.risk.risk_inr,
            "breakeven_pct": self.risk.breakeven_pct,
            "atr_pct": self.risk.atr_pct,
        }
        if last is None:
            return payload
        payload["price"] = last.close
        bar_move = (last.high - last.low) / last.close if last.close else 0.0
        payload["bar_range_pct"] = bar_move
        # The single most useful line on the page: did this bar even move
        # enough to pay for the trade?
        payload["clears_costs"] = bar_move > self.risk.breakeven_pct
        if self.risk.atr_pct:
            stop = 2.0 * self.risk.atr_pct * last.close
            payload["sizing"] = self.risk.position(last.close, stop)
        return payload
