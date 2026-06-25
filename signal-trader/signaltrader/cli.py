"""Command-line entry point.

Three subcommands:

* ``parse``  — show how a message would be parsed (no trading).
* ``simulate`` — run one or more messages through the engine against the paper
                 broker and print the resulting fills.
* ``listen`` — connect to Telegram and trade live signals (paper by default).
"""

from __future__ import annotations

import argparse
import logging
import sys

from .brokers import build_broker
from .config import load_config
from .engine import TradingEngine
from .signals.parser import parse_signal


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--config", help="path to a YAML config file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signaltrader",
        description="Turn chat trading signals into (paper) exchange orders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="parse a message and print the signal")
    p_parse.add_argument("message", help="the signal text to parse")
    _add_common(p_parse)

    p_sim = sub.add_parser("simulate", help="run messages through the paper engine")
    p_sim.add_argument("messages", nargs="+", help="one or more signal messages")
    _add_common(p_sim)

    p_listen = sub.add_parser("listen", help="listen to Telegram and trade signals")
    _add_common(p_listen)

    return parser


def _engine_from_config(path: str | None) -> TradingEngine:
    cfg = load_config(path)
    broker = build_broker(cfg.broker, cfg.broker_config, dry_run=cfg.dry_run)
    return TradingEngine(broker, risk=cfg.risk)


def cmd_parse(args: argparse.Namespace) -> int:
    signal = parse_signal(args.message)
    if signal is None:
        print("not recognised as a signal")
        return 1
    print(f"symbol     : {signal.symbol}")
    print(f"side       : {signal.side.value}")
    print(f"order type : {signal.order_type.value}")
    print(f"entry      : {signal.entry_price}")
    print(f"stop loss  : {signal.stop_loss}")
    print(f"targets    : {signal.targets}")
    print(f"leverage   : {signal.leverage}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    engine = _engine_from_config(args.config)
    for msg in args.messages:
        outcome = engine.handle_message(msg)
        if outcome.parsed is None:
            print(f"[skip] {msg!r}: {outcome.skipped_reason}")
            continue
        if outcome.result is None:
            print(f"[skip] {outcome.parsed.symbol}: {outcome.skipped_reason}")
            continue
        r = outcome.result
        status = "OK " if r.accepted else "REJ"
        print(
            f"[{status}] {r.broker} {r.side.value} {r.symbol} "
            f"qty={r.quantity:.6g} @ {r.price} ({r.message})"
        )
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    broker = build_broker(cfg.broker, cfg.broker_config, dry_run=cfg.dry_run)
    engine = TradingEngine(broker, risk=cfg.risk)

    mode = "DRY-RUN" if cfg.dry_run else "*** LIVE TRADING ***"
    print(f"broker={cfg.broker}  mode={mode}")
    if not cfg.dry_run:
        print("WARNING: live trading is enabled. Real orders will be placed.")

    from .sources.telegram_source import TelegramSource

    source = TelegramSource(cfg.telegram)

    def on_message(text: str) -> None:
        outcome = engine.handle_message(text)
        if outcome.result and outcome.result.accepted:
            r = outcome.result
            print(f"-> {r.side.value} {r.symbol} qty={r.quantity:.6g} @ {r.price}")

    source.run(on_message)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return {
        "parse": cmd_parse,
        "simulate": cmd_simulate,
        "listen": cmd_listen,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
