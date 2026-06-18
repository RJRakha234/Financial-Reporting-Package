"""CLI:  python -m mailtrigger <command>"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .runner import run_forever, run_once


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mailtrigger",
        description="Run a report when a specific email arrives in Outlook.",
    )
    p.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    p.add_argument(
        "command",
        choices=["watch", "run-once", "check-config"],
        help="watch = poll forever; run-once = single check; "
        "check-config = validate the config and exit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check-config":
        print(f"config OK: watching {cfg['mailbox']['username']} -> {cfg['report'].get('url')}")
        return 0
    if args.command == "run-once":
        n = run_once(cfg)
        print(f"done: {n} report(s) run")
        return 0
    if args.command == "watch":
        run_forever(cfg)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
