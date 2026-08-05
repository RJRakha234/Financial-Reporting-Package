# Crypto Signal Engine

An **alert-only** market-data and signal-generation engine for Binance spot pairs.

> **This tool generates signals, not financial advice.** It never places, cancels,
> or modifies an order, and no code path exists that could. Backtest results do
> not predict future returns. Most retail systematic strategies lose money after
> costs. See [Limitations](#limitations-and-what-would-break-this).

---

## ⚠️ Read this first: the data in this repository is SYNTHETIC

The build environment for this project **has no network route to any market-data
provider**. `api.binance.com` and the `data-api.binance.vision` mirror are refused
at the egress proxy with `403 Forbidden` on CONNECT — an organisation
network-policy denial, not an outage:

```
$ python -m cse.cli check-connectivity
BLOCKED BY NETWORK POLICY  https://api.binance.com
    Outbound access to https://api.binance.com is blocked by network policy
    (403 Forbidden). This is not a transient failure and will not be retried.
```

This is a strict allowlist, not a Binance-specific block. Twelve candidate hosts
were probed; every one was refused at CONNECT:

```
www.tradingview.com  scanner.tradingview.com  api.coingecko.com
api.kraken.com       api.exchange.coinbase.com  api.bybit.com
www.okx.com          min-api.cryptocompare.com  www.bitstamp.net
api.gemini.com       api.binance.us             query1.finance.yahoo.com
```

**Why not TradingView?** Beyond being blocked here, it is the wrong foundation
even with network access. TradingView is a *display* layer — for Binance pairs it
is re-rendering Binance's own data, so Binance remains the authoritative source.
It publishes no documented historical-OHLCV API; its charts are fed by a private,
undocumented WebSocket protocol that changes without notice, and automated
extraction is contrary to its Terms of Service. Decisively, the spec's order-flow
requirements — `aggTrade` aggressor imbalance and top-20 order-book depth — do not
exist in a chart feed at all, so the volume/flow feature group could not be built
from it regardless.

**The fix is a network-policy change, not a different provider:** allow
`api.binance.com` and `stream.binance.com`, then set `data.mode: live`. See the
[Claude Code on the web docs](https://code.claude.com/docs/en/claude-code-on-the-web)
for how an environment's network policy is configured.

Consequences you must understand before reading any number this project produces:

| | Status |
|---|---|
| Live Binance REST/WebSocket client | **Written and unit-tested against mocked HTTP. Never executed against the real exchange.** |
| Historical candles in `data_store/` | **Generated, not real.** Regime-switching GBM with a common market factor. |
| Any backtest result computed here | **Evidence about plumbing only.** Zero evidence about profitability. |

Every dataset carries a `provenance` tag (`cse/data/source.py`). Synthetic data is
stamped `real_market_data: false`, and that tag is designed to be printed on every
downstream report so a generated result can never be mistaken for a real one.

**To get real results:** allow egress to `api.binance.com`, set `data.mode: live`
in `config.yaml`, and re-run the backfill. The code path is identical — only the
bytes change.

---

## Quick start

```bash
pip install -e ".[dev]"          # add [ml] and [dashboard] for later phases
cp .env.example .env             # optional; no API key is needed for market data

python -m cse.cli check-connectivity   # can we reach Binance?
python -m cse.cli backfill             # populate the local candle archive
python -m cse.cli verify               # integrity report over everything stored
python -m cse.cli stats                # coverage per symbol/timeframe
```

Exit codes: `0` success, `1` failure, `2` egress blocked by network policy.

## Configuration

All parameters live in [`config.yaml`](config.yaml) — there are no magic numbers
in the code. The file is validated by pydantic at startup (`cse/config.py`), with
`extra="forbid"`, so a typo'd key is a startup error rather than a silently
ignored setting.

| Key | Meaning |
|---|---|
| `symbols`, `timeframes` | Pairs and candle intervals to track. Timeframes are sorted by duration automatically. |
| `base_timeframe` | Primary decision timeframe. Must appear in `timeframes`. |
| `history_days` | Backfill lookback (1095 = 3 years). |
| `capital_model_usdt`, `risk_per_trade_pct` | Position-sizing inputs for the backtest. |
| `data.mode` | `live` (Binance) · `replay` (stored candles) · `synthetic` (generated, offline). |
| `data.intrabar` | `false` (default) means **no signal is ever computed from an unclosed candle**. |
| `data.rate_limit.*` | Weight budget and thresholds; cross-checked against the exchange's advertised limit at startup. |
| `data.integrity.*` | Tolerances for OHLC validation and live-vs-REST reconciliation. |

Secrets go in `.env` (gitignored), never in `config.yaml`. See `.env.example`.

## Alert-only guarantee

This is enforced structurally, not by convention:

- `cse/data/rest.py` defines `PUBLIC_PATHS`, an allowlist of eight public
  market-data endpoints. `_request` raises `ForbiddenEndpointError` for anything
  else, and only ever issues `GET`.
- No signing, no `X-MBX-APIKEY` header on any request, no order endpoints anywhere
  in the package.
- A test (`test_trading_endpoints_are_structurally_unreachable`) asserts that
  `/api/v3/order` is rejected.

No API key is required at all. If you supply one to raise your rate-limit tier,
it must be **read-only**.

## Architecture (Phase 1)

```
                    ┌─────────────────────────────────────────┐
   Binance public   │  cse/data/rest.py      REST client      │
   REST  ──────────►│    • PUBLIC_PATHS allowlist (GET only)  │
                    │    • adaptive weight limiting           │
                    │    • fail-fast on proxy policy denial   │
                    │    • failover to binance.vision mirror  │
                    └──────────────┬──────────────────────────┘
                                   │
   Binance          ┌──────────────▼──────────────────────────┐
   WebSocket ──────►│  cse/data/ws.py        Stream manager   │
   kline/aggTrade/  │    • reconnect w/ exponential backoff   │
   depth20/ticker   │    • app-level staleness detection      │
                    │    • REST fallback after 30s down       │
                    │    • drops UNCLOSED candles at source   │
                    └──────────────┬──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────┐
                    │  cse/data/integrity.py                  │
                    │    dedupe · sort · align · gap-detect   │
                    │    OHLC validate · WS/REST reconcile    │
                    └──────────────┬──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────┐
                    │  cse/data/store.py     Parquet archive  │
                    │  symbol=/timeframe=/date= (Hive)        │
                    │  atomic per-partition writes · O(1)     │
                    │  resume via last_open_time()            │
                    └──────────────┬──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────┐
                    │  cse/data/source.py                     │
                    │  Live │ Replay │ Synthetic  (+provenance)│
                    └─────────────────────────────────────────┘
```

Phases 2–7 (features, ML, backtest, decision engine, dashboard, alerts) are not
yet built. See the delivery plan at the bottom of this file.

## Data integrity guarantees

The layer that everything downstream depends on. `normalize()` enforces, in order:

1. dtype coercion (Binance sends numbers as JSON strings),
2. NaN row removal,
3. **misalignment removal** — bars whose `open_time` is off the interval grid are
   dropped, because keeping one corrupts every gap calculation downstream,
4. **duplicate removal keeping the LAST copy** — a re-fetched bar is by definition
   more settled than the one already held,
5. sort by `open_time`,
6. malformed-OHLC removal (`high < max(open,close)`, `low > min(open,close)`,
   `high < low`, or any non-positive price),
7. gap detection over what survived.

Every repair is recorded in an `IntegrityReport` and logged — nothing is discarded
silently.

**The unclosed-candle rule.** A forming bar has a moving close; storing one poisons
every feature derived from it, silently, and only for the most recent data — which
is exactly where you would not notice. Three independent defences:

- the WebSocket manager drops `kline` frames with `x == false` unless
  `data.intrabar` is explicitly enabled,
- the backfiller bounds every request at `last_closed_open_time(now)`,
- aggregation emits only buckets holding a complete set of source bars.

## Testing

```bash
python -m pytest -q          # 143 tests
python -m ruff check .       # clean
python -m ruff format --check .
python -m mypy cse tests     # strict, clean
```

No test touches the network: HTTP is mocked with `respx`, and the WebSocket
manager takes an injected `connect_fn` so the reconnect state machine is exercised
without a socket. The rate limiter takes an injected clock, so tests that cover
60-second windows run in microseconds.

### On reproducibility of the synthetic source

The generated series is a pure function of `config.yaml`: the price at a given
timestamp depends only on `synthetic.seed` and `synthetic.start`, never on when
or over what window you ran. Three separate bugs violated this and were caught
only by re-running the backfill at full scale — each one silently rewrote
existing history, and the result still passed every integrity check because it
was gapless with valid OHLC.

If you change the generator, keep these invariants (each has a test):

- **window-length independence** — bar *i* depends only on *i*; extending the
  window must not alter earlier bars,
- **cross-process determinism** — two fresh interpreters must produce identical
  bytes (`hash()` on `str` is randomised per process and will break this),
- **chain continuity after a top-up** — every `open` still equals the previous
  `close`, which is how a spliced realisation reveals itself.

## Limitations and what would break this

Stated plainly, because these matter more than any backtest number:

**Already true of this build**

- **No real market data has ever passed through this code.** Every result here is
  from a generator. The live client is unit-tested against mocked responses, which
  proves it handles the *documented* API — not the real one's quirks.
- The synthetic generator has volatility clustering and cross-asset correlation.
  It does **not** have fat tails, liquidation cascades, funding-rate feedback,
  order-flow reflexivity, news, or realistic microstructure. A strategy tuned on
  it is tuned on nothing.

**Would break the live system**

- **Regime change.** Crypto microstructure shifts materially year over year. A
  model fitted on 2022–2024 is not obviously valid in 2026.
- **Exchange outages and halts.** Detected as gaps and reported as outages rather
  than silently interpolated — but the engine still goes blind during them.
- **Flash crashes.** ATR-based stops assume continuous prices. A 30% wick in one
  bar fills nowhere near the stop level.
- **Delistings and symbol changes.** Not currently handled; a delisted pair simply
  stops producing candles.
- **Fee-tier changes.** Backtest costs are configured, not discovered. If your real
  fee tier differs, every net number is wrong.
- **Clock skew.** Bar alignment assumes your clock is close to the exchange's;
  `check-connectivity` reports the skew.
- **Shared-IP rate limits.** The weight limiter accepts the server's usage figure
  as authoritative precisely because another process on your IP may be spending
  budget you did not.

Fee, slippage, and fill assumptions will be documented here in full when the
backtester lands in Phase 3. None are yet in effect.

## Delivery plan

| Phase | Scope | Status |
|---|---|---|
| 1 | Data layer, storage, integrity tests | **Complete** |
| 2 | Feature/indicator layer + reference tests | Not started |
| 3 | Backtest engine + baseline TA strategy | Not started |
| 4 | Statistical + ML layer + walk-forward validation | Not started |
| 5 | Decision engine + fusion logic | Not started |
| 6 | Dashboard + alerts | Not started |
| 7 | Full documentation pass | Not started |

---

*Signals, not advice. Backtested performance does not predict future returns.*
