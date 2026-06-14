"""newsalpha — real-time, news-driven stock selector.

Pulls live financial news from keyless RSS feeds, scores each headline with a
finance-tuned sentiment model, and ranks your watchlist into an actionable
board (STRONG BUY → STRONG AVOID) with a confidence weight.

Public API::

    from newsalpha import select

    board = select(["AAPL", "NVDA", "TSLA"])
    for sig in board.ranked:
        print(sig.ticker, sig.score, sig.action)
    print(board.as_json())

⚠️  This is decision-support tooling. News sentiment is a single, noisy input;
it is **not** financial advice and does not guarantee profit. Always do your
own due diligence and manage risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import report
from .feeds import Article, fetch_news
from .signals import TickerSignal, rank_signals
from .universe import DEFAULT_WATCHLIST

__all__ = ["select", "Selection", "TickerSignal", "Article", "DEFAULT_WATCHLIST"]


@dataclass
class Selection:
    ranked: list[TickerSignal]
    articles: list[Article] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def buys(self) -> list[TickerSignal]:
        return [s for s in self.ranked if s.action in ("STRONG BUY", "BUY")]

    @property
    def avoids(self) -> list[TickerSignal]:
        return [s for s in self.ranked if s.action in ("STRONG AVOID", "AVOID")]

    def top(self, n: int = 5) -> list[TickerSignal]:
        return self.ranked[:n]

    def as_dict(self) -> dict:
        return report.to_dict(self.ranked)

    def as_json(self, indent: int = 2) -> str:
        return report.to_json(self.ranked, indent=indent)


def select(
    tickers: list[str] | None = None,
    *,
    since_hours: float = 48.0,
    half_life_hours: float = 12.0,
    min_articles: int = 1,
    timeout: float = 10.0,
    articles: list[Article] | None = None,
    now: datetime | None = None,
) -> Selection:
    """Fetch news for ``tickers`` and return a ranked :class:`Selection`.

    Args:
        tickers: symbols to screen (defaults to :data:`DEFAULT_WATCHLIST`).
        since_hours: ignore news older than this.
        half_life_hours: recency decay half-life for the sentiment weighting.
        min_articles: minimum fresh articles for a ticker to be scored.
        timeout: per-request network timeout.
        articles: pre-fetched articles (skips the network; used for offline /
            backtesting). When given, no fetching is performed.
        now: reference time (injectable for tests / replay).
    """
    tickers = list(tickers) if tickers else list(DEFAULT_WATCHLIST)
    now = now or datetime.now(timezone.utc)
    if articles is None:
        articles = fetch_news(tickers, since_hours=since_hours, timeout=timeout,
                              now=now)
    ranked = rank_signals(
        articles, tickers=tickers, half_life_hours=half_life_hours,
        min_articles=min_articles, now=now,
    )
    return Selection(ranked=ranked, articles=articles, generated_at=now)
