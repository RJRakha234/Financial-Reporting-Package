"""Event-driven backtest engine.

Bar by bar, in order, with no vectorised shortcut that could see forward. The
loop is deliberately boring; the value is in what it refuses to do:

* it will not start unless the point-in-time universe is verified and complete
  (:meth:`PointInTimeUniverse.assert_backtest_ready`);
* it only lets a strategy trade names that were in the index **on that date**;
* it acts on a signal at the *next* bar's open, never the signal bar's close;
* it will not fill through a circuit limit;
* in intraday mode it squares off before the configured cut-off, because
  carrying a position overnight changes the entire cost and margin profile.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from nifty50.backtest.costs import CostModel, Side, TradeStyle
from nifty50.backtest.execution import (
    ExecutionSimulator,
    Fill,
    FillRejection,
    PriceBand,
    SettlementLedger,
    SlippageModel,
)
from nifty50.backtest.metrics import PerformanceReport, summarise
from nifty50.domain import Timeframe, ensure_ist
from nifty50.logging_setup import get_logger
from nifty50.trading_calendar.calendar import TradingCalendar
from nifty50.universe import PointInTimeUniverse

log = get_logger(__name__)


def _row_at(frame: pd.DataFrame, timestamp: dt.datetime) -> pd.Series:
    """One row of a frame, as a Series.

    ``DataFrame.loc[ts]`` is typed ``Series | DataFrame`` because a duplicated
    index would return a frame. The bar store guarantees unique timestamps, so
    narrowing once here keeps every call site honest instead of scattering
    casts — and turns a duplicate that slipped through into a loud failure.
    """
    row = frame.loc[timestamp]
    if isinstance(row, pd.DataFrame):
        raise ValueError(
            f"duplicated bar timestamp {timestamp}: the store should have prevented this"
        )
    return row


@dataclass(frozen=True, slots=True)
class BarSlice:
    """Everything visible to a strategy at one instant.

    Contains only closed bars and features computed from them. There is no
    handle on future data to misuse, by construction.
    """

    timestamp: dt.datetime
    bars: dict[str, pd.Series]
    features: dict[str, pd.Series]
    tradeable: frozenset[str]
    equity: float
    positions: dict[str, float]

    def feature(self, symbol: str, name: str, default: float = float("nan")) -> float:
        row = self.features.get(symbol)
        if row is None or name not in row.index:
            return default
        value = row[name]
        return float(value) if pd.notna(value) else default


@dataclass(frozen=True, slots=True)
class Intent:
    """A desired change in position, expressed as a target weight of equity."""

    symbol: str
    target_weight: float
    reason: str = ""


class Strategy(Protocol):
    """What the engine needs from a strategy."""

    name: str

    def on_bar(self, view: BarSlice) -> Sequence[Intent]:
        """Return desired target weights given only what is visible now."""
        ...


@dataclass(slots=True)
class Trade:
    symbol: str
    entry_time: dt.datetime
    entry_price: float
    quantity: float
    exit_time: dt.datetime | None = None
    exit_price: float | None = None
    gross_pnl: float = 0.0
    costs: float = 0.0
    exit_reason: str = ""

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    @property
    def closed(self) -> bool:
        return self.exit_time is not None


@dataclass(slots=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade]
    rejections: dict[FillRejection, int]
    report: PerformanceReport
    benchmark: PerformanceReport | None = None
    notes: list[str] = field(default_factory=list)

    def beats_benchmark(self) -> bool | None:
        """Whether the strategy beat buy-and-hold, net of everything.

        Returns ``None`` when no benchmark was supplied rather than defaulting
        to ``True``. Most systematic retail strategies do not beat the index
        after the Indian cost stack, and the honest answer to "did it" is never
        an unstated assumption.
        """
        if self.benchmark is None:
            return None
        return self.report.net_pnl > self.benchmark.net_pnl


class BacktestEngine:
    """Runs one strategy over one point-in-time universe."""

    def __init__(
        self,
        *,
        calendar: TradingCalendar,
        universe: PointInTimeUniverse,
        cost_model: CostModel,
        slippage: SlippageModel,
        starting_equity: float,
        timeframe: Timeframe,
        square_off_time: dt.time | None = None,
        price_band_pct: float = 0.20,
        settlement_days: int = 1,
    ) -> None:
        self._calendar = calendar
        self._universe = universe
        self._costs = cost_model
        self._execution = ExecutionSimulator(slippage)
        self._starting_equity = starting_equity
        self._timeframe = timeframe
        self._square_off_time = square_off_time
        self._price_band_pct = price_band_pct
        self._ledger = SettlementLedger(settlement_days)

    def run(
        self,
        strategy: Strategy,
        bars: dict[str, pd.DataFrame],
        features: dict[str, pd.DataFrame],
        *,
        start: dt.date,
        end: dt.date,
        risk_free_rate: float = 0.0,
    ) -> BacktestResult:
        """Execute the strategy bar by bar over ``[start, end]``."""
        # Fail closed before a single bar is read. See Phase 2.
        self._universe.assert_backtest_ready(self._calendar, start, end)

        timeline = self._timeline(bars, start, end)
        if not timeline:
            raise ValueError("no bars in the requested window")

        cash = self._starting_equity
        positions: dict[str, float] = {}
        open_trades: dict[str, Trade] = {}
        closed_trades: list[Trade] = []
        rejections: dict[FillRejection, int] = {}
        equity_points: list[tuple[dt.datetime, float]] = []
        pending: list[Intent] = []
        exposed_bars = 0
        turnover = 0.0
        total_costs = 0.0
        previous_closes: dict[str, float] = {}

        for timestamp in timeline:
            session_day = timestamp.date()
            cash += self._ledger.release(session_day)

            row: dict[str, pd.Series] = {
                symbol: _row_at(frame, timestamp)
                for symbol, frame in bars.items()
                if timestamp in frame.index
            }
            if not row:
                continue

            # 1. Act on the PREVIOUS bar's intents, at this bar's open. A signal
            #    formed from a closed bar cannot be executed inside that bar.
            for intent in pending:
                fill, cash, total_costs, turnover = self._apply(
                    intent,
                    timestamp,
                    row,
                    positions,
                    open_trades,
                    closed_trades,
                    cash,
                    total_costs,
                    turnover,
                    previous_closes,
                    equity=self._equity(cash, positions, row),
                )
                if fill is not None and fill.rejected is not None:
                    rejections[fill.rejected] = rejections.get(fill.rejected, 0) + 1
            pending = []

            # 2. Forced square-off before the intraday cut-off.
            if self._should_square_off(timestamp):
                for symbol in list(positions):
                    cash, total_costs, turnover = self._close_position(
                        symbol,
                        timestamp,
                        row,
                        positions,
                        open_trades,
                        closed_trades,
                        cash,
                        total_costs,
                        turnover,
                        reason="intraday_square_off",
                    )

            equity = self._equity(cash, positions, row)
            equity_points.append((timestamp, equity))
            if positions:
                exposed_bars += 1

            # 3. Ask the strategy for next-bar intents, restricted to names that
            #    were actually in the index on this date.
            tradeable = self._universe.members_on(session_day) & frozenset(row)
            if not self._should_square_off(timestamp):
                view = BarSlice(
                    timestamp=timestamp,
                    bars=row,
                    features={
                        symbol: _row_at(frame, timestamp)
                        for symbol, frame in features.items()
                        if timestamp in frame.index
                    },
                    tradeable=frozenset(tradeable),
                    equity=equity,
                    positions=dict(positions),
                )
                pending = [intent for intent in strategy.on_bar(view) if intent.symbol in tradeable]

            for symbol, bar in row.items():
                previous_closes[symbol] = float(bar["close"])

        equity_curve = pd.Series(
            [value for _, value in equity_points],
            index=pd.DatetimeIndex([ts for ts, _ in equity_points], name="ts"),
        )
        gross = sum(t.gross_pnl for t in closed_trades)
        report = summarise(
            equity_curve,
            [t.net_pnl for t in closed_trades],
            periods_per_year=self._periods_per_year(),
            gross_pnl=gross,
            total_costs=total_costs,
            exposure_time=exposed_bars / len(timeline) if timeline else 0.0,
            turnover=turnover,
            risk_free_rate=risk_free_rate,
        )
        return BacktestResult(
            equity_curve=equity_curve,
            trades=closed_trades,
            rejections=rejections,
            report=report,
        )

    # ---------------------------------------------------------- internals

    def _timeline(
        self, bars: dict[str, pd.DataFrame], start: dt.date, end: dt.date
    ) -> list[dt.datetime]:
        stamps: set[pd.Timestamp] = set()
        for frame in bars.values():
            stamps.update(frame.index)
        return sorted(ts for ts in stamps if start <= ts.date() <= end)

    def _should_square_off(self, timestamp: dt.datetime) -> bool:
        if self._costs.style is not TradeStyle.INTRADAY or self._square_off_time is None:
            return False
        # Convert to IST before reading the wall clock: the cut-off is an IST
        # time, and comparing a differently-zoned timestamp's raw clock would
        # square off at the wrong moment.
        return ensure_ist(timestamp).time() >= self._square_off_time

    def _equity(self, cash: float, positions: dict[str, float], row: dict[str, pd.Series]) -> float:
        holdings = sum(
            quantity * float(row[symbol]["close"])
            for symbol, quantity in positions.items()
            if symbol in row
        )
        return cash + holdings + self._ledger.unsettled

    def _band(self, symbol: str, previous_closes: dict[str, float]) -> PriceBand | None:
        previous = previous_closes.get(symbol)
        if previous is None:
            return None
        return PriceBand.from_previous_close(previous, self._price_band_pct)

    def _apply(
        self,
        intent: Intent,
        timestamp: dt.datetime,
        row: dict[str, pd.Series],
        positions: dict[str, float],
        open_trades: dict[str, Trade],
        closed_trades: list[Trade],
        cash: float,
        total_costs: float,
        turnover: float,
        previous_closes: dict[str, float],
        *,
        equity: float,
    ) -> tuple[Fill | None, float, float, float]:
        symbol = intent.symbol
        if symbol not in row:
            return None, cash, total_costs, turnover
        bar = row[symbol]
        # Execution reference is the OPEN of this bar: the first price available
        # after the decision was made.
        reference = float(bar["open"])
        held = positions.get(symbol, 0.0)
        target_value = equity * intent.target_weight
        target_quantity = target_value / reference if reference > 0 else 0.0
        delta = target_quantity - held

        if abs(delta) < 1e-9:
            return None, cash, total_costs, turnover
        side = Side.BUY if delta > 0 else Side.SELL

        fill = self._execution.execute(
            timestamp=timestamp,
            symbol=symbol,
            side=side,
            desired_quantity=abs(delta),
            reference_price=reference,
            bar_volume=float(bar["volume"]) if "volume" in bar.index else None,
            band=self._band(symbol, previous_closes),
        )
        if not fill.filled:
            return fill, cash, total_costs, turnover

        signed = fill.quantity if side is Side.BUY else -fill.quantity
        new_quantity = held + signed
        turnover += fill.turnover

        leg_costs = self._costs.leg(fill.turnover, side)
        cost_total = leg_costs.total + fill.slippage + fill.impact_cost
        total_costs += cost_total

        if side is Side.BUY:
            cash -= fill.turnover + cost_total
            trade = open_trades.get(symbol)
            if trade is None:
                open_trades[symbol] = Trade(
                    symbol=symbol,
                    entry_time=timestamp,
                    entry_price=fill.price,
                    quantity=fill.quantity,
                    costs=cost_total,
                )
            else:
                trade.costs += cost_total
        else:
            proceeds = fill.turnover - cost_total
            if self._costs.style is TradeStyle.DELIVERY:
                available = self._calendar.next_trading_day(timestamp.date())
                self._ledger.record_sale(proceeds, available)
            else:
                cash += proceeds
            trade = open_trades.get(symbol)
            if trade is not None:
                trade.costs += cost_total
                trade.gross_pnl += (fill.price - trade.entry_price) * fill.quantity
                if abs(new_quantity) < 1e-9:
                    trade.exit_time = timestamp
                    trade.exit_price = fill.price
                    trade.exit_reason = intent.reason or "target_weight"
                    closed_trades.append(trade)
                    open_trades.pop(symbol, None)

        if abs(new_quantity) < 1e-9:
            positions.pop(symbol, None)
        else:
            positions[symbol] = new_quantity
        return fill, cash, total_costs, turnover

    def _close_position(
        self,
        symbol: str,
        timestamp: dt.datetime,
        row: dict[str, pd.Series],
        positions: dict[str, float],
        open_trades: dict[str, Trade],
        closed_trades: list[Trade],
        cash: float,
        total_costs: float,
        turnover: float,
        *,
        reason: str,
    ) -> tuple[float, float, float]:
        intent = Intent(symbol=symbol, target_weight=0.0, reason=reason)
        _, cash, total_costs, turnover = self._apply(
            intent,
            timestamp,
            row,
            positions,
            open_trades,
            closed_trades,
            cash,
            total_costs,
            turnover,
            previous_closes={},
            equity=self._equity(cash, positions, row),
        )
        return cash, total_costs, turnover

    def _periods_per_year(self) -> float:
        sessions = 250.0
        if self._timeframe is Timeframe.D1:
            return sessions
        reference = self._calendar.next_trading_day(dt.date(2025, 1, 1))
        return sessions * self._calendar.bars_per_session(reference, self._timeframe)


def buy_and_hold_benchmark(
    index_bars: pd.DataFrame,
    *,
    starting_equity: float,
    periods_per_year: float,
    expense_ratio: float = 0.0,
) -> PerformanceReport:
    """Buy-and-hold the index, after an index fund's expense ratio.

    The bar every strategy must clear. A systematic strategy that does not beat
    a plain index fund, net of the full cost stack, is not worth running — and
    most do not.
    """
    closes = index_bars["close"].dropna()
    if closes.empty:
        return PerformanceReport()
    units = starting_equity / float(closes.iloc[0])
    equity = closes * units
    if expense_ratio:
        periods = pd.Series(range(len(equity)), index=equity.index, dtype="float64")
        equity = equity * (1.0 - expense_ratio) ** (periods / periods_per_year)
    gross = float(equity.iloc[-1] - equity.iloc[0])
    report = summarise(
        equity,
        [gross],
        periods_per_year=periods_per_year,
        gross_pnl=gross,
        total_costs=0.0,
        exposure_time=1.0,
        turnover=starting_equity,
    )
    report.notes.append("buy-and-hold benchmark: one entry, no turnover, expense ratio applied")
    return report
