# signaltrader — chat signals → (paper) exchange orders

`signaltrader` reads trading signals from **Telegram**, parses the free-text
into a structured order, runs it through a **risk layer**, and routes it to an
exchange adapter (**Binance**, with **CoinDCX** / **Zerodha** scaffolds and a
built-in **paper broker**).

```
Telegram message ──▶ parse_signal ──▶ risk / position-sizing ──▶ Broker.place_order
   "BUY BTCUSDT @ 65000                                            (paper / testnet
    SL 64000  TP 66000"                                             / live)
```

> ⚠️ **Money warning.** Automated trading can lose real money fast. This tool
> ships in **dry-run / paper mode** and that is the only mode that is
> exhaustively tested. Live trading exists behind an explicit opt-in. Test on
> paper and on exchange testnets for a long time before risking real funds, and
> never trade money you can't afford to lose. **No tool can verify that the
> person posting a signal is honest or competent — you are responsible for
> every order placed on your behalf.**

## Why paper-first

The valuable, deterministic part of a signal bot is everything *except* the
final API call: reliably parsing messy signals, sizing positions sanely, and
enforcing limits. All of that runs with **zero credentials and no network**, so
you can develop and trust it before any money is involved.

| Platform | Status | Safe test path |
|----------|--------|----------------|
| Paper (built-in) | ✅ full, default | pure simulation, no keys |
| Binance | ✅ implemented | **testnet** when `dry_run: true` |
| CoinDCX | 🟡 scaffold | dry-run returns simulated fills (no sandbox exists) |
| Zerodha (Kite) | 🟡 scaffold | dry-run returns simulated fills (no sandbox exists) |

## Install

```bash
pip install -r requirements.txt   # integrations are optional; see the file
```

The core (parser, risk, paper broker, CLI) needs **no third-party packages**.
Install `telethon` only for Telegram, `python-binance` only for Binance, etc.

## Try it without any accounts

```bash
# See how a message parses
python -m signaltrader parse "BUY BTCUSDT @ 65000
SL 64000
TP 66000 67000"

# Run messages through the paper engine and watch the simulated fills
python -m signaltrader simulate \
  "BUY BTCUSDT @ 65000, SL 64000, TP 66000" \
  "#ETH LONG entry 3200-3250 sl 3100 tp 3400" \
  -c config.example.yaml
```

## Use it as a library

```python
from signaltrader import TradingEngine, PaperBroker, RiskConfig

engine = TradingEngine(
    PaperBroker(starting_cash=10_000, prices={"BTCUSDT": 65000}),
    risk=RiskConfig(risk_per_trade=0.02, max_notional=1_000),
)
outcome = engine.handle_message("BUY BTCUSDT market\nSL 64000\nTP 67000")
print(outcome.result)   # OrderResult(accepted=True, dry_run=True, ...)
```

## Live(-ish) listening over Telegram

1. Get `api_id` / `api_hash` from <https://my.telegram.org>.
2. Copy the config and set your secrets in the environment:

   ```bash
   cp config.example.yaml config.yaml
   export TELEGRAM_API_ID=123456
   export TELEGRAM_API_HASH=your-hash
   # for a real exchange (kept on testnet while dry_run: true):
   export EXCHANGE_API_KEY=...    EXCHANGE_API_SECRET=...
   ```

3. Edit `config.yaml` — choose `broker`, set `risk` limits and the `chats` to
   watch. **Leave `dry_run: true`.**
4. Run:

   ```bash
   python -m signaltrader listen -c config.yaml
   ```

Every incoming message is parsed; recognised signals are sized and sent to the
broker. With `dry_run: true` the Binance adapter uses the **testnet** and the
CoinDCX/Zerodha adapters return simulated fills — no real orders.

### Going live (only when you mean it)

Set `dry_run: false` (or `export SIGNALTRADER_LIVE=1`) **and** provide real
credentials. The CLI prints a loud warning when live. Zerodha additionally
needs a fresh `EXCHANGE_ACCESS_TOKEN` from its daily login flow.

## How parsing works

`parse_signal` recognises the common building blocks across signal groups —
side (`buy/sell/long/short`), symbol (`BTCUSDT`, `BTC/USDT`, `#ETH`), entry
price or zone, `SL`, one or more `TP`/targets, and `10x` leverage. A message
without a confident side **and** symbol returns `None`, so ordinary chatter is
ignored. See `tests/test_parser.py` for the formats covered. Real channels vary
wildly — treat the parser as a starting point and add cases for your sources.

## Risk layer

`RiskConfig` + `size_order` decide *how much* to trade and reject bad signals:

- `risk_per_trade` — equity fraction per trade; with a stop-loss it sizes so a
  stop-out loses about that fraction, otherwise it allocates that notional.
- `max_notional` — hard cap on any single order.
- `max_leverage` — caps leverage requested by a signal.
- `symbol_allowlist` — when set, only these symbols trade.
- `require_stop_loss` — drop signals with no SL.

## Architecture

```
signaltrader/
  signals/   parser.py, models.py   — text ─▶ Signal
  risk.py                            — Signal ─▶ sized OrderRequest (+ limits)
  brokers/   base, paper, binance, coindcx, zerodha, factory
  sources/   base, telegram_source  — chat ─▶ message text
  engine.py                          — wires source ─▶ parser ─▶ risk ─▶ broker
  cli.py                             — parse / simulate / listen
```

Adding an exchange = implement `Broker` (`place_order`, `get_price`) and
register it in `brokers/__init__.py`. Adding WhatsApp = implement `SignalSource`
(e.g. via the WhatsApp Business Cloud API) and feed `engine.handle_message`.

## Tests

```bash
cd signal-trader && python -m pytest -q
```

## Disclaimer

This is software for educational and personal-automation use. It is **not**
financial advice, and it comes with no warranty. You are solely responsible for
complying with the terms of service of any exchange or messaging platform you
connect it to, and for any trades it places.
