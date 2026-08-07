"""Baseline strategies.

These exist to exercise the engine and to give later phases something to beat.
They are deliberately simple: a baseline whose edge is obvious is a baseline
whose backtest result can be attributed to the engine rather than to cleverness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nifty50.backtest.engine import BarSlice, Intent


@dataclass(slots=True)
class EmaCrossoverStrategy:
    """Long while the fast EMA is above the slow one, flat otherwise.

    Long-only: short selling in the Indian cash segment is intraday-only and
    carries a different margin and cost profile, so a long/short baseline would
    need its own cost treatment before it meant anything.
    """

    fast_period: int = 9
    slow_period: int = 21
    max_positions: int = 5
    name: str = "ema_crossover"

    def on_bar(self, view: BarSlice) -> Sequence[Intent]:
        fast_column = f"ema_{self.fast_period}"
        slow_column = f"ema_{self.slow_period}"
        wanted: list[str] = []
        for symbol in sorted(view.tradeable):
            fast = view.feature(symbol, fast_column)
            slow = view.feature(symbol, slow_column)
            if fast != fast or slow != slow:  # NaN during warm-up
                continue
            if fast > slow:
                wanted.append(symbol)

        selected = wanted[: self.max_positions]
        weight = 1.0 / self.max_positions if selected else 0.0
        intents = [
            Intent(symbol=symbol, target_weight=weight, reason="ema_fast_above_slow")
            for symbol in selected
        ]
        # Exit anything held that no longer qualifies.
        intents.extend(
            Intent(symbol=symbol, target_weight=0.0, reason="ema_cross_down")
            for symbol in view.positions
            if symbol not in selected
        )
        return intents


@dataclass(slots=True)
class BuyAndHoldStrategy:
    """Buy an equal-weight basket on the first bar and never trade again.

    The control: any strategy whose net result is worse than this one is being
    beaten by doing nothing, which is the outcome the README warns about.
    """

    max_positions: int = 5
    name: str = "buy_and_hold"

    def on_bar(self, view: BarSlice) -> Sequence[Intent]:
        if view.positions:
            return []
        selected = sorted(view.tradeable)[: self.max_positions]
        if not selected:
            return []
        weight = 1.0 / len(selected)
        return [
            Intent(symbol=symbol, target_weight=weight, reason="initial_entry")
            for symbol in selected
        ]
