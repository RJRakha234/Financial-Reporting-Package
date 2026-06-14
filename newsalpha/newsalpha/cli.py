"""Command-line interface: ``python -m newsalpha AAPL NVDA TSLA``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from . import select
from .feeds import Article
from .report import detail, to_console, to_json
from .universe import DEFAULT_WATCHLIST


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="newsalpha",
        description="Rank a watchlist by real-time news sentiment to surface "
        "the strongest and weakest stories right now.",
        epilog="Decision-support only — not financial advice, no profit "
        "guarantee. Manage your risk.",
    )
    p.add_argument("tickers", nargs="*",
                   help=f"symbols to screen (default: {' '.join(DEFAULT_WATCHLIST)})")
    p.add_argument("--top", type=int, default=None,
                   help="show only the top N rows")
    p.add_argument("--since", type=float, default=48.0, metavar="HOURS",
                   help="ignore news older than this many hours (default: 48)")
    p.add_argument("--half-life", type=float, default=12.0, metavar="HOURS",
                   help="recency decay half-life (default: 12)")
    p.add_argument("--min-articles", type=int, default=1,
                   help="minimum fresh articles for a ticker to be scored")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="per-request network timeout in seconds")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a board")
    p.add_argument("--detail", action="store_true",
                   help="print a per-ticker headline breakdown")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    p.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                   help="live mode: refresh every N seconds until Ctrl-C")
    p.add_argument("--offline", metavar="PATH",
                   help="score articles from a local JSON file instead of the "
                   "network (see make_sample.py)")
    return p


def _load_offline(path: str) -> list[Article]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data["articles"] if isinstance(data, dict) else data
    out = []
    for d in items:
        pub = d.get("published")
        published = None
        if pub:
            try:
                published = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        out.append(Article(
            ticker=d["ticker"].upper(), title=d.get("title", ""),
            summary=d.get("summary", ""), source=d.get("source", ""),
            url=d.get("url", ""), published=published,
        ))
    return out


def _render(args, color: bool) -> str:
    offline_articles = _load_offline(args.offline) if args.offline else None
    board = select(
        args.tickers or None,
        since_hours=args.since,
        half_life_hours=args.half_life,
        min_articles=args.min_articles,
        timeout=args.timeout,
        articles=offline_articles,
    )
    rows = board.ranked[: args.top] if args.top else board.ranked
    if args.json:
        return to_json(rows)
    parts = [to_console(rows, color=color, limit=args.top)]
    if args.detail:
        parts.append("")
        parts.extend(detail(s) for s in rows)
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    color = (not args.no_color) and sys.stdout.isatty()

    if args.watch and args.watch > 0 and not args.json:
        try:
            while True:
                sys.stdout.write("\033[2J\033[H" if color else "\n" * 2)
                print(_render(args, color))
                print(f"\n↻ refreshing every {args.watch:g}s — Ctrl-C to stop")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0

    print(_render(args, color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
