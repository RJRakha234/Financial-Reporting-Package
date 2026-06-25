from signaltrader.signals import OrderType, Side, parse_signal


def test_simple_buy_market():
    sig = parse_signal("BUY BTCUSDT market\nSL 64000\nTP 66000 67000")
    assert sig is not None
    assert sig.symbol == "BTCUSDT"
    assert sig.side is Side.BUY
    assert sig.order_type is OrderType.MARKET
    assert sig.stop_loss == 64000
    assert sig.targets == [66000, 67000]


def test_limit_entry_with_at_price():
    sig = parse_signal("BUY BTCUSDT @ 65000\nSL: 64000\nTP: 66000")
    assert sig is not None
    assert sig.order_type is OrderType.LIMIT
    assert sig.entry == 65000
    assert sig.entry_price == 65000


def test_hashtag_symbol_and_long():
    sig = parse_signal(
        "#ETH LONG\nEntry: 3200 - 3250\nTargets: 3300, 3400, 3500\n"
        "Stop loss: 3100\nLeverage: 10x"
    )
    assert sig is not None
    assert sig.symbol == "ETH"
    assert sig.side is Side.BUY
    assert sig.entry == 3200
    assert sig.entry_high == 3250
    assert sig.entry_price == 3225
    assert sig.targets == [3300, 3400, 3500]
    assert sig.stop_loss == 3100
    assert sig.leverage == 10


def test_short_is_sell():
    sig = parse_signal("SHORT #SOLUSDT now, sl 180, tp 150")
    assert sig is not None
    assert sig.side is Side.SELL
    assert sig.symbol == "SOLUSDT"
    assert sig.order_type is OrderType.MARKET


def test_thousands_separator():
    sig = parse_signal("buy BTCUSDT @ 1,05,000")  # forgiving on grouping
    assert sig is not None
    assert sig.entry == 105000


def test_non_signal_returns_none():
    assert parse_signal("gm everyone, great day to hodl") is None
    assert parse_signal("") is None
    assert parse_signal("   ") is None


def test_targets_do_not_swallow_stoploss_on_next_line():
    sig = parse_signal("LONG BTCUSDT\nTargets: 66000 67000\nSL: 64000")
    assert sig is not None
    assert sig.targets == [66000, 67000]
    assert sig.stop_loss == 64000


def test_slash_symbol():
    sig = parse_signal("BUY BTC/USDT @ 65000")
    assert sig is not None
    assert sig.symbol == "BTC/USDT"
