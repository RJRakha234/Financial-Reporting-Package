"""Render ranked signals to a console board or JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .signals import TickerSignal

# ANSI colours; degrade to plain text when output is not a TTY (handled by CLI).
_COLOR = {
    "STRONG BUY": "\033[1;32m",
    "BUY": "\033[32m",
    "WATCH": "\033[33m",
    "NEUTRAL": "\033[90m",
    "AVOID": "\033[31m",
    "STRONG AVOID": "\033[1;31m",
}
_RESET = "\033[0m"


def _bar(score: float) -> str:
    """A little -100..100 gauge."""
    n = int(round(abs(score) / 20))  # 0..5 blocks
    blocks = "█" * n + "·" * (5 - n)
    return ("+" if score >= 0 else "-") + blocks


def to_console(signals: list[TickerSignal], *, color: bool = True,
               limit: int | None = None) -> str:
    rows = signals[:limit] if limit else signals
    if not rows:
        return "No signals — no fresh news matched your tickers."

    out = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"📊 newsalpha — news-driven stock board  ({ts})")
    out.append("=" * 72)
    out.append(f"{'#':>2}  {'TICKER':<7}{'SCORE':>7}  {'GAUGE':<7} "
               f"{'ACTION':<13}{'CONF':>6}  HEADLINE")
    out.append("-" * 72)
    for i, s in enumerate(rows, 1):
        action = s.action
        if color and action in _COLOR:
            action_disp = f"{_COLOR[action]}{action:<13}{_RESET}"
        else:
            action_disp = f"{action:<13}"
        head = (s.latest.title[:42] + "…") if s.latest and len(s.latest.title) > 43 \
            else (s.latest.title if s.latest else "—")
        out.append(
            f"{i:>2}  {s.ticker:<7}{s.score:>7.1f}  {_bar(s.score):<7} "
            f"{action_disp}{s.confidence*100:>5.0f}%  {head}"
        )
    out.append("-" * 72)
    out.append(
        "Score = recency-weighted news sentiment × conviction (±momentum). "
        "Higher = more bullish news flow."
    )
    out.append(
        "⚠️  Decision-support only. News sentiment is one input — not financial "
        "advice and not a profit guarantee. Size positions and manage risk."
    )
    return "\n".join(out)


def detail(signal: TickerSignal) -> str:
    """Verbose breakdown for a single ticker."""
    out = [
        f"{signal.ticker}  score={signal.score:.1f}  action={signal.action}  "
        f"conf={signal.confidence*100:.0f}%",
        f"  {signal.rationale}",
    ]
    for s, title in signal.headlines:
        sign = "▲" if s > 0.05 else "▼" if s < -0.05 else "•"
        out.append(f"  {sign} ({s:+.2f}) {title}")
    return "\n".join(out)


def to_dict(signals: list[TickerSignal]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(signals),
        "signals": [s.as_dict() for s in signals],
    }


def to_json(signals: list[TickerSignal], indent: int = 2) -> str:
    return json.dumps(to_dict(signals), indent=indent)
