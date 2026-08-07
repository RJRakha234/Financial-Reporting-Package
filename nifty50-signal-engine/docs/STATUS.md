# Nifty 50 Signal Engine — Development Status

**As of:** 2026-08-07
**Branch:** `claude/nifty50-signal-engine-n0ffwq` (repository `RJRakha234/Financial-Reporting-Package`)
**Progress:** Phases 1–4 of 8 complete
**Health:** 355 tests passing · `ruff` clean · `mypy --strict` clean

---

## 1. Executive summary

The engine's entire **data and evaluation spine** is built and tested: it can ingest
NSE market data, adjust it for corporate actions, know exactly which sessions and
bars exist, compute 57 leak-free features, and run an event-driven backtest under
the full nine-charge Indian cost stack.

It **cannot yet produce a trustworthy performance number**, and this is deliberate.
The point-in-time constituents file ships empty and the backtest refuses to start
without it. Every result produced so far comes from synthetic bars and validates
plumbing only.

Nothing that generates a BUY/SELL/HOLD decision exists yet. Phases 5–8 — the
statistical/India-flow features, the ML layer, the decision engine, the dashboard
and alerts — are not started.

### Scale

| | |
|---|---|
| Source | 7,722 lines across 45 modules |
| Tests | 4,441 lines, 355 tests, 18 files |
| Commits | 5 on the feature branch |
| Test-to-source ratio | ~0.58 |

### Module sizes

| Module | Lines | Phase |
|---|---:|---|
| `data/` (incl. `brokers/`) | 2,821 | 1 |
| `backtest/` | 1,443 | 4 |
| `features/` | 1,160 | 3 |
| `top-level` (`config`, `domain`, `frames`, `logging_setup`) | 651 | 1 |
| `corporate_actions/` | 501 | 1 |
| `trading_calendar/` | 473 | 1 |
| `universe/` | 348 | 2 |
| `scripts/` | 325 | 1–2 |

---

## 2. Phase-by-phase status

| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Broker adapter, data layer, corporate actions, calendar, integrity | **Complete** | Reference data is seeded, needs verification |
| 2 | Point-in-time universe + survivorship-bias test | **Complete (machinery)** | Membership data must be supplied by hand |
| 3 | Feature/indicator layer + reference-value tests | **Complete** | — |
| 4 | Backtest engine + full Indian cost stack | **Complete** | Robustness analysis partial |
| 5 | Statistical + India-flow features, ML, walk-forward | **Not started** | — |
| 6 | Decision engine and fusion logic | **Not started** | `signals/` is an empty directory |
| 7 | Dashboard and alerts | **Not started** | `dashboard/`, `alerts/` are empty directories |
| 8 | Full documentation pass | **Not started** | README current but not final |

---

## 3. Phase 1 — Data layer, calendar, corporate actions

### Built

**`data/brokers/`** — `BrokerAdapter` ABC with two implementations:
- `KiteAdapter` — Zerodha Kite Connect, market data only. Handles the daily OAuth
  token cycle, per-timeframe historical request caps, websocket subscription caps,
  and threaded tick callbacks.
- `ReplayAdapter` — offline adapter serving stored bars through the identical
  interface, with deliberate fault injection (socket drops, duplicate ticks,
  reordered ticks, auth failure). This is what makes the whole data layer testable
  in CI without credentials.

**`data/store.py`** — Parquet bar store partitioned `exchange/symbol/timeframe/year-month`.
Atomic writes via temp-file-and-rename. Upsert semantics with later-write-wins.
Coverage queries drive restart-and-resume.

**`data/aggregator.py`** — Session-aware tick→candle aggregation.

**`data/backfill.py`** — Chunked, rate-limited, resumable historical pulls.

**`data/integrity.py`** — Gaps, duplicates, out-of-order bars, OHLC-contract
violations, live-vs-REST reconciliation, suspected unadjusted corporate actions,
and calendar disagreement in both directions.

**`data/stream.py`** — Connect → stale → REST-fallback → halt → idle state machine.

**`trading_calendar/`** — NSE sessions, holidays, muhurat evenings, and the
intraday bar grid that is the ground truth for gap detection.

**`corporate_actions/`** — Split, bonus, rights, dividend and demerger adjustment
of price **and** volume, with cumulative factors.

### Key design decisions

**Storage partitions by month, not date.** The spec says partition by
`symbol/timeframe/date`. 50 symbols × 5 timeframes × 7 years of daily partitions
is ~440,000 files of a few KB — metadata-bound scans, inode pressure, tens of
thousands of `open()` calls per full read. Month partitions give ~21,000 files of
a few hundred KB, where parquet row-group statistics start earning their keep,
and a single-day repair still rewrites one small file.

**Adjustment factors applied on read, never baked into stored bars.** Storage keeps
raw traded prints forever. Discovering a missed 2021 bonus in 2026 re-adjusts
history for free — no rewrite, no double-adjust risk. Circuit bands, price-band
proximity and the cost stack keep operating on prices that were actually printed.
Features and ML labels read the adjusted series; cost and band logic reads raw.

**Bar volume is a difference of cumulative totals.** Kite sends `volume_traded` as
the day's running total — a tick is a book snapshot, not a trade report — so
summing `last_quantity` double-counts. Volume is
`cumulative_now − cumulative_at_previous_bar_close`, rebased at the session open.
Cold starts (attaching mid-session) carry `volume_estimated=True` rather than
claiming the whole morning's turnover.

**The hourly timeframe has a ragged final bar.** The continuous session is 375
minutes, which divides evenly by 1, 5 and 15 but not 60. Every NSE day's last
hourly bar is a 15-minute stub (15:15–15:30), emitted and flagged `partial=True`.
Dropping it loses the closing run-up; stretching it to 16:15 fabricates 45 minutes.

**Daily bars are stamped at the session open, not midnight.** A daily bar stamped
00:00 would look available nine hours before the session it summarises began — a
look-ahead hazard sitting in the index itself.

**Muhurat sessions are modelled, not ignored.** A real one-hour Diwali session that
can fall on a Saturday. Own window, excluded from `is_trading_day`, flagged
`tradeable=False`.

**`schedule()` raises on a closed day.** "Never signal on a non-trading day" is
enforced by the type of failure rather than by everyone remembering to check.

### "No window rolls across a session boundary" — what was actually implemented

Taken literally, this makes EMA(200) on 15-minute bars impossible. What is
implemented and tested:

- the bar grid contains **no bar** between 15:30 and the next 09:15;
- consecutive bars across a day boundary are **not** one bar-duration apart, and
  `is_session_open_bar` lets features branch on it;
- the overnight move is a **gap event between two bars**, never inside one;
- intraday accumulators (VWAP, the volume baseline, first-hour range) reset at
  the open.

Multi-day indicators still span days, as they must. What they never do is treat
the gap as elapsed intraday time.

### Regulatory posture — enforced structurally

- `BrokerAdapter` has no order-placement surface.
- `assert_read_only()` raises `ReadOnlyViolationError` on `place_*`, `modify_*`,
  `cancel_*`, `exit_*`, `square_off*`, `*_order`, `gtt*`, `basket*`.
- The check runs from `__init_subclass__`, so a non-conforming adapter fails at
  **import** time, not mid-session.
- `config.yaml` has no execution section, and a test asserts it never grows one.
- `runtime.mode` accepts only `paper` and `backtest`.

---

## 4. Phase 2 — Point-in-time universe

### Built

`universe/constituents.py` — `PointInTimeUniverse` with as-of membership queries,
`Provenance` tracking, overlap rejection, size-invariant validation, and
`assert_backtest_ready()`.

`scripts/verify_universe.py` — validates the file and reports what's missing;
`--check-store` also flags members whose price history was never downloaded.

### The design decision that matters

**It fails closed.** `data/reference/nifty50_constituents.csv` ships with **no data
rows**, on purpose.

Shipping today's fifty names with open-ended start dates would look helpful and
would silently reintroduce survivorship bias into every result the project ever
produces — a basket selected *because* it survived hands the strategy
foreknowledge of which companies did not blow up. So there is no plausible
placeholder to forget to delete.

`assert_backtest_ready()` raises on: an empty file, any `provenance=seed` row, or
the index not being exactly `universe.expected_size` names on a sampled trading day.

### Two API details worth knowing

- `symbols_ever()` returns every name that was *ever* a member, including departed
  ones. That is the set to backfill price data for — a point-in-time universe is
  useless if you only downloaded the survivors.
- A symbol may leave and later rejoin: two rows with disjoint ranges. *Overlapping*
  ranges for one symbol are rejected at load as a data-entry error.

### Edge case found

With `end_date` and `start_date` both inclusive, a spell ending 2020-01-01 and the
next starting 2020-01-01 double-counts that symbol for one day — the index reads
51. It looks like clean adjacency and it is a bug. Overlap detection treats
touching bounds as an overlap, with a test pinning it.

---

## 5. Phase 3 — Feature layer

### Built

57 features across four modules:

- **`trend.py`** — EMA ribbon (9/21/50/200) + stack score + crossover state/events,
  MACD, Wilder RSI + divergence, ADX with ±DI, Supertrend, Ichimoku, rate of
  change, index-relative strength.
- **`volatility.py`** — ATR + percentile, Bollinger (%B, bandwidth), Keltner,
  squeeze, realized volatility, volatility regime, India VIX gate.
- **`volume.py`** — session-matched volume z-score, relative volume, OBV + slope,
  session VWAP, distance-from-VWAP in ATR units, order-book imbalance, breakout state.
- **`session.py` / `multiframe.py`** — session ordinal, bar-of-session, overnight
  gap, opening-range extremes, higher-timeframe alignment and conflict scoring.

### The look-ahead test is the centrepiece

`tests/test_lookahead.py` computes the whole feature set on full history,
recomputes on history truncated at bar *k*, and compares row *k−1* column by
column — over **every** column, not a sample, so a new indicator with a leak fails
the day it is added.

It also **self-tests**: a deliberately leaky centred rolling mean must be rejected,
because a guard that cannot fail is not a guard. Backed by a static AST scan
rejecting `shift(-n)` anywhere in the source tree.

### Three leaks handled explicitly

**1. Higher-timeframe joins align on bar *close*, not start.** A 1-hour bar stamped
10:15 covers 10:15–11:15 and is only complete at 11:15. The obvious
`reindex(...).ffill()` hands the 10:30 bar an hourly bar containing 45 minutes of
prices it could not have known — the strategy then appears to predict the next
three quarters of an hour because it was shown them. `align_higher_timeframe`
merges as-of each higher bar's close, clamped to the session close so the ragged
15:15–15:30 stub is available from 15:30 rather than a fictional 16:15. A test
asserts both the fix and that the naive join genuinely would have leaked.

**2. Ichimoku's Chikou span is not emitted.** Chikou is the close displaced 26
periods *backwards*, so reading it at its plotted position is reading a price that
has not happened. The Senkou spans are displaced *forwards*, carry 26-bar-old
data, and are emitted.

**3. Divergence is stamped at confirmation, not at the pivot.** A swing high is only
a swing high once N further bars have failed to exceed it.

### Session-matched volume — the India-specific one

NSE intraday volume is strongly U-shaped: heavy at the open, thin at midday, heavy
into the close. A flat trailing baseline reports "unusually busy" every single open
and "unusually quiet" every single midday — a feature that looks informative and is
measuring nothing but the clock.

The test pins exactly that, on identical bars: flat baseline median |z| **> 4.0**,
session-matched median |z| **< 1.0**.

`relative_volume` adds a median-based companion that survives a single outlier and
a degenerate zero-dispersion history.

### Test approach for "known-good reference values"

Two kinds of assertion, deliberately:

- **Analytic cases** where the answer follows from the definition — EMA of a
  constant, RSI of a monotonic rise = 100, ATR of a fixed range, Keltner bands at
  ±3 on a 2-point range, Bollinger bandwidth 0 on a flat series.
- **Independent reimplementation** — a slow, obviously-correct version written in
  the test and compared against the vectorised one.

This avoids transcribing numbers from a chart nobody can re-derive.

### Deliberate restriction

**India VIX term structure is not emitted.** India VIX is a single 30-day index;
NSE publishes no term structure for it. A near/next-month implied-vol ratio would
have to be computed from the NIFTY option chain, which is Phase 5 data the engine
does not yet ingest. Only the level and a panic gate are emitted — the alternative
would be fabricating a term structure from a spot index.

---

## 6. Phase 4 — Backtest engine and the Indian cost stack

### The cost stack

Nine charges per round trip, differing by trade style, several one-sided, with GST
compounding on a subset. On ₹1,00,000 closed for a clean 1% gain:

| Charge | Intraday | Delivery |
|---|---:|---:|
| Brokerage | ₹40.00 | ₹0.00 |
| **STT** | ₹25.25 | **₹201.00** |
| Exchange transaction | ₹5.97 | ₹5.97 |
| SEBI turnover fee | ₹0.20 | ₹0.20 |
| IPFT | ₹0.20 | ₹0.20 |
| Stamp duty | ₹3.00 | ₹15.00 |
| GST | ₹8.35 | ₹1.15 |
| DP charges | — | ₹15.93 |
| **Total** | **₹82.97** | **₹239.45** |
| **Share of the ₹1,000 gross win** | **8.3%** | **23.9%** |
| **Breakeven move** | **0.083%** | **0.238%** |

Two facts the model captures that a flat-percentage approximation destroys:

- **STT is charged on both legs for delivery, at four times the intraday rate.** It
  is 84% of the delivery total. A strategy that looks marginal intraday can be
  hopeless as a delivery strategy.
- **Brokerage is capped at ₹20 and therefore regressive.** A ₹10,000 trade pays
  0.03%; a ₹10,00,000 trade pays 0.002%. Modelling it as a flat percentage is wrong
  in both directions depending on size.
- **GST never taxes a tax** — it applies to brokerage + exchange + SEBI + IPFT, not
  to STT or stamp duty.

Every expected number in `tests/test_backtest_costs.py` is derived from the
configured rate *in the test itself*, so a rate change surfaces as a failure with
an arithmetic trail rather than as a quietly different backtest.

### What the engine refuses to do

- Start unless the point-in-time universe is verified and complete.
- Let a strategy trade a name that was not in the index **on that date**. A
  reconstitution test asserts a dropped name cannot be traded after it left and an
  added one cannot be traded before it joined.
- Act on a signal inside the bar that produced it — fills happen at the **next**
  bar's open.
- Fill through a circuit limit. It rejects rather than clamping to the band,
  because clamping books a trade that could not have happened.
- Assume infinite liquidity — order size is capped at a configurable share of the
  bar's traded volume.
- Let slippage help — it always moves against the trader, and impact grows with the
  square root of participation.
- Recycle unsettled cash — delivery sale proceeds are locked until T+1.
- Carry an intraday position past the cut-off — forced square-off.

### Reporting

Gross and net PnL are always carried together. `costs_ate_the_edge` flags the case
that matters most: profitable gross, loss-making net.

### Baseline result — and what it does *not* mean

EMA(9/21) crossover, 7 years, delivery, on **synthetic** bars:

| | |
|---|---:|
| Trades | 222 |
| Gross PnL | **+₹1,67,693** |
| Costs | ₹2,55,233 |
| Net PnL | **−₹87,540** |
| Costs / gross | **152.2%** |
| Sharpe | −0.55 |
| Max drawdown | −48.2% |
| Buy-and-hold net | +₹1,42,697 |

> **This is synthetic data and is not strategy evidence.** A geometric random walk
> has no edge to find by construction, so an EMA crossover *should* lose on it.
> What it demonstrates is the mechanism the spec warns about: the cost stack alone
> is large enough to flip a gross winner into a net loser, and to lose to doing
> nothing.

### Implemented vs deferred robustness

- **Implemented:** Monte Carlo trade-order reshuffling (drawdown confidence
  interval), deflated Sharpe ratio.
- **Deferred:** per-regime, per-sector and parameter-sensitivity breakdowns. These
  need real data and a sector map; building them against synthetic bars would
  produce charts that look rigorous and mean nothing.

---

## 7. Bugs found during development

A record, because the pattern is informative — every one was caught by a test, and
several by guards written in an earlier phase.

| # | Phase | Bug | Why it mattered |
|---|---|---|---|
| 1 | 1 | `build_cum_close_lookup` used `bisect_right`, picking the ex-date's own (already-adjusted) bar as the cum close | Understated every dividend and demerger adjustment factor |
| 2 | 1 | Aggregator's session rollover reset the volume baseline to 0 unconditionally, clobbering the "baseline unknown" state | Cold starts would claim the whole morning's turnover as one bar |
| 3 | 3 | Bollinger bandwidth returned NaN instead of 0 on a zero-width band | Reused the NaN-guarded denominator; only %B is genuinely undefined there |
| 4 | 3 | RSI returned 100 on a perfectly flat series | Reads as maximally overbought on a stock that has not moved; now NaN |
| 5 | 4 | **Sharpe returned 7.3e16 on a constant return stream** | Zero-variance data has floating-point std ~1e-19, so the `== 0.0` guard never fired and the ratio divided by nothing — producing a number that looks like a spectacular strategy |
| 6 | 4 | Square-off compared a raw wall clock without converting to IST first | Caught by the **Phase 1** timezone AST scanner; a differently-zoned timestamp would square off at the wrong moment |

Bug 6 is the most encouraging: a guard written three phases earlier fired on new
code without anyone remembering it existed.

---

## 8. Test inventory

| File | Tests | Covers |
|---|---:|---|
| `test_trading_calendar.py` | 41 | Sessions, holidays, bar grid, ragged bars, muhurat, session boundaries |
| `test_features_indicators.py` | 41 | Indicator reference values, analytic + independent reimplementation |
| `test_universe.py` | 29 | **Survivorship bias**, point-in-time queries, fail-closed |
| `test_corporate_actions.py` | 23 | Real HDFCBANK 1:2 split round-trip, bonus, dividend, demerger |
| `test_stream.py` | 21 | Backoff, staleness, degradation, idle-when-closed |
| `test_integrity.py` | 21 | Gaps, duplicates, bad bars, missed corporate actions |
| `test_backtest_costs.py` | 21 | Every cost line item, hand-derived |
| `test_brokers.py` | 20 | Adapter contract, **read-only guarantee** |
| `test_backtest_metrics.py` | 20 | Drawdown, Sharpe/Sortino/Calmar, deflated Sharpe, Monte Carlo |
| `test_features_session.py` | 19 | Session resets, U-shaped volume problem |
| `test_backtest_engine.py` | 18 | Fail-closed, point-in-time gating, fills, settlement, square-off |
| `test_aggregator.py` | 18 | Session boundaries, cumulative volume, ragged bars |
| `test_store.py` | 15 | Round trip, upsert, partitioning, resume |
| `test_lookahead.py` | 12 | **No feature sees the future**, MTF alignment |
| `test_config.py` | 11 | Validation, regulatory posture |
| `test_backfill.py` | 11 | Chunk planning, resume, error handling |
| `test_timezone_discipline.py` | 10 | Runtime rejection + static AST scan |
| `test_pipeline_integration.py` | 4 | End-to-end ingest cycle |
| **Total** | **355** | |

---

## 9. Blockers and data provenance

Three data inputs are missing. Each blocks something different.

### 1. Point-in-time constituents — the hard blocker

**Status:** file ships empty; the backtest refuses to start.
**Why:** no broker API serves historical index membership — not Kite, not Fyers,
not any free tier.
**Source:** NSE Indices reconstitution press releases
(<https://www.niftyindices.com> → Media / Press Releases), published ~4 weeks
before each semi-annual change. Changes are effective from the last trading day of
March and September.
**Effort:** ~4–8 changes per year over 2019–2026 — a couple of hours of careful
transcription, not a research project.
**Validate with:** `python -m nifty50.scripts.verify_universe --start 2019-01-01 --end 2026-12-31`

### 2. Broker credentials

**Status:** everything runs on the replay adapter.
**Unblocks:** real bars, live streaming, the actual data-quality picture.
**Note:** Kite access tokens expire daily (~06:00 IST) with no refresh token — the
OAuth redirect requires a human every trading morning. This is a vendor property,
not an implementation gap.

### 3. Reference-data verification

| File | Status |
|---|---|
| `nse_holidays.csv` | Seeded 2019–2025 from recall. **2026 deliberately incomplete** — fixed-date national holidays only |
| `nse_special_sessions.csv` | Muhurat 2019–2025, seeded; times especially need verification |
| `corporate_actions.csv` | **5 rows** against a real need for dozens |

Two automated defences exist because of this:
- `find_suspected_unadjusted_actions` flags any unexplained overnight move beyond a
  configured threshold.
- `verify_calendar` cross-checks the holiday file against ingested bars in **both**
  directions — phantom holidays (bars exist on a supposed holiday) and missing
  holidays (no symbol traded on a supposed trading day).

The cost rates in `config.yaml` carry an `effective_from` date and **must be
verified against your own broker's contract note**. Applying 2026 rates to 2019
fills is its own species of look-ahead.

---

## 10. What is not built

| Component | Directory | Phase |
|---|---|---|
| ML models, triple-barrier labelling, purged walk-forward CV | `models/` (empty) | 5 |
| Statistical layer (Hurst, OU half-life, ADF, beta/residual, cointegration) | — | 5 |
| India-flow features (delivery %, FII/DII, OI quadrant, PCR, futures basis) | — | 5 |
| Bhavcopy ingestion | — | 5 |
| ASM/GSM and circuit-proximity flags | — | 5 |
| Decision engine, fusion, suppression rules, regime gating | `signals/` (empty) | 6 |
| Dashboard (FastAPI + WebSocket, heatmap, charts, health panel) | `dashboard/` (empty) | 7 |
| Alerts (desktop, console, Telegram, webhook) | `alerts/` (empty) | 7 |
| `docker-compose.yml`, architecture diagram, final docs pass | — | 8 |

Also not implemented within completed phases:
- REST fallback is a single-poll primitive (`poll_rest_once`), not wired into an
  automatic polling loop.
- Order-book imbalance is NaN in any backtest — depth is a live-only field, absent
  from every historical bar feed. This is by construction, not omission.
- Only Kite and replay adapters exist. Upstox, Angel One, Dhan and Fyers are
  config-shaped but unwritten.

---

## 11. Critical path

```
   [ NSE constituents transcription ]  ← YOU
                  │
                  ▼
   [ Phase 4 re-run on real data ]  ← first honest number the project produces
                  │
                  ▼
   [ Phase 6 decision engine ]  ← weights are meant to be justified against
                  │                measured performance
                  ▼
   [ Phase 7 dashboard + alerts ]
```

Phase 5 (statistical + India-flow + ML) can be built in parallel — it needs the
constituents file *validated*, not *written* — but every result it produces stays
provisional until the blocker clears.

### The risk worth naming

The code is well-tested and the guards keep catching real problems. The risk is
that **phases 5–7 add a large amount of surface area on top of data that still is
not real.** An ML layer trained on synthetic bars, a decision engine fusing
untested signals, and a dashboard displaying both is three phases of work whose
output cannot be validated until the constituents file exists.

---

## 12. Standing constraints

**Alert-only, read-only.** There is no execution module and there must not be one.
The posture is enforced structurally (see §3). Adding order placement brings the
strategy inside SEBI's algo-registration perimeter: >10 orders/second per exchange
requires exchange registration through the broker with a unique algo ID; only a
static registered IP may send API orders with OAuth and 2FA; the broker is the
principal and you cannot connect directly to an exchange; distributing a black-box
strategy requires SEBI Research Analyst registration.

**Not investment advice.** This tool generates signals. Backtested performance does
not predict future returns. After the full Indian cost stack most retail systematic
strategies underperform a plain Nifty index fund. Redistributing its signals to
other people may trigger SEBI RA/IA registration requirements.

**Repository containment.** The engine lives entirely under `nifty50-signal-engine/`
and shares no code with `fincheck/` — no imports in either direction. `main` is
untouched. PR #5 was closed rather than merged.

---

## 13. What would break this

- **Index rebalancing** — semi-annual reconstitution; without the point-in-time
  table every historical result is survivorship-biased.
- **Regulatory change** — STT, stamp duty, exchange charges and lot sizes all move
  by circular. The cost stack is dated for this reason.
- **Broker API deprecation and daily token expiry** — an unmanned overnight run
  finds a dead session at 09:15.
- **Circuit halts** — a frozen stock cannot be filled through. Fills assumed through
  a circuit limit are one of the four standard causes of a fake-winning Indian
  backtest.
- **Results-season volatility** — earnings gaps dominate short-horizon features and
  are not tradeable in the way a backtest implies.
- **Regime** — seven years of a broadly rising Indian market has never seen a
  sustained bear. Assume out-of-sample performance is worse than anything measured.
