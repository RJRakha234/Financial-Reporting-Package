"""Run the live dashboard.

    python -m nifty50.scripts.dashboard --symbols RELIANCE,TCS,HDFCBANK
    python -m nifty50.scripts.dashboard --replay          # no credentials needed

The Kite path needs ``KITE_API_KEY`` and ``KITE_API_SECRET`` in ``.env``, plus a
``KITE_REQUEST_TOKEN`` pasted from the login redirect. Access tokens expire
around 06:00 IST and Kite issues no refresh token, so that paste is a daily
manual step — the engine persists the resulting access token itself.

``--replay`` runs the whole stack against stored bars. Use it to check the
dashboard works before market open, and to develop against on a weekend.

Ctrl-C shuts the stream down cleanly. Nothing here can place an order: the
adapter base class rejects any subclass exposing an order method, and the
server handles no verb but GET.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import webbrowser

from nifty50.backtest.costs import CostModel, TradeStyle, breakeven_move_pct
from nifty50.config import Config, load_config
from nifty50.dashboard.server import serve_in_background
from nifty50.dashboard.state import DashboardState, RiskContext
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.base import BrokerAdapter, StreamMode
from nifty50.data.stream import StreamSupervisor
from nifty50.domain import Exchange, Timeframe, now_ist
from nifty50.logging_setup import configure_logging, get_logger
from nifty50.trading_calendar import TradingCalendar

log = get_logger(__name__)

# How often the health panel is refreshed from the supervisor. The stream
# itself is event-driven; this only governs what the browser can see.
_HEALTH_REFRESH_SECONDS: float = 1.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols", default="RELIANCE",
        help="comma-separated NSE symbols to stream",
    )
    parser.add_argument(
        "--timeframes", default="15m",
        help="comma-separated bar sizes to aggregate (1m,5m,15m,30m,1h)",
    )
    parser.add_argument(
        "--replay", action="store_true",
        help="use stored bars instead of a live broker connection",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config)
    calendar = TradingCalendar.from_config(config)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    try:
        timeframes = [Timeframe(t.strip()) for t in args.timeframes.split(",") if t.strip()]
    except ValueError as error:
        print(f"unknown timeframe: {error}", file=sys.stderr)
        return 2
    if not symbols or not timeframes:
        print("need at least one symbol and one timeframe", file=sys.stderr)
        return 2

    state = DashboardState(risk=_risk_context(config))
    host = args.host or config.dashboard.host
    port = args.port or config.dashboard.port
    server, _thread = serve_in_background(state, host=host, port=port)
    url = f"http://{host}:{port}"

    print(f"dashboard   {url}")
    print(f"symbols     {', '.join(symbols)}")
    print(f"timeframes  {', '.join(t.value for t in timeframes)}")
    print(f"mode        {'REPLAY (stored bars)' if args.replay else 'LIVE (Kite)'}")
    print(f"session     {calendar.phase(now_ist()).value}")
    print("\nAlert-only and read-only. Ctrl-C to stop.\n")
    if not args.no_browser:
        # Headless boxes have no browser; that is not a failure.
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    adapter = _adapter(config, replay=args.replay)
    # One aggregator per timeframe; each builds bars for every subscribed
    # instrument and hands closed ones straight to the dashboard.
    aggregators = [
        CandleAggregator(calendar, timeframe, on_candle=state.on_candle)
        for timeframe in timeframes
    ]
    supervisor = StreamSupervisor(
        adapter,
        calendar,
        config,
        aggregators=aggregators,
        on_candle=state.on_candle,
    )

    try:
        instruments = [adapter.resolve(symbol, Exchange.NSE) for symbol in symbols]
        supervisor.subscribe(instruments, StreamMode.FULL)
    except Exception as error:
        log.exception("dashboard_subscribe_failed")
        print(f"\ncould not subscribe: {error}", file=sys.stderr)
        server.shutdown()
        return 1

    try:
        asyncio.run(_drive(supervisor, state))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log.exception("dashboard_stream_failed")
        print(f"\nstream failed: {error}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        print("\nstopped.")
    return 0


async def _drive(supervisor: StreamSupervisor, state: DashboardState) -> None:
    """Run the supervisor and mirror its health into the dashboard.

    The supervisor owns the event loop's real work; this only copies health
    across on a timer so the browser has something to poll. Ctrl-C sets the
    stop event rather than killing the loop mid-write, so the websocket is
    closed cleanly and the last partial bar is flushed.
    """
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        # Not available on Windows' Proactor loop; the KeyboardInterrupt path
        # in main() covers that case.
        loop.add_signal_handler(signal.SIGINT, supervisor.request_stop)

    async def mirror_health() -> None:
        while True:
            state.set_health(supervisor.health)
            await asyncio.sleep(_HEALTH_REFRESH_SECONDS)

    mirror = asyncio.create_task(mirror_health())
    try:
        await supervisor.run()
    finally:
        mirror.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mirror
        await supervisor.shutdown()
        state.set_health(supervisor.health)


def _risk_context(config: Config) -> RiskContext:
    """Cost hurdle computed from the real cost stack, not a guess."""
    model = CostModel(config.costs, TradeStyle.INTRADAY)
    reference_price = 1000.0
    quantity = int(100_000 / reference_price)
    return RiskContext(
        capital_inr=config.capital.starting_capital_inr,
        risk_fraction=config.capital.risk_per_trade_fraction,
        breakeven_pct=breakeven_move_pct(model, reference_price, quantity),
    )


def _adapter(config: Config, *, replay: bool) -> BrokerAdapter:
    if replay:
        from nifty50.data.brokers.replay import ReplayAdapter

        return ReplayAdapter(config)
    from nifty50.data.brokers.kite import KiteAdapter

    adapter = KiteAdapter(config)
    status = adapter.authenticate()
    remaining = (status.expires_at - now_ist()).total_seconds() if status.expires_at else 0.0
    print(f"kite token valid for {remaining / 3600:.1f}h")
    if remaining < 3600:
        print(
            "WARNING: the token expires within the hour. Kite issues no refresh "
            "token -- re-run the login before the session starts.",
            file=sys.stderr,
        )
    return adapter


if __name__ == "__main__":
    raise SystemExit(main())
