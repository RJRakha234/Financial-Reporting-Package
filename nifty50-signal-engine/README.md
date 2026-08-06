# Nifty 50 Signal Engine

A locally-runnable, **alert-only** analytical tool for the NSE Nifty 50. It streams
market data, computes layered signals, and surfaces them on a dashboard. It does
not, and will not, place orders.

> **This tool generates signals, not investment advice.** It is a personal
> analytical aid, not an investment advisory service. Backtested performance does
> not predict future returns. After the full Indian cost stack, most retail
> systematic strategies underperform a plain Nifty index fund. Redistributing its
> signals to other people may trigger SEBI Research Analyst / Investment Adviser
> registration requirements — see [Regulatory posture](#regulatory-posture).

---

## Build status

The build follows the phased delivery protocol in the specification. **Phase 1 is
complete; phases 2–8 are not started.**

| # | Phase | Status |
|---|---|---|
| 1 | Broker adapter, data layer, corporate actions, trading calendar, integrity tests | **Complete** |
| 2 | Point-in-time universe + survivorship-bias test | Not started |
| 3 | Feature/indicator layer + reference-value unit tests | Not started |
| 4 | Backtest engine with the full Indian cost stack | Not started |
| 5 | Statistical + India-flow features, ML layer, walk-forward validation | Not started |
| 6 | Decision engine and fusion logic | Not started |
| 7 | Dashboard and alerts | Not started |
| 8 | Documentation pass | Not started |

`config.yaml` already carries the parameter blocks for later phases so it stays
the single source of truth; Phase 1 code does not read them.

---

## What Phase 1 gives you

```
src/nifty50/
├── config.py              typed, validated config.yaml loader (pydantic, extra="forbid")
├── domain.py              Candle / Tick / Instrument / Timeframe; tz-aware invariants
├── frames.py              the OHLCV DataFrame contract
├── logging_setup.py       structlog: JSON lines to a rotating file, readable console
├── trading_calendar/      NSE sessions, holidays, muhurat, the intraday bar grid
├── corporate_actions/     action table + back-adjustment of price AND volume
├── data/
│   ├── brokers/           BrokerAdapter ABC, Kite adapter, offline replay adapter
│   ├── store.py           parquet bar store, partitioned exchange/symbol/timeframe/month
│   ├── aggregator.py      session-aware tick → candle
│   ├── backfill.py        chunked, rate-limited, resumable historical pulls
│   ├── integrity.py       gaps, duplicates, bad bars, missed corporate actions
│   ├── stream.py          connect / degrade / recover / idle state machine
│   └── synthetic.py       deterministic bars for tests and offline development
└── scripts/               backfill and calendar-verification entry points
```

195 tests, `ruff` clean, `mypy --strict` clean.

---

## Setup

Requires Python 3.11+.

```bash
cd nifty50-signal-engine
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # add ',kite' for the live Kite adapter
cp .env.example .env             # then fill in your keys
pytest -q
```

Everything below works with no broker account: set `broker.name: replay` in
`config.yaml` and the offline adapter serves stored bars through the same
interface the live one does.

### Broker onboarding (Zerodha Kite Connect)

Kite is the default for documentation quality and stability, not price
(~₹500/month per API key). The data layer sits behind `BrokerAdapter`, so the
vendor is a config change plus one new adapter file.

1. Create an app at <https://developers.kite.trade>. Note the API key and secret.
2. Put both in `.env` as `KITE_API_KEY` / `KITE_API_SECRET`.
3. Run any entry point. With no valid token it prints the login URL and exits.
4. Log in; copy the `request_token` from the redirect URL into `.env`.
5. Re-run. The engine exchanges it for an access token, writes that back to
   `.env`, and clears the (single-use) request token.

**Steps 3–5 repeat every trading morning.** Kite access tokens expire daily at
around 06:00 IST and there is no refresh token; the OAuth redirect requires a
human. This is a property of the vendor, not a gap in the implementation. The
dashboard shows a token-expiry countdown for exactly this reason.

### Running

```bash
# Pull history and run the integrity suite over it
python -m nifty50.scripts.backfill --symbols RELIANCE,HDFCBANK,INFY --timeframes 15m,1d

# Check the seeded holiday calendar against the bars you actually have
python -m nifty50.scripts.verify_calendar --start 2019-01-01 --end 2026-12-31
```

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │              config.yaml                 │
                    │  every parameter; no magic numbers        │
                    └────────────────────┬─────────────────────┘
                                         │
  ┌──────────────┐   WebSocket   ┌───────▼────────┐
  │  Broker      │──────────────▶│ StreamSupervisor│  connect │ stale │ REST
  │  (Kite /     │   REST        │  state machine  │  fallback │ halted │ idle
  │   replay)    │◀──────────────└───────┬────────┘
  └──────┬───────┘                       │ ticks
         │ historical                    ▼
         │                       ┌───────────────┐   session-aware, cumulative
         │                       │CandleAggregator│   volume, ragged stub bars
         │                       └───────┬───────┘
         ▼                               │ closed candles
  ┌──────────────┐                       ▼
  │  Backfiller  │──────────────▶┌────────────────┐
  │ chunk/resume │   raw bars    │    BarStore    │  parquet, RAW prints only
  └──────────────┘               │ exch/sym/tf/ym │
                                 └───────┬────────┘
                                         │ read
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
          ┌──────────────────┐  ┌────────────────┐  ┌──────────────────┐
          │ CorporateActions │  │   Integrity    │  │ TradingCalendar  │
          │ adjust on read   │  │ gaps/dupes/    │  │ sessions, bar    │
          │ price AND volume │  │ missed actions │  │ grid, holidays   │
          └────────┬─────────┘  └────────────────┘  └──────────────────┘
                   │ adjusted series
                   ▼
        [ Phase 3+ : features → ML → decision engine → dashboard/alerts ]
```

---

## Design decisions that depart from the specification

Each of these is a deliberate deviation, not an oversight.

### Storage partitions by month, not by date

The spec says partition by `symbol/timeframe/date`. Fifty symbols × five
timeframes × seven years of daily partitions is ~440,000 files of a few kilobytes
each. That makes every scan metadata-bound, strains inode budgets, and turns a
full-history read into tens of thousands of `open()` calls. Month partitions give
~21,000 files of a few hundred KB — the size at which parquet row-group
statistics start paying for themselves — and are still fine-grained enough that
repairing one day rewrites one small file.

### Adjustment factors are applied on read, never baked into stored bars

Storage keeps the raw traded prints forever. Discovering a missed 2021 bonus in
2026 then re-adjusts history for free, with no rewrite and no risk of
double-adjusting a file that was already touched. It also means circuit bands,
price-band proximity, tick-size rounding and the whole cost stack keep operating
on prices that were actually printed — the only series on which those
calculations mean anything. Features and ML labels read the adjusted series;
cost and band logic reads the raw one. Both are configured explicitly under
`corporate_actions.feature_series` / `cost_and_band_series`.

### "No window rolls across a session boundary" — what that means in practice

Taken literally, a rule that no indicator window may span a session boundary
makes EMA(200) on 15-minute bars impossible. The rule that is actually
implemented and tested:

* the bar grid contains **no bar** between 15:30 and the next 09:15, so no
  window can silently interpolate across the gap;
* consecutive bars across a day boundary are **not** one bar-duration apart, and
  the calendar exposes `is_session_open_bar` so features can branch on it;
* the overnight move is a **gap event between two bars**, never a move inside
  one — Phase 3 will surface it as an explicit feature in ATR units;
* intraday accumulators that are meaningless across days (VWAP, the
  cumulative-volume baseline, first-hour range) reset at the session open.

Multi-day indicators still span days, as they must. What they never do is treat
the gap as elapsed intraday time.

### The hourly timeframe has a ragged final bar

The continuous session is 375 minutes, which divides evenly by 1, 5 and 15 but
not by 60. The last hourly bar of every NSE day is therefore a 15-minute stub
(15:15–15:30). It is emitted and flagged `partial=True`. Dropping it would lose
the closing run-up; stretching it to 16:15 would fabricate 45 minutes of price
action. Most home-built engines silently do one or the other.

### Bar volume is a difference of cumulative totals

Kite and most NSE feeds send `volume_traded` as the day's **running total**, not
the size of the last print — a tick is a snapshot of the book, not a trade
report. Summing `last_quantity` across ticks double-counts. Bar volume is
therefore `cumulative_now − cumulative_at_previous_bar_close`, rebased to zero at
the session open. When the engine attaches mid-session the baseline is genuinely
unknown, so those bars carry `volume_estimated=True` rather than claiming the
whole morning's turnover.

### Muhurat sessions are modelled, not ignored

Diwali muhurat trading is a real one-hour session that produces real bars and can
fall on a Saturday. It is in the calendar with its own window, excluded from
`is_trading_day`, and flagged `tradeable=False` so it does not contaminate
indicator windows.

### Daily bars are stamped at the session open, not midnight

Bars are start-stamped throughout. A daily bar stamped 00:00 would look available
nine hours before the session it summarises had begun — a look-ahead hazard
sitting in the index itself.

---

## Data provenance — read this before trusting a backtest

Three reference files ship as **seed data compiled from recall, not scraped from
an NSE circular**. They are a starting point, and the engine is built to correct
them from evidence rather than to trust them.

| File | Status |
|---|---|
| `data/reference/nse_holidays.csv` | Seeded 2019–2025. **2026 is deliberately incomplete** — only fixed-date national holidays. Load the official 2026 circular before running anything on 2026 data. |
| `data/reference/nse_special_sessions.csv` | Muhurat sessions 2019–2025, seeded. Times in particular should be verified. |
| `data/reference/corporate_actions.csv` | Five actions only, as a starting fixture. **This is nowhere near a complete Nifty 50 action history.** |

Two automated defences exist because of this:

* `nifty50.data.integrity.find_suspected_unadjusted_actions` flags any overnight
  move beyond a configured threshold that no corporate action explains. Treat
  every hit as a missing row until proven otherwise.
* `python -m nifty50.scripts.verify_calendar` cross-checks the holiday file
  against ingested bars in both directions — phantom holidays (bars exist on a
  supposed holiday) and missing holidays (no symbol traded on a supposed trading
  day) — and prints the rows to add or remove.

Run both after your first full backfill. Do not skip this step; a wrong calendar
row silently deletes or manufactures a session in every downstream computation.

The cost rates in `config.yaml` are likewise recorded with an `effective_from`
date and **must be verified against your own broker's contract note.** Rates
change by circular; applying 2026 rates to 2019 fills is one of the standard ways
a losing strategy backtests as a winner.

---

## Regulatory posture

SEBI's algo-trading framework (circular dated 4 February 2025, fully mandatory
from 1 April 2026) governs **order placement through broker APIs**, not
market-data consumption. Because this tool is alert-only and read-only, it sits
outside the algo-registration perimeter.

That posture is enforced structurally, not just documented:

* `BrokerAdapter` has no order-placement surface;
* `assert_read_only()` inspects any adapter class and raises
  `ReadOnlyViolationError` on `place_*`, `modify_*`, `cancel_*`, `exit_*`,
  `square_off*`, `*_order`, `gtt*` or `basket*` attributes;
* the check runs from `__init_subclass__`, so a non-conforming adapter fails at
  **import** time, not mid-session;
* `config.yaml` has no execution section, and a test asserts it never grows one;
* `runtime.mode` accepts only `paper` and `backtest`.

**Do not add an execution module.** If you want one, the following apply and the
project changes character entirely: strategies exceeding 10 orders/second per
exchange must be registered with the exchange through your broker and tagged with
a unique algo ID; only a static IP registered with the broker may send API
orders, with OAuth and 2FA mandatory; the broker is the principal and is
accountable for every algo on its platform, so you cannot connect directly to an
exchange; and distributing a "black box" strategy whose logic is hidden from its
user requires SEBI Research Analyst registration.

This tool is a personal analytical aid. It is not an investment advisory service.
Redistributing its signals to others may trigger SEBI RA/IA registration
requirements.

---

## Limitations (Phase 1)

* **No signals yet.** Phases 3–6 are not built. Nothing here produces a BUY,
  SELL or HOLD.
* **No point-in-time universe yet** (Phase 2). Until it exists, any backtest over
  today's constituent list carries full survivorship bias.
* **Only Kite and replay adapters.** Upstox, Angel One, Dhan and Fyers are
  config-shaped but unwritten.
* **Reference data is seeded, not authoritative** — see above.
* **Delivery %, FII/DII flows and bhavcopy ingestion are not implemented.** The
  config records the T+1 availability lag that will govern them; the loader is
  Phase 5.
* **Synthetic data is plumbing-only.** `synthetic.py` generates a geometric
  random walk with a plausible intraday volume U-shape. It has no
  microstructure, no fat tails and no real autocorrelation. Any strategy result
  computed on it is meaningless by construction, and it is used only to exercise
  session boundaries, aggregation, storage and gap detection.
* **The replay adapter's depth ladder is synthetic.** It exercises order-book
  imbalance code paths; it must never be mistaken for a real book when
  evaluating a depth-sensitive strategy.
* **REST fallback is implemented as a single-poll primitive**
  (`StreamSupervisor.poll_rest_once`) and is not yet wired into an automatic
  polling loop.
* **India VIX term structure is not directly available.** India VIX is a single
  30-day index; NSE publishes no term structure for it. Phase 3 will either
  compute a near/next-month implied-vol ratio from the NIFTY option chain or
  report the feature as unavailable — it will not be faked from the spot index.

## What would break this

* **Index rebalancing.** The Nifty 50 is reconstituted semi-annually; without the
  Phase 2 point-in-time table, every historical result is survivorship-biased.
* **Regulatory change.** STT rates, stamp duty, exchange transaction charges and
  lot sizes all move by circular. The cost stack is dated for this reason.
* **Broker API deprecation and daily token expiry.** Kite tokens die every
  morning and cannot be refreshed unattended; an unmanned overnight run will find
  a dead session at 09:15.
* **Circuit halts.** A stock frozen at a band cannot be filled through. Fills
  assumed through a circuit limit are one of the four standard causes of a
  fake-winning Indian backtest.
* **Results-season volatility.** Earnings-day gaps dominate short-horizon
  features and are not tradeable in the way a backtest implies.
* **Regime.** A strategy fitted on the last seven years of a broadly rising
  Indian market has never seen a sustained bear regime. Assume the out-of-sample
  performance is worse than anything you measure.
