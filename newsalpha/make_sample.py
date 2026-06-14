"""Generate a sample offline news file so you can try newsalpha with no network.

    python make_sample.py            # writes sample_news.json
    python -m newsalpha --offline sample_news.json

The timestamps are relative to *now* so recency weighting behaves realistically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


SAMPLE = [
    # NVDA — strong, consistent, fresh bullish flow
    ("NVDA", 0.5, "Nvidia beats estimates and raises guidance on record AI chip demand",
     "Data-center revenue surged as Blackwell GPUs sold out for the quarter."),
    ("NVDA", 2.0, "Analysts upgrade Nvidia, price target raised on accelerating orders",
     "Several firms turned bullish citing robust demand and strong momentum."),
    ("NVDA", 6.0, "Nvidia announces new partnership to expand AI data centers",
     "The deal is expected to boost long-term growth."),
    # AAPL — mixed
    ("AAPL", 1.5, "Apple iPhone sales rise modestly but services growth slows",
     "Solid quarter though demand in one region looked weaker."),
    ("AAPL", 20.0, "Apple faces lawsuit over App Store practices",
     "Regulators opened a probe into the company's fees."),
    # TSLA — worsening, bearish
    ("TSLA", 0.5, "Tesla misses delivery estimates, stock plunges in premarket",
     "A demand slowdown and price cuts pressured margins."),
    ("TSLA", 3.0, "Tesla downgraded as analysts warn on weak guidance",
     "Bearish notes cited rising competition and shrinking deliveries."),
    ("TSLA", 30.0, "Tesla recalls vehicles over software issue",
     "The recall affects a large number of units."),
    # AMD — quiet, neutral
    ("AMD", 10.0, "AMD to present at industry conference next week",
     "The company will discuss its product roadmap."),
    # MSFT — moderately positive
    ("MSFT", 4.0, "Microsoft Azure growth accelerates on strong Copilot demand",
     "Cloud momentum lifted the outlook."),
    ("MSFT", 26.0, "Microsoft wins large government cloud contract",
     "The award boosts the company's commercial backlog."),
]


def build() -> dict:
    articles = []
    for ticker, hours_ago, title, summary in SAMPLE:
        articles.append({
            "ticker": ticker,
            "title": title,
            "summary": summary,
            "source": "sample.local",
            "url": "https://example.invalid/news",
            "published": _ago(hours_ago),
        })
    return {"generated_at": NOW.isoformat(), "articles": articles}


if __name__ == "__main__":
    with open("sample_news.json", "w", encoding="utf-8") as fh:
        json.dump(build(), fh, indent=2)
    print("Wrote sample_news.json — try:  python -m newsalpha --offline sample_news.json")
