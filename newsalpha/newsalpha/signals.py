"""Turn a pile of news articles into a ranked, actionable signal per ticker.

The composite score blends four explainable factors:

* **Direction** — recency-weighted average sentiment of the headlines.
* **Conviction** — how much fresh news agrees on direction (consistency).
* **Buzz** — effective volume of fresh articles (more news == more confidence).
* **Momentum** — whether the most recent headlines are improving or worsening
  versus the older ones.

Recency uses exponential time-decay with a configurable half-life, so a story
from 30 minutes ago counts far more than one from two days ago — which is what
you want for trading the *new* information, not yesterday's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import sentiment
from .feeds import Article


@dataclass
class TickerSignal:
    ticker: str
    score: float                 # composite, roughly [-100, 100]
    action: str                  # STRONG BUY / BUY / WATCH / NEUTRAL / AVOID / STRONG AVOID
    confidence: float            # 0..1
    n_articles: int
    avg_sentiment: float         # [-1, 1], recency-weighted
    momentum: float              # [-1, 1], recent vs older
    buzz: float                  # effective fresh-article count
    agreement: float             # 0..1 directional consensus
    latest: Article | None = None
    headlines: list[tuple[float, str]] = field(default_factory=list)  # (sentiment, title)
    rationale: str = ""

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "score": round(self.score, 1),
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "n_articles": self.n_articles,
            "avg_sentiment": round(self.avg_sentiment, 3),
            "momentum": round(self.momentum, 3),
            "buzz": round(self.buzz, 2),
            "agreement": round(self.agreement, 3),
            "latest_headline": self.latest.title if self.latest else None,
            "latest_url": self.latest.url if self.latest else None,
            "rationale": self.rationale,
            "top_headlines": [
                {"sentiment": round(s, 3), "title": t} for s, t in self.headlines
            ],
        }


def _decay_weight(age_hours: float, half_life: float) -> float:
    return 0.5 ** (age_hours / max(half_life, 1e-6))


def _classify(score: float, confidence: float, buzz: float) -> str:
    if confidence < 0.25 or buzz < 0.75:
        return "WATCH" if buzz >= 0.75 else "NEUTRAL"
    if score >= 40:
        return "STRONG BUY"
    if score >= 15:
        return "BUY"
    if score <= -40:
        return "STRONG AVOID"
    if score <= -15:
        return "AVOID"
    return "WATCH"


def score_ticker(
    ticker: str,
    articles: list[Article],
    *,
    half_life_hours: float = 12.0,
    now: datetime | None = None,
) -> TickerSignal:
    """Compute the composite signal for one ticker from its articles."""
    now = now or datetime.now(timezone.utc)
    ticker = ticker.upper()

    if not articles:
        return TickerSignal(
            ticker=ticker, score=0.0, action="NEUTRAL", confidence=0.0,
            n_articles=0, avg_sentiment=0.0, momentum=0.0, buzz=0.0,
            agreement=0.0, rationale="no fresh news",
        )

    scored: list[tuple[Article, float, float]] = []  # (article, sentiment, weight)
    for art in articles:
        s = sentiment.score_text(art.text)
        w = _decay_weight(art.age_hours(now), half_life_hours)
        scored.append((art, s, w))

    total_w = sum(w for _, _, w in scored) or 1e-9
    avg_sentiment = sum(s * w for _, s, w in scored) / total_w
    buzz = total_w  # effective count of "fresh" articles

    # Directional agreement: net bullish/bearish weight over total opinionated.
    pos_w = sum(w for _, s, w in scored if s > 0.05)
    neg_w = sum(w for _, s, w in scored if s < -0.05)
    opin = pos_w + neg_w
    agreement = abs(pos_w - neg_w) / opin if opin > 1e-9 else 0.0

    # Momentum: newest third vs the rest (by recency-sorted articles).
    by_age = sorted(scored, key=lambda x: x[0].age_hours(now))
    cut = max(1, len(by_age) // 3)
    recent = by_age[:cut]
    older = by_age[cut:]
    recent_avg = sum(s for _, s, _ in recent) / len(recent)
    momentum = 0.0
    if older:
        older_avg = sum(s for _, s, _ in older) / len(older)
        momentum = max(-1.0, min(1.0, recent_avg - older_avg))

    # Confidence: grows with volume (saturating) and directional agreement.
    volume_conf = buzz / (buzz + 3.0)         # ~0.25 at 1 article, ~0.77 at 10
    confidence = volume_conf * (0.4 + 0.6 * agreement)

    # Composite score: direction scaled by confidence, nudged by momentum.
    score = 100.0 * avg_sentiment * (0.5 + 0.5 * confidence)
    score += 18.0 * momentum * volume_conf
    score = max(-100.0, min(100.0, score))

    action = _classify(score, confidence, buzz)

    headlines = sorted(
        ((s, a.title) for a, s, _ in scored), key=lambda x: abs(x[0]), reverse=True
    )[:3]
    latest = min(articles, key=lambda a: a.age_hours(now))

    rationale = _rationale(avg_sentiment, momentum, agreement, int(round(buzz)),
                           len(articles))

    return TickerSignal(
        ticker=ticker, score=score, action=action, confidence=confidence,
        n_articles=len(articles), avg_sentiment=avg_sentiment, momentum=momentum,
        buzz=buzz, agreement=agreement, latest=latest, headlines=headlines,
        rationale=rationale,
    )


def _rationale(avg: float, mom: float, agree: float, fresh: int, n: int) -> str:
    parts = [f"{n} article(s), ~{fresh} fresh", sentiment.label(avg)]
    if mom > 0.12:
        parts.append("improving tone")
    elif mom < -0.12:
        parts.append("worsening tone")
    parts.append(f"{round(agree * 100)}% directional agreement")
    return "; ".join(parts)


def rank_signals(
    articles: list[Article],
    *,
    tickers: list[str] | None = None,
    half_life_hours: float = 12.0,
    min_articles: int = 1,
    now: datetime | None = None,
) -> list[TickerSignal]:
    """Group articles by ticker, score each, and return them best-first.

    Args:
        articles: articles from :func:`newsalpha.feeds.fetch_news`.
        tickers: optional explicit universe; tickers with no news still appear
            (as NEUTRAL) so you can see coverage gaps.
        half_life_hours: recency decay half-life.
        min_articles: drop tickers with fewer than this many articles.
        now: reference time (injectable for tests).
    """
    now = now or datetime.now(timezone.utc)
    grouped: dict[str, list[Article]] = {}
    for art in articles:
        grouped.setdefault(art.ticker.upper(), []).append(art)
    if tickers:
        for t in tickers:
            grouped.setdefault(t.upper(), [])

    signals = [
        score_ticker(t, arts, half_life_hours=half_life_hours, now=now)
        for t, arts in grouped.items()
        if len(arts) >= min_articles or (tickers and t.upper() in
                                         {x.upper() for x in tickers})
    ]
    signals.sort(key=lambda s: (s.score, s.confidence), reverse=True)
    return signals
