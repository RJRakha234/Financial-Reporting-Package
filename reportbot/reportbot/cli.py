"""Command-line interface: ``python -m reportbot <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config
from .runner import Runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reportbot",
        description="Watch an Outlook mailbox and auto-download reports from a "
        "portal when a matching email arrives.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="path to the YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="poll the mailbox on a loop and download reports")
    sub.add_parser(
        "once", help="check the mailbox a single time, then exit"
    )
    sub.add_parser(
        "check",
        help="list recent messages and which trigger rule (if any) they match, "
        "without downloading anything",
    )
    return parser


def _load_config(path: str) -> Config:
    cfg_path = Path(path)
    if not cfg_path.is_file():
        print(f"error: config file not found: {cfg_path}", file=sys.stderr)
        raise SystemExit(2)
    return Config.load(cfg_path)


def _cmd_check(config: Config) -> int:
    from .mail import build_mail_client
    from .triggers import matching_rule

    client = build_mail_client(config.mail)
    messages = client.fetch_recent(
        config.mail.folder, config.mail.lookback_days
    )
    if not messages:
        print("No recent messages in the lookback window.")
        return 0
    print(f"Recent messages ({len(messages)}):")
    for m in messages:
        rule = matching_rule(config.triggers, m)
        tag = f"MATCH:{rule.name}" if rule else "—"
        subject = (m.subject or "(no subject)")[:70]
        print(f"  [{tag:>16}]  {m.sender[:30]:30}  {subject}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = _load_config(args.config)

    if args.command == "check":
        return _cmd_check(config)

    runner = Runner(config)
    if args.command == "once":
        events = runner.run_once()
        print(f"Done. {len(events)} report download(s) triggered.")
        return 0
    if args.command == "run":
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
