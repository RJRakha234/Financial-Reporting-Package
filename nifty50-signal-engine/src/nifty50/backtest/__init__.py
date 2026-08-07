"""Event-driven backtest engine with the full Indian cost stack."""

from nifty50.backtest.costs import CostBreakdown, CostModel, Side, TradeStyle
from nifty50.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    BarSlice,
    Intent,
    Strategy,
    Trade,
    buy_and_hold_benchmark,
)
from nifty50.backtest.execution import (
    ExecutionSimulator,
    Fill,
    FillRejection,
    PriceBand,
    SlippageModel,
)
from nifty50.backtest.metrics import PerformanceReport, summarise

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BarSlice",
    "CostBreakdown",
    "CostModel",
    "ExecutionSimulator",
    "Fill",
    "FillRejection",
    "Intent",
    "PerformanceReport",
    "PriceBand",
    "Side",
    "SlippageModel",
    "Strategy",
    "Trade",
    "TradeStyle",
    "buy_and_hold_benchmark",
    "summarise",
]
