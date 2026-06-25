import pytest

from signaltrader.risk import RiskConfig, RiskRejection, size_order
from signaltrader.signals import OrderType, Side, Signal


def _sig(**kw):
    base = dict(symbol="BTCUSDT", side=Side.BUY)
    base.update(kw)
    return Signal(**base)


def test_explicit_quantity_respected_when_within_notional():
    order = size_order(
        _sig(quantity=2, entry=100), equity=10_000, price=100,
        config=RiskConfig(max_notional=10_000),
    )
    assert order.quantity == 2


def test_notional_cap_limits_quantity():
    order = size_order(
        _sig(quantity=100, entry=100), equity=10_000, price=100,
        config=RiskConfig(max_notional=1_000),
    )
    assert order.quantity == 10  # 1000 / 100


def test_risk_based_sizing_from_stop_distance():
    # 2% of 10_000 = 200 risk capital; stop is 5 away -> 40 units.
    order = size_order(
        _sig(entry=100, stop_loss=95), equity=10_000, price=100,
        config=RiskConfig(risk_per_trade=0.02, max_notional=1e9),
    )
    assert order.quantity == pytest.approx(40)


def test_allowlist_blocks_other_symbols():
    with pytest.raises(RiskRejection):
        size_order(
            _sig(entry=100), equity=10_000, price=100,
            config=RiskConfig(symbol_allowlist=("ETHUSDT",)),
        )


def test_require_stop_loss_rejects_without_one():
    with pytest.raises(RiskRejection):
        size_order(
            _sig(entry=100), equity=10_000, price=100,
            config=RiskConfig(require_stop_loss=True),
        )


def test_leverage_is_capped():
    order = size_order(
        _sig(entry=100, leverage=50), equity=10_000, price=100,
        config=RiskConfig(max_leverage=10),
    )
    assert order.leverage == 10


def test_limit_order_carries_price():
    order = size_order(
        _sig(entry=100, order_type=OrderType.LIMIT), equity=10_000, price=100,
        config=RiskConfig(),
    )
    assert order.order_type is OrderType.LIMIT
    assert order.price == 100
