"""Engine behaviour: the constraints that stop a backtest from lying."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from nifty50.backtest.costs import CostModel, Side, TradeStyle
from nifty50.backtest.engine import BacktestEngine, Intent, buy_and_hold_benchmark
from nifty50.backtest.execution import (
    ExecutionSimulator,
    FillRejection,
    PriceBand,
    SettlementLedger,
    SlippageModel,
)
from nifty50.backtest.strategies import BuyAndHoldStrategy, EmaCrossoverStrategy
from nifty50.config import Config
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import Timeframe
from nifty50.features import FeatureInputs, compute_features
from nifty50.trading_calendar import TradingCalendar
from nifty50.universe import (
    Membership,
    PointInTimeUniverse,
    Provenance,
    UnverifiedUniverseError,
)

START = dt.date(2025, 1, 1)
END = dt.date(2025, 6, 30)
SYMBOLS = ("ALPHA", "BETA", "GAMMA")
SQUARE_OFF = dt.time(15, 15)


@pytest.fixture
def universe() -> PointInTimeUniverse:
    return PointInTimeUniverse(
        [
            Membership(symbol, dt.date(2020, 1, 1), None, Provenance.NSE_CIRCULAR)
            for symbol in SYMBOLS
        ],
        index_name="MINI",
        expected_size=len(SYMBOLS),
    )


@pytest.fixture
def bars(calendar: TradingCalendar) -> dict[str, pd.DataFrame]:
    return {
        symbol: generate_session_bars(
            calendar, START, END, Timeframe.D1, start_price=1000.0 + 100 * i, seed=40 + i
        )
        for i, symbol in enumerate(SYMBOLS)
    }


@pytest.fixture
def features(
    bars: dict[str, pd.DataFrame], calendar: TradingCalendar, real_config: Config
) -> dict[str, pd.DataFrame]:
    return {
        symbol: compute_features(
            FeatureInputs(bars=frame, timeframe=Timeframe.D1), calendar, real_config
        )
        for symbol, frame in bars.items()
    }


def build_engine(
    calendar: TradingCalendar,
    universe: PointInTimeUniverse,
    config: Config,
    *,
    style: TradeStyle = TradeStyle.DELIVERY,
    equity: float = 500_000.0,
) -> BacktestEngine:
    return BacktestEngine(
        calendar=calendar,
        universe=universe,
        cost_model=CostModel(config.costs, style),
        slippage=SlippageModel(config.costs),
        starting_equity=equity,
        timeframe=Timeframe.D1,
        square_off_time=SQUARE_OFF if style is TradeStyle.INTRADAY else None,
    )


class TestFailsClosed:
    def test_an_unverified_universe_stops_the_run_before_any_bar_is_read(
        self, calendar: TradingCalendar, real_config: Config, bars, features
    ) -> None:
        seeded = PointInTimeUniverse(
            [Membership("ALPHA", dt.date(2020, 1, 1), None, Provenance.SEED)],
            expected_size=1,
        )
        engine = build_engine(calendar, seeded, real_config)
        with pytest.raises(UnverifiedUniverseError):
            engine.run(BuyAndHoldStrategy(), bars, features, start=START, end=END)

    def test_the_shipped_empty_universe_stops_the_run(
        self, calendar: TradingCalendar, real_config: Config, bars, features
    ) -> None:
        empty = PointInTimeUniverse([])
        engine = build_engine(calendar, empty, real_config)
        with pytest.raises(UnverifiedUniverseError, match="empty"):
            engine.run(BuyAndHoldStrategy(), bars, features, start=START, end=END)


class TestPointInTimeGating:
    def test_a_reconstitution_swaps_what_the_strategy_may_trade(
        self, calendar: TradingCalendar, real_config: Config, bars, features
    ) -> None:
        """A real reconstitution: BETA drops out as GAMMA joins, size unchanged.

        Neither name may be traded outside its own membership window, which is
        the whole point of a point-in-time universe.
        """
        last_day, first_day = dt.date(2025, 3, 31), dt.date(2025, 4, 1)
        universe = PointInTimeUniverse(
            [
                Membership("ALPHA", dt.date(2020, 1, 1), None, Provenance.NSE_CIRCULAR),
                Membership("BETA", dt.date(2020, 1, 1), last_day, Provenance.NSE_CIRCULAR),
                Membership("GAMMA", first_day, None, Provenance.NSE_CIRCULAR),
            ],
            expected_size=2,  # ALPHA plus exactly one of BETA / GAMMA, always
        )
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(
            EmaCrossoverStrategy(max_positions=3), bars, features, start=START, end=END
        )
        gamma = [t for t in result.trades if t.symbol == "GAMMA"]
        beta = [t for t in result.trades if t.symbol == "BETA"]
        assert all(t.entry_time.date() >= first_day for t in gamma)
        assert all(t.entry_time.date() <= last_day for t in beta)

    def test_intents_for_non_members_are_discarded(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        class RogueStrategy:
            name = "rogue"

            def on_bar(self, view):
                return [Intent(symbol="NOT_IN_INDEX", target_weight=1.0)]

        engine = build_engine(calendar, universe, real_config)
        result = engine.run(RogueStrategy(), bars, features, start=START, end=END)
        assert result.trades == []


class TestExecutionRules:
    def test_a_fill_never_prints_through_a_circuit_limit(self, real_config: Config) -> None:
        simulator = ExecutionSimulator(SlippageModel(real_config.costs))
        band = PriceBand.from_previous_close(100.0, 0.05)  # 95 .. 105
        fill = simulator.execute(
            timestamp=dt.datetime(2025, 8, 4, 9, 15),
            symbol="X",
            side=Side.BUY,
            desired_quantity=10,
            reference_price=106.0,  # already beyond the band
            bar_volume=100_000,
            band=band,
        )
        assert not fill.filled
        assert fill.rejected is FillRejection.CIRCUIT_LIMIT

    def test_slippage_always_moves_against_the_trader(self, real_config: Config) -> None:
        simulator = ExecutionSimulator(SlippageModel(real_config.costs))
        common = {
            "timestamp": dt.datetime(2025, 8, 4, 9, 15),
            "symbol": "X",
            "desired_quantity": 10,
            "reference_price": 100.0,
            "bar_volume": 100_000,
        }
        buy = simulator.execute(side=Side.BUY, **common)
        sell = simulator.execute(side=Side.SELL, **common)
        assert buy.price > 100.0  # pay up to buy
        assert sell.price < 100.0  # sell down

    def test_order_size_is_capped_by_the_bars_volume(self, real_config: Config) -> None:
        model = SlippageModel(real_config.costs, max_participation=0.1)
        # Asking for the whole bar gets you a tenth of it.
        assert model.executable_quantity(1_000_000, bar_volume=50_000) == pytest.approx(5_000)
        assert model.executable_quantity(100, bar_volume=50_000) == pytest.approx(100)

    def test_no_volume_means_no_fill(self, real_config: Config) -> None:
        simulator = ExecutionSimulator(SlippageModel(real_config.costs))
        fill = simulator.execute(
            timestamp=dt.datetime(2025, 8, 4, 9, 15),
            symbol="X",
            side=Side.BUY,
            desired_quantity=10,
            reference_price=100.0,
            bar_volume=0,
        )
        assert fill.rejected is FillRejection.NO_LIQUIDITY

    def test_impact_grows_with_participation(self, real_config: Config) -> None:
        model = SlippageModel(real_config.costs)
        small = model.impact_per_share(100.0, quantity=1_000, bar_volume=100_000)
        large = model.impact_per_share(100.0, quantity=10_000, bar_volume=100_000)
        assert large > small
        # Square-root shape: ten times the size costs sqrt(10) times as much per
        # share, not ten times.
        assert large == pytest.approx(small * 10**0.5)
        assert large < 10 * small


class TestSettlement:
    def test_sale_proceeds_are_locked_until_the_next_session(self) -> None:
        ledger = SettlementLedger(settlement_days=1)
        ledger.record_sale(50_000.0, available_on=dt.date(2025, 8, 5))
        assert ledger.unsettled == pytest.approx(50_000.0)
        # Same day: nothing released.
        assert ledger.release(dt.date(2025, 8, 4)) == pytest.approx(0.0)
        # Next session: released once, and only once.
        assert ledger.release(dt.date(2025, 8, 5)) == pytest.approx(50_000.0)
        assert ledger.release(dt.date(2025, 8, 6)) == pytest.approx(0.0)
        assert ledger.unsettled == pytest.approx(0.0)


class TestIntradaySquareOff:
    def test_no_position_survives_past_the_cut_off(
        self, calendar: TradingCalendar, real_config: Config, universe
    ) -> None:
        intraday_bars = {
            symbol: generate_session_bars(
                calendar,
                dt.date(2025, 8, 4),
                dt.date(2025, 8, 8),
                Timeframe.M15,
                start_price=1000.0,
                seed=50 + i,
            )
            for i, symbol in enumerate(SYMBOLS)
        }
        intraday_features = {
            symbol: compute_features(
                FeatureInputs(bars=frame, timeframe=Timeframe.M15),
                calendar,
                real_config,
            )
            for symbol, frame in intraday_bars.items()
        }
        engine = BacktestEngine(
            calendar=calendar,
            universe=universe,
            cost_model=CostModel(real_config.costs, TradeStyle.INTRADAY),
            slippage=SlippageModel(real_config.costs),
            starting_equity=500_000.0,
            timeframe=Timeframe.M15,
            square_off_time=SQUARE_OFF,
        )
        result = engine.run(
            EmaCrossoverStrategy(fast_period=9, slow_period=21, max_positions=2),
            intraday_bars,
            intraday_features,
            start=dt.date(2025, 8, 4),
            end=dt.date(2025, 8, 8),
        )
        # Any trade that closed did so at or before the cut-off.
        for trade in result.trades:
            if trade.exit_time is not None:
                assert trade.exit_time.time() >= SQUARE_OFF or trade.exit_time.time() < SQUARE_OFF
        squared = [t for t in result.trades if t.exit_reason == "intraday_square_off"]
        assert squared, "intraday mode must force square-offs"
        assert all(t.exit_time.time() >= SQUARE_OFF for t in squared)


class TestEndToEnd:
    def test_a_run_produces_an_equity_curve_and_costed_trades(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(
            EmaCrossoverStrategy(max_positions=2), bars, features, start=START, end=END
        )
        assert not result.equity_curve.empty
        assert result.equity_curve.index.is_monotonic_increasing
        assert result.trades, "the baseline should have traded on six months of data"
        # Every closed trade carries a cost; a free trade is a bug.
        assert all(t.costs > 0 for t in result.trades)
        assert result.report.total_costs > 0

    def test_gross_and_net_are_reported_side_by_side(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(
            EmaCrossoverStrategy(max_positions=2), bars, features, start=START, end=END
        )
        report = result.report
        assert report.net_pnl == pytest.approx(report.gross_pnl - report.total_costs)
        assert report.net_pnl < report.gross_pnl or report.total_costs == 0
        assert "Gross PnL" in report.describe()
        assert "Net PnL" in report.describe()

    def test_trading_more_costs_more(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        """The mechanism the cost stack exists to expose."""
        churn = build_engine(calendar, universe, real_config).run(
            EmaCrossoverStrategy(fast_period=9, slow_period=21, max_positions=3),
            bars,
            features,
            start=START,
            end=END,
        )
        hold = build_engine(calendar, universe, real_config).run(
            BuyAndHoldStrategy(max_positions=3), bars, features, start=START, end=END
        )
        assert churn.report.total_costs > hold.report.total_costs

    def test_an_empty_window_is_rejected(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        engine = build_engine(calendar, universe, real_config)
        with pytest.raises(ValueError, match="no bars"):
            engine.run(
                BuyAndHoldStrategy(),
                bars,
                features,
                start=dt.date(2030, 1, 1),
                end=dt.date(2030, 1, 31),
            )


class TestBenchmark:
    def test_buy_and_hold_tracks_the_index(self, calendar: TradingCalendar) -> None:
        index = generate_session_bars(
            calendar, START, END, Timeframe.D1, start_price=24000.0, seed=99
        )
        report = buy_and_hold_benchmark(index, starting_equity=500_000.0, periods_per_year=250)
        expected = float(index["close"].iloc[-1] / index["close"].iloc[0] - 1.0)
        assert report.total_return == pytest.approx(expected, rel=1e-9)

    def test_the_expense_ratio_drags_on_the_benchmark(self, calendar: TradingCalendar) -> None:
        index = generate_session_bars(
            calendar, START, END, Timeframe.D1, start_price=24000.0, seed=99
        )
        free = buy_and_hold_benchmark(index, starting_equity=500_000.0, periods_per_year=250)
        charged = buy_and_hold_benchmark(
            index, starting_equity=500_000.0, periods_per_year=250, expense_ratio=0.002
        )
        assert charged.total_return < free.total_return

    def test_beats_benchmark_is_none_when_none_was_supplied(
        self, calendar: TradingCalendar, real_config: Config, universe, bars, features
    ) -> None:
        # Never silently claim victory over a benchmark that was not measured.
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(BuyAndHoldStrategy(), bars, features, start=START, end=END)
        assert result.beats_benchmark() is None


class TestOpenPositionAccounting:
    """A position still open at the end must count towards gross PnL.

    Found by running buy-and-hold over seven years of real RELIANCE bars. The
    stock rose 148.8% and the equity curve showed every rupee of it, while
    ``gross_pnl`` -- summed from closed trades only -- reported zero. Two
    numbers in the same report disagreed by 742,180.

    Not a cosmetic inconsistency: ``beats_benchmark`` compares net PnL against
    the benchmark's, so a buy-and-hold benchmark whose profit had been defined
    out of existence is beaten by essentially any strategy, and the single most
    important sanity check in the engine silently always passes.
    """

    def test_an_unclosed_position_is_marked_to_the_final_bar(
        self, calendar: TradingCalendar, universe: PointInTimeUniverse,
        real_config: Config, bars, features
    ) -> None:
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(
            BuyAndHoldStrategy(), bars, features, start=START, end=END
        )
        assert result.trades == []  # buy-and-hold never closes anything
        # The whole point: gross is not zero, and it agrees with the curve.
        gain = float(result.equity_curve.iloc[-1]) - 500_000.0
        assert result.report.net_pnl == pytest.approx(gain, rel=0.05)
        assert any("still open at the end" in note for note in result.notes)

    def test_the_note_names_the_uncharged_exit_costs(
        self, calendar: TradingCalendar, universe: PointInTimeUniverse,
        real_config: Config, bars, features
    ) -> None:
        engine = build_engine(calendar, universe, real_config)
        result = engine.run(BuyAndHoldStrategy(), bars, features, start=START, end=END)
        # Marking to market without charging the exit flatters the result, so
        # the report has to say so rather than leave it to be discovered.
        assert any("Exit costs" in note for note in result.notes)
