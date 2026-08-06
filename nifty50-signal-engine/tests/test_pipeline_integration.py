"""End-to-end data pipeline: broker -> backfill -> store -> adjust -> integrity.

This is the test that would have caught every bug found while building Phase 1,
because it exercises the seams rather than the units: a corporate action
discovered after ingest, a restart mid-history, and a live-built candle
reconciled against the vendor's own bar.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.config import Config
from nifty50.corporate_actions import ActionType, CorporateAction, adjust_ohlcv
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.backfill import Backfiller
from nifty50.data.brokers import StreamCallbacks, StreamMode, build_adapter
from nifty50.data.brokers.replay import ReplayAdapter
from nifty50.data.integrity import check_bars, reconcile_candles
from nifty50.data.store import BarStore
from nifty50.data.synthetic import apply_unadjusted_split, generate_session_bars
from nifty50.domain import Exchange, Instrument, InstrumentKind, Tick, Timeframe
from nifty50.trading_calendar import TradingCalendar

START = dt.date(2019, 9, 2)
END = dt.date(2019, 9, 30)
SPLIT_EX_DATE = dt.date(2019, 9, 19)
SYMBOL = "HDFCBANK"


@pytest.fixture
def vendor(config: Config, calendar: TradingCalendar) -> ReplayAdapter:
    """A broker serving raw, *unadjusted* bars spanning a real split — as Kite would."""
    root = config.path(config.broker.replay.root)
    directory = root / "NSE" / SYMBOL
    directory.mkdir(parents=True, exist_ok=True)
    for timeframe in (Timeframe.M15, Timeframe.D1):
        clean = generate_session_bars(calendar, START, END, timeframe, start_price=1100.0, seed=42)
        raw = apply_unadjusted_split(clean, SPLIT_EX_DATE, ratio_new=2, ratio_old=1)
        raw.to_parquet(directory / f"{timeframe.value}.parquet")
    adapter = ReplayAdapter(config, root=root)
    adapter.authenticate()
    return adapter


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(SYMBOL, Exchange.NSE, InstrumentKind.EQUITY, broker_token=341249)


def test_full_ingest_cycle(
    config: Config,
    calendar: TradingCalendar,
    store: BarStore,
    vendor: ReplayAdapter,
    instrument: Instrument,
) -> None:
    backfiller = Backfiller(vendor, store, calendar, config)
    result = backfiller.backfill(instrument, Timeframe.D1, start=START, end=END)
    assert result.ok

    expected_sessions = len(calendar.trading_days(START, END))
    stored = store.read(Exchange.NSE, SYMBOL, Timeframe.D1)
    assert len(stored) == expected_sessions

    # 1. The store holds RAW bars, so the unadjusted split is still visible.
    #    That is the point: storage is evidence, not interpretation.
    worst_raw_move = np.min(np.diff(np.log(stored["close"].to_numpy())))
    assert worst_raw_move < -0.6

    # 2. Integrity flags it as a suspected missing corporate action, loudly.
    before = check_bars(
        stored,
        symbol=SYMBOL,
        timeframe=Timeframe.D1,
        calendar=calendar,
        start=START,
        end=END,
        suspect_abs_log_return=config.data.integrity.suspect_unadjusted_abs_log_return,
    )
    assert not before.is_clean
    assert any(f.check == "suspected_unadjusted_action" for f in before.errors)

    # 3. Recording the action clears it — with no rewrite of stored data.
    action = CorporateAction(
        symbol=SYMBOL,
        ex_date=SPLIT_EX_DATE,
        action_type=ActionType.SPLIT,
        ratio_new=2,
        ratio_old=1,
    )
    after = check_bars(
        stored,
        symbol=SYMBOL,
        timeframe=Timeframe.D1,
        calendar=calendar,
        start=START,
        end=END,
        suspect_abs_log_return=config.data.integrity.suspect_unadjusted_abs_log_return,
        known_action_dates=[SPLIT_EX_DATE],
    )
    assert after.is_clean, after.describe()

    # 4. Reading through the adjuster produces the series features will see.
    adjusted = adjust_ohlcv(stored, [action], symbol=SYMBOL).frame
    worst_adjusted_move = np.min(np.diff(np.log(adjusted["close"].to_numpy())))
    assert worst_adjusted_move > -0.2
    # The raw prints survive alongside, for circuit bands and cost maths.
    assert adjusted["raw_close"].iloc[0] == pytest.approx(stored["close"].iloc[0])


def test_restart_resumes_without_duplicating_or_losing_bars(
    config: Config,
    calendar: TradingCalendar,
    store: BarStore,
    vendor: ReplayAdapter,
    instrument: Instrument,
) -> None:
    backfiller = Backfiller(vendor, store, calendar, config)
    backfiller.backfill(instrument, Timeframe.M15, start=START, end=dt.date(2019, 9, 13))
    partial = store.coverage(Exchange.NSE, SYMBOL, Timeframe.M15).bar_count

    # A fresh Backfiller against the same store: the restart path.
    resumed = Backfiller(vendor, store, calendar, config)
    resumed.backfill(instrument, Timeframe.M15, start=START, end=END)

    final = store.read(Exchange.NSE, SYMBOL, Timeframe.M15)
    expected = len(calendar.expected_bar_starts(START, END, Timeframe.M15))
    assert len(final) == expected
    assert partial < expected
    assert not final.index.duplicated().any()
    assert final.index.is_monotonic_increasing


def test_live_candles_reconcile_against_the_vendor_rest_bars(
    config: Config,
    calendar: TradingCalendar,
    vendor: ReplayAdapter,
    instrument: Instrument,
) -> None:
    """The engine's own bars must agree with the broker's, or we are signalling
    off a series the exchange would not recognise."""
    # Derived from the calendar rather than hardcoded: picking a date by hand is
    # how you end up asserting against Moharram.
    day = calendar.trading_days(START, END)[5]
    received: list[Tick] = []
    vendor.open_stream(StreamCallbacks(on_ticks=received.extend))
    vendor.subscribe([instrument], StreamMode.FULL)

    # The replay adapter has no 1m series here, so drive the aggregator from the
    # 15m bars' closes: enough to prove the plumbing and the comparison.
    built: list = []
    aggregator = CandleAggregator(calendar, Timeframe.M15, on_candle=built.append)
    rest = vendor.historical_candles(
        instrument,
        Timeframe.M15,
        calendar.schedule(day).continuous.start,
        calendar.schedule(day).continuous.end,
    )
    for ts, row in rest.iterrows():
        aggregator.on_tick(
            Tick(
                instrument_key=instrument.key,
                ts=ts.to_pydatetime(),
                last_price=float(row["close"]),
                volume_traded_today=None,
            )
        )
    # Bars close as the next tick arrives; the day's last one needs the flush.
    aggregator.flush_all()
    assert len(built) == len(rest)

    # Closes must match exactly; the synthetic tick carried only the close, so
    # only that field is compared.
    live = pd.DataFrame(
        {
            "open": [c.open for c in built],
            "high": [c.high for c in built],
            "low": [c.low for c in built],
            "close": [c.close for c in built],
            "volume": [c.volume for c in built],
        },
        index=pd.DatetimeIndex([c.ts for c in built], name="ts"),
    )
    mismatches = reconcile_candles(
        live[["close"]],
        rest[["close"]],
        price_rel_tolerance=config.data.integrity.reconcile_price_rel_tolerance,
        volume_rel_tolerance=config.data.integrity.reconcile_volume_rel_tolerance,
    )
    assert mismatches == []


def test_the_configured_adapter_satisfies_the_whole_contract(config: Config) -> None:
    adapter = build_adapter(config)
    for method in (
        "authenticate",
        "token_status",
        "list_instruments",
        "resolve",
        "historical_candles",
        "historical_chunk_days",
        "quote",
        "ltp",
        "open_stream",
        "close_stream",
        "subscribe",
        "unsubscribe",
    ):
        assert callable(getattr(adapter, method)), method
    assert adapter.max_stream_instruments > 0
