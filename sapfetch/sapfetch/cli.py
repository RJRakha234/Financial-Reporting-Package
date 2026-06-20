"""Command-line interface: ``python -m sapfetch ...``.

Two subcommands:

    sapfetch login   --config config.yaml
        Open a browser, complete SSO once, and save the session.

    sapfetch download --config config.yaml --fiscal-year 2025 \
                      --from-period 1 --to-period 10 [--consol-group G_GRUP] \
                      [--report "GR INDAS Consolidated PL" ...] [--show-browser]
        Pull every configured report (or just the named ones) for the period.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import load
from .downloader import download_reports
from .errors import SapfetchError
from .period import Period
from .session import capture_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sapfetch",
        description="Automate downloading SAP BI / Group Reporting reports "
        "from the Enterprise Portal for a chosen period.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-c", "--config", default="config.yaml",
        help="path to the YAML config (default: config.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="capture an SSO session in a visible browser")

    dl = sub.add_parser("download", help="download reports for a period")
    dl.add_argument("--fiscal-year", type=int, required=True,
                    help="e.g. 2025")
    dl.add_argument("--from-period", type=int, required=True,
                    help="first posting period (1-16)")
    dl.add_argument("--to-period", type=int, default=None,
                    help="last posting period, inclusive (default: from-period)")
    dl.add_argument("--consol-group", default=None,
                    help="consolidation group key (overrides config default)")
    dl.add_argument("--report", action="append", dest="reports", metavar="NAME",
                    help="only download this report (repeatable; "
                    "default: all reports in the config)")
    dl.add_argument("--show-browser", action="store_true",
                    help="run with a visible browser (debugging)")
    return parser


def _cmd_login(config) -> int:
    path = capture_session(config.portal)
    print(f"Session saved to {path}. You can now run `sapfetch download`.")
    return 0


def _cmd_download(config, args) -> int:
    if args.show_browser:
        config.portal.headless = False
    period = Period(
        fiscal_year=args.fiscal_year,
        from_period=args.from_period,
        to_period=args.to_period,
        consol_group=args.consol_group,
    )
    print(f"Downloading {len(config.reports)} report(s) for {period.label} ...")
    results = download_reports(config, period, only=args.reports)

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    for r in results:
        if r.ok:
            print(f"  ✓ {r.report}  ->  {r.path}")
        else:
            print(f"  ✗ {r.report}  ({r.error})", file=sys.stderr)

    print(f"\nDone: {len(ok)} succeeded, {len(failed)} failed.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load(args.config)
        if args.command == "login":
            return _cmd_login(config)
        if args.command == "download":
            return _cmd_download(config, args)
    except SapfetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
