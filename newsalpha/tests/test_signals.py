from datetime import datetime, timedelta, timezone

from newsalpha import select
from newsalpha.feeds import Article, parse_feed
from newsalpha.signals import rank_signals, score_ticker

NOW = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)


def _art(ticker, title, hours_ago=1.0, summary=""):
    return Article(
        ticker=ticker, title=title, summary=summary, source="t",
        url="https://example.invalid", published=NOW - timedelta(hours=hours_ago),
    )


def test_bullish_cluster_ranks_above_bearish():
    arts = [
        _art("NVDA", "Nvidia beats estimates and raises guidance", 0.5),
        _art("NVDA", "Analysts upgrade Nvidia on record demand", 1.0),
        _art("TSLA", "Tesla misses estimates, stock plunges on weak guidance", 0.5),
        _art("TSLA", "Tesla downgraded as analysts warn", 1.0),
    ]
    ranked = rank_signals(arts, now=NOW)
    assert ranked[0].ticker == "NVDA"
    assert ranked[0].score > 0
    assert ranked[-1].ticker == "TSLA"
    assert ranked[-1].score < 0
    assert ranked[0].action in ("BUY", "STRONG BUY")
    assert ranked[-1].action in ("AVOID", "STRONG AVOID")


def test_recency_weighting_prefers_fresh_news():
    fresh_bull = score_ticker(
        "X", [_art("X", "X surges on record profit beat", 0.1)], now=NOW)
    stale_bull = score_ticker(
        "X", [_art("X", "X surges on record profit beat", 47.0)],
        half_life_hours=12.0, now=NOW)
    assert fresh_bull.buzz > stale_bull.buzz
    assert abs(fresh_bull.score) > abs(stale_bull.score)


def test_more_agreeing_news_raises_confidence():
    one = score_ticker("X", [_art("X", "X beats estimates", 1.0)], now=NOW)
    many = score_ticker("X", [
        _art("X", "X beats estimates", 1.0),
        _art("X", "X surges on strong demand", 1.2),
        _art("X", "Analysts upgrade X, raise price target", 1.4),
    ], now=NOW)
    assert many.confidence > one.confidence


def test_empty_news_is_neutral():
    sig = score_ticker("ZZZ", [], now=NOW)
    assert sig.action == "NEUTRAL"
    assert sig.score == 0.0
    assert sig.confidence == 0.0


def test_select_with_injected_articles_is_offline():
    arts = [_art("NVDA", "Nvidia beats estimates and raises guidance", 0.5)]
    board = select(["NVDA", "AMD"], articles=arts, now=NOW)
    tickers = {s.ticker for s in board.ranked}
    assert {"NVDA", "AMD"} <= tickers  # both appear; AMD as neutral
    assert board.ranked[0].ticker == "NVDA"


def test_momentum_detects_improving_tone():
    sig = score_ticker("X", [
        _art("X", "X surges on record profit beat and upgrade", 0.2),  # newest, bullish
        _art("X", "X falls on weak guidance and downgrade", 20.0),     # old, bearish
    ], half_life_hours=48.0, now=NOW)
    assert sig.momentum > 0


def test_parse_feed_rss():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Apple beats estimates</title>
            <description>Strong quarter</description>
            <link>https://news.example/apple</link>
            <pubDate>Sun, 14 Jun 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    arts = parse_feed(xml, "AAPL")
    assert len(arts) == 1
    assert arts[0].title == "Apple beats estimates"
    assert arts[0].published is not None


def test_parse_feed_atom():
    xml = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Nvidia surges</title>
             <summary>Record demand</summary>
             <link href="https://news.example/nvda"/>
             <updated>2026-06-14T10:00:00Z</updated></entry>
    </feed>"""
    arts = parse_feed(xml, "NVDA")
    assert len(arts) == 1
    assert arts[0].url == "https://news.example/nvda"
