# newsalpha — real-time, news-driven stock selector

`newsalpha` watches the live news flow for your stock universe, scores every
headline with a **finance-tuned sentiment model**, and ranks your watchlist into
an actionable board — from **STRONG BUY** to **STRONG AVOID** — each weighted by
how much fresh, agreeing news is behind it.

It is built for the way news actually moves stocks: an upgrade from 20 minutes
ago matters far more than yesterday's, three sources agreeing matters more than
one, and a *turning* tone (improving or worsening) is itself a signal.

```
📊 newsalpha — news-driven stock board  (2026-06-14 21:15 UTC)
========================================================================
 #  TICKER   SCORE  GAUGE   ACTION         CONF  HEADLINE
------------------------------------------------------------------------
 1  NVDA      70.0  +████·  STRONG BUY      46%  Nvidia beats estimates and raises guidance…
 2  MSFT      53.1  +███··  STRONG BUY      25%  Microsoft Azure growth accelerates on stro…
 3  AAPL      17.7  +█····  WATCH           20%  Apple iPhone sales rise modestly but servi…
 8  TSLA     -66.2  -███··  STRONG AVOID    40%  Tesla misses delivery estimates, stock plu…
------------------------------------------------------------------------
```

> ⚠️ **Read this first.** `newsalpha` is **decision-support tooling, not
> financial advice, and not a profit guarantee.** News sentiment is one noisy
> input among many. No tool — this one included — can promise you make money in
> the market. Markets are adversarial, news is often already priced in, and you
> can lose money. Use this to *screen and prioritise* what to research, size
> positions sensibly, manage risk, and make your own decisions. Past or
> backtested behaviour does not predict future results.

## Why it works without any setup

* **No API keys, no signup, no paid data.** News comes from public, keyless RSS
  feeds (Yahoo Finance per-symbol headlines + a Google News keyword search).
* **Standard library only.** Nothing to `pip install` to use it. Sentiment,
  fetching, parsing, ranking, and the CLI are all pure Python.
* **Works offline too.** A bundled sample lets you try the whole pipeline with
  no network, and you can feed it your own saved headlines for backtesting.

## Quick start

```bash
# 1) Try it instantly with the offline sample (no network needed)
python make_sample.py
python -m newsalpha --offline sample_news.json

# 2) Go live: rank the default watchlist by real-time news
python -m newsalpha

# 3) Screen your own tickers
python -m newsalpha AAPL NVDA TSLA AMD MSFT --top 5

# 4) Live "trading desk" mode — refresh every 60s until Ctrl-C
python -m newsalpha NVDA AMD TSLA --watch 60

# 5) Machine-readable output for your own pipeline / bot
python -m newsalpha AAPL NVDA --json
```

### Useful flags

| Flag | What it does |
|------|--------------|
| `--top N` | show only the strongest/weakest N rows |
| `--since HOURS` | ignore news older than this (default 48) |
| `--half-life HOURS` | recency decay half-life (default 12) — lower = more reactive |
| `--min-articles N` | require at least N fresh articles to score a ticker |
| `--detail` | print the headline-by-headline sentiment breakdown |
| `--watch SECONDS` | live mode: re-fetch and redraw the board on an interval |
| `--offline PATH` | score a local JSON file instead of the network |
| `--json` | emit JSON instead of the console board |

## Use it as a library

```python
from newsalpha import select

board = select(["AAPL", "NVDA", "TSLA"], half_life_hours=8, since_hours=24)

for sig in board.top(5):
    print(f"{sig.ticker:5} {sig.score:6.1f}  {sig.action:11} "
          f"conf={sig.confidence:.0%}  {sig.rationale}")

print("Buys :", [s.ticker for s in board.buys])
print("Avoid:", [s.ticker for s in board.avoids])
print(board.as_json())
```

Backtest / replay on your own saved headlines (no network):

```python
from newsalpha import select
from newsalpha.feeds import Article

articles = [Article("NVDA", "Nvidia beats estimates and raises guidance",
                    "record demand", "wire", "http://…", published=my_datetime)]
board = select(["NVDA"], articles=articles)   # articles given -> offline
```

## How the score is built

For each ticker, every headline gets a sentiment in `[-1, 1]`, then four
factors are combined into the composite score (≈ `-100…+100`):

1. **Direction** — recency-weighted average sentiment. Recency uses exponential
   time-decay: weight `= 0.5 ** (age_hours / half_life)`, so newer news
   dominates.
2. **Conviction** — directional agreement: do the fresh stories point the same
   way, or contradict each other?
3. **Buzz** — the effective count of *fresh* articles (saturating), so a single
   headline can't masquerade as high confidence.
4. **Momentum** — newest headlines vs older ones, to catch a tone that is
   turning before the average does.

```
score = 100 · avg_sentiment · (0.5 + 0.5·confidence) + 18 · momentum · buzz_conf
confidence = volume_conf · (0.4 + 0.6·agreement)
```

`ACTION` buckets the score, but only commits to BUY/AVOID when there is enough
fresh, agreeing news behind it — otherwise it stays `WATCH`/`NEUTRAL`.

### The sentiment model

A hand-curated, **finance-specific** lexicon (Loughran-McDonald inspired):
*beats, upgrade, raises guidance, record high, bullish* push up; *miss,
downgrade, cuts guidance, lawsuit, plunge, recall, bankruptcy* push down. It
handles **negation** ("did **not** beat"), **intensifiers** ("**sharply**
lower"), and **multi-word phrases** ("beat estimates", "price target cut") that
trip up general-purpose models. Scores are squashed with `tanh` so a pile-up of
strong words saturates instead of running away. If you install the optional
`vaderSentiment` package it can be blended in, but nothing requires it.

## Customising the news sources

Set `NEWSALPHA_FEEDS` to a comma-separated list of RSS URL templates
(`{ticker}` → symbol, `{query}` → URL-encoded company query) to point at your
own / additional feeds:

```bash
export NEWSALPHA_FEEDS="https://news.google.com/rss/search?q={query}&hl=en-US"
python -m newsalpha NVDA
```

The ticker→company map (used to build queries and filter for relevance) lives in
`newsalpha/universe.py` — add your names/aliases there for better recall.

## Responsible use

* This is a **screening and prioritisation** aid, not an auto-trader. It does
  not place orders.
* Headlines can be **stale, already priced in, paywalled, or wrong**; RSS
  coverage is uneven. Always read the underlying story before acting.
* **Validate before risking capital.** Use `--offline` / injected articles to
  backtest the scoring against past events and your own outcomes first.
* Markets carry real risk of loss. Nothing here is investment advice. You are
  responsible for your own trades.

## Testing

```bash
pip install -r requirements.txt   # only needs pytest
python -m pytest -q
```
