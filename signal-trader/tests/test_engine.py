from signaltrader import PaperBroker, RiskConfig, TradingEngine


def _engine(**risk):
    broker = PaperBroker(starting_cash=10_000, prices={"BTCUSDT": 65000})
    return TradingEngine(broker, risk=RiskConfig(max_notional=5_000, **risk)), broker


def test_full_pipeline_places_paper_order():
    engine, broker = _engine()
    outcome = engine.handle_message("BUY BTCUSDT @ 65000\nSL 64000\nTP 66000")
    assert outcome.parsed is not None
    assert outcome.acted
    assert outcome.result.dry_run is True
    assert broker.positions["BTCUSDT"].quantity > 0


def test_non_signal_is_skipped():
    engine, _ = _engine()
    outcome = engine.handle_message("good morning team")
    assert outcome.parsed is None
    assert outcome.result is None
    assert outcome.skipped_reason == "not a signal"


def test_risk_rejection_skips_order():
    engine, broker = _engine(symbol_allowlist=("ETHUSDT",))
    outcome = engine.handle_message("BUY BTCUSDT @ 65000")
    assert outcome.parsed is not None
    assert outcome.result is None
    assert "risk" in outcome.skipped_reason
    assert "BTCUSDT" not in broker.positions


def test_market_order_uses_broker_price():
    engine, broker = _engine()
    outcome = engine.handle_message("BUY BTCUSDT market")
    assert outcome.acted
    assert outcome.result.price == 65000
