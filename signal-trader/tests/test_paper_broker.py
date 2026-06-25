from signaltrader.brokers import OrderRequest, PaperBroker
from signaltrader.signals import OrderType, Side


def _buy(symbol, qty, price=None):
    return OrderRequest(symbol, Side.BUY, qty, OrderType.MARKET, price=price)


def test_fill_reduces_cash_and_opens_position():
    b = PaperBroker(starting_cash=10_000, prices={"BTCUSDT": 100})
    res = b.place_order(_buy("BTCUSDT", 10))
    assert res.accepted
    assert res.price == 100
    assert b.cash == 9_000
    assert b.positions["BTCUSDT"].quantity == 10


def test_insufficient_cash_rejected():
    b = PaperBroker(starting_cash=100, prices={"BTCUSDT": 100})
    res = b.place_order(_buy("BTCUSDT", 10))  # needs 1000
    assert not res.accepted
    assert "insufficient" in res.message
    assert b.cash == 100
    assert "BTCUSDT" not in b.positions


def test_unknown_symbol_without_price_rejected():
    b = PaperBroker()
    res = b.place_order(_buy("DOGEUSDT", 1))
    assert not res.accepted
    assert "no price" in res.message


def test_explicit_price_overrides_book():
    b = PaperBroker(starting_cash=10_000)
    res = b.place_order(_buy("BTCUSDT", 2, price=250))
    assert res.accepted
    assert res.price == 250
    assert b.cash == 9_500


def test_buy_then_sell_closes_position():
    b = PaperBroker(starting_cash=10_000, prices={"BTCUSDT": 100})
    b.place_order(_buy("BTCUSDT", 10))
    sell = OrderRequest("BTCUSDT", Side.SELL, 10, OrderType.MARKET)
    res = b.place_order(sell)
    assert res.accepted
    assert "BTCUSDT" not in b.positions
    assert b.cash == 10_000


def test_equity_marks_to_market():
    b = PaperBroker(starting_cash=10_000, prices={"BTCUSDT": 100})
    b.place_order(_buy("BTCUSDT", 10))
    b.set_price("BTCUSDT", 120)
    # 9000 cash + 10 units * 120 = 10200
    assert b.equity() == 10_200
