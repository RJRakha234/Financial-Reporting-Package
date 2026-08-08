"""Run the live dashboard.

    python -m nifty50.scripts.dashboard --symbols RELIANCE,TCS,HDFCBANK
    python -m nifty50.scripts.dashboard --replay          # no credentials needed

Run it from the ``nifty50-signal-engine`` directory, with the package
installed (``pip install -e .``). ``python -m`` resolves modules from the
current directory, so invoking it from the repository root -- where ``src/``
is one level down -- fails with ``No module named 'nifty50'``.

The Kite path needs ``KITE_API_KEY`` and ``KITE_API_SECRET`` in ``.env``, plus a
``KITE_REQUEST_TOKEN`` pasted from the login redirect. Access tokens expire
around 06:00 IST and Kite issues no refresh token, so that paste is a daily
manual step -- the engine persists the resulting access token itself.

``--replay`` runs the whole stack against stored bars: real tick synthesis,
the real aggregator, the real bar boundaries. Use it to check the dashboard
works before market open, and to develop against on a weekend. It needs a
replay root, which :mod:`nifty50.scripts.build_replay` produces from a
directory of downloaded vendor files. It never reports itself as LIVE.

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
from collections.abc import Callable

from nifty50.backtest.costs import CostModel, TradeStyle, breakeven_move_pct
from nifty50.config import Config, load_config
from nifty50.dashboard.server import serve_in_background
from nifty50.dashboard.state import DashboardState, RiskContext
from nifty50.data.aggregator import CandleAggregator
from nifty50.data.brokers.base import BrokerAdapter, StreamMode
from nifty50.data.stream import StreamHealth, StreamSupervisor
from nifty50.domain import Exchange, Instrument, Timeframe, now_ist
from nifty50.logging_setup import configure_logging, get_logger
from nifty50.trading_calendar import TradingCalendar

log = get_logger(__name__)

# How often the health panel and the forming bar are refreshed. The stream
# itself is event-driven; this only governs what the browser can see, and a
# second is far finer than any bar this engine builds.
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
    parser.add_argument(
        "--replay-sessions", type=int, default=30,
        help="how many of the most recent stored sessions to replay (0 = all)",
    )
    parser.add_argument(
        "--replay-speed", type=float, default=0.02,
        help="seconds between replayed ticks; 0 replays as fast as possible",
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

    try:
        adapter = _adapter(config, replay=args.replay)
        # One aggregator per timeframe; each builds bars for every subscribed
        # instrument. Candles are routed by whatever drives the aggregators,
        # not by a callback here, so a bar is delivered exactly once.
        aggregators = [
            CandleAggregator(calendar, timeframe) for timeframe in timeframes
        ]
        instruments = [_resolve(adapter, symbol) for symbol in symbols]
    except Exception as error:
        log.exception("dashboard_setup_failed")
        print(f"\n{error}", file=sys.stderr)
        server.shutdown()
        return 1

    try:
        if args.replay:
            asyncio.run(_drive_replay(adapter, aggregators, state, instruments, args))
        else:
            asyncio.run(_drive_live(adapter, calendar, config, aggregators, state, instruments))
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


def _resolve(adapter: BrokerAdapter, symbol: str) -> Instrument:
    """Find a symbol on either segment.

    Cash equities live on NSE and derivatives on NFO, and the caller should not
    have to know which -- ``NIFTY26OCTFUT`` and ``RELIANCE`` are both just
    symbols to whoever is typing the command.
    """
    errors: list[str] = []
    for exchange in (Exchange.NSE, Exchange.NFO):
        try:
            return adapter.resolve(symbol, exchange)
        except Exception as error:
            errors.append(f"{exchange.value}: {error}")
    raise KeyError(f"could not resolve {symbol!r} -- " + "; ".join(errors))


async def _drive_live(
    adapter: BrokerAdapter,
    calendar: TradingCalendar,
    config: Config,
    aggregators: list[CandleAggregator],
    state: DashboardState,
    instruments: list[Instrument],
) -> None:
    """Run the supervisor and mirror its state into the dashboard.

    The supervisor owns the event loop's real work; this only copies health and
    the in-progress bar across on a timer so the browser has something to poll.
    Ctrl-C sets the stop event rather than killing the loop mid-write, so the
    websocket is closed cleanly and the last partial bar is flushed.
    """
    supervisor = StreamSupervisor(
        adapter, calendar, config, aggregators=aggregators, on_candle=state.on_candle
    )
    supervisor.subscribe(instruments, StreamMode.FULL)
    _install_sigint(supervisor.request_stop)

    mirror = asyncio.create_task(
        _mirror(state, aggregators, instruments, lambda: supervisor.health)
    )
    try:
        await supervisor.run()
    finally:
        await _cancel(mirror)
        await supervisor.shutdown()
        state.set_health(supervisor.health)


async def _drive_replay(
    adapter: BrokerAdapter,
    aggregators: list[CandleAggregator],
    state: DashboardState,
    instruments: list[Instrument],
    args: argparse.Namespace,
) -> None:
    """Replay stored bars through the same aggregators the live path uses."""
    from nifty50.dashboard.replay_driver import ReplayDriver
    from nifty50.data.brokers.replay import ReplayAdapter

    assert isinstance(adapter, ReplayAdapter)
    driver = ReplayDriver(
        adapter=adapter,
        aggregators=aggregators,
        state=state,
        instruments=instruments,
        seconds_per_tick=max(0.0, args.replay_speed),
        max_sessions=args.replay_sessions or None,
    )
    source = driver.source_timeframe()
    sessions = driver.sessions(source)
    print(f"replaying   {len(sessions)} session(s) from stored {source.value} bars")
    if sessions:
        print(f"            {sessions[0]} .. {sessions[-1]}\n")

    _install_sigint(driver.request_stop)
    await driver.run()
    print("\nreplay complete. The dashboard stays up; Ctrl-C to stop.")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()


async def _mirror(
    state: DashboardState,
    aggregators: list[CandleAggregator],
    instruments: list[Instrument],
    read_health: Callable[[], StreamHealth],
) -> None:
    """Push health and the unclosed bar into the dashboard on a timer.

    The supervisor emits candles only when they close, so without this the
    forming bar -- the whole point of drawing it separately -- would never
    reach the browser until the interval was already over.
    """
    while True:
        state.set_health(read_health())
        for aggregator in aggregators:
            for instrument in instruments:
                forming = aggregator.in_progress(instrument.key)
                if forming is not None:
                    state.on_candle(forming)
        await asyncio.sleep(_HEALTH_REFRESH_SECONDS)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _install_sigint(handler: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        # Not available on Windows' Proactor loop; the KeyboardInterrupt path
        # in main() covers that case.
        loop.add_signal_handler(signal.SIGINT, handler)


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

        adapter = ReplayAdapter(config)
        root = config.path(config.broker.replay.root)
        if not root.is_dir():
            raise FileNotFoundError(
                f"no replay data at {root}. Build it from your downloads first:\n"
                f"  python -m nifty50.scripts.build_replay --source <download-dir>"
            )
        adapter.authenticate()
        return adapter
    from nifty50.data.brokers.kite import KiteAdapter

    kite = KiteAdapter(config)
    status = kite.authenticate()
    remaining = (status.expires_at - now_ist()).total_seconds() if status.expires_at else 0.0
    print(f"kite token valid for {remaining / 3600:.1f}h")
    if remaining < 3600:
        print(
            "WARNING: the token expires within the hour. Kite issues no refresh "
            "token -- re-run the login before the session starts.",
            file=sys.stderr,
        )
    return kite


if __name__ == "__main__":
    raise SystemExit(main())
