"""Parse free-text chat messages into structured :class:`Signal` objects.

Trading-signal channels have no standard format, so the parser is deliberately
forgiving. It recognises the common building blocks that appear across most
crypto/stock signal groups:

    BUY BTCUSDT @ 65000
    SL 64000  TP 66000 67000

    #ETH LONG
    Entry: 3200 - 3250
    Targets: 3300, 3400, 3500
    Stop loss: 3100
    Leverage: 10x

A message is only turned into a signal when both a *side* (buy/sell/long/short)
and a *symbol* can be identified with reasonable confidence. Anything else
returns ``None`` so ordinary chatter is ignored.
"""

from __future__ import annotations

import re

from .models import OrderType, Side, Signal

# Words that indicate trade direction.
_BUY_WORDS = {"buy", "long", "bullish"}
_SELL_WORDS = {"sell", "short", "bearish"}

# A number like 65000, 65,000.50, 1,05,000, 3.14 — any comma grouping allowed
# (commas are stripped before parsing, so Indian and Western grouping both work).
_NUM = r"\d+(?:,\d+)*(?:\.\d+)?"

# Symbols look like BTCUSDT, BTC/USDT, BTC-USDT, RELIANCE, NIFTY50. We accept
# 2-15 uppercase alphanumerics, optionally split by / or -.
_SYMBOL = r"[A-Z][A-Z0-9]{1,14}(?:[/-][A-Z0-9]{2,10})?"

_SIDE_RE = re.compile(
    r"\b(buy|sell|long|short|bullish|bearish)\b", re.IGNORECASE
)
_LEVERAGE_RE = re.compile(r"(?:lev(?:erage)?[:\s]*)?(\d{1,3})\s*x\b", re.IGNORECASE)


def _to_float(token: str) -> float:
    return float(token.replace(",", ""))


def _find_numbers(text: str) -> list[float]:
    return [_to_float(m) for m in re.findall(_NUM, text)]


def _extract_labelled(text: str, labels: list[str]) -> str | None:
    """Return the text following any of ``labels`` up to the line end."""
    for label in labels:
        m = re.search(
            rf"{label}\s*[:\-=]?\s*(.+)", text, re.IGNORECASE
        )
        if m:
            return m.group(1).strip()
    return None


def parse_signal(message: str) -> Signal | None:
    """Parse a raw chat ``message`` into a :class:`Signal`, or ``None``.

    The parser never raises on malformed input; unrecognised messages simply
    yield ``None``.
    """
    if not message or not message.strip():
        return None

    text = message.strip()

    side_match = _SIDE_RE.search(text)
    if not side_match:
        return None
    word = side_match.group(1).lower()
    side = Side.BUY if word in _BUY_WORDS else Side.SELL

    symbol = _find_symbol(text, around=side_match.start())
    if symbol is None:
        return None

    leverage = None
    lev_match = _LEVERAGE_RE.search(text)
    if lev_match:
        leverage = int(lev_match.group(1))

    entry, entry_high, order_type = _parse_entry(text)
    stop_loss = _parse_first_number(
        text, ["stop\\s*loss", "stoploss", r"\bsl\b", "stop"]
    )
    targets = _parse_targets(text)

    return Signal(
        symbol=symbol,
        side=side,
        order_type=order_type,
        entry=entry,
        entry_high=entry_high,
        stop_loss=stop_loss,
        targets=targets,
        leverage=leverage,
        raw=message,
    )


def _find_symbol(text: str, around: int) -> str | None:
    """Find the most likely trading symbol in ``text``.

    Prefers an explicit ``#TICKER`` hashtag, then a token adjacent to the
    side keyword, then the first symbol-shaped token that is not a reserved
    keyword.
    """
    hashtag = re.search(r"#([A-Za-z][A-Za-z0-9]{1,14}(?:[/-][A-Za-z0-9]{2,10})?)", text)
    if hashtag:
        return hashtag.group(1)

    reserved = _BUY_WORDS | _SELL_WORDS | {
        "sl", "tp", "entry", "target", "targets", "stop", "stoploss",
        "leverage", "lev", "loss", "now", "market", "limit", "zone",
    }
    candidates = [
        (m.start(), m.group(0))
        for m in re.finditer(_SYMBOL, text)
        if m.group(0).lower() not in reserved
    ]
    if not candidates:
        return None
    # Choose the candidate closest to the side keyword (signals usually read
    # "BUY BTCUSDT" or "BTCUSDT LONG").
    candidates.sort(key=lambda c: abs(c[0] - around))
    return candidates[0][1]


def _parse_entry(text: str) -> tuple[float | None, float | None, OrderType]:
    """Extract entry price/zone and infer the order type.

    A message that says "market" or "now", or that has no entry price, is a
    MARKET order. An explicit entry price makes it a LIMIT order.
    """
    if re.search(r"\b(market|now|cmp)\b", text, re.IGNORECASE):
        return None, None, OrderType.MARKET

    raw = _extract_labelled(text, ["entry", "buy\\s*zone", "entry\\s*zone", "@"])
    if raw is None:
        # Fall back to a price right after the side keyword: "BUY BTC 65000".
        m = re.search(
            rf"\b(?:buy|sell|long|short)\b[^\d]*({_NUM})", text, re.IGNORECASE
        )
        if not m:
            return None, None, OrderType.MARKET
        return _to_float(m.group(1)), None, OrderType.LIMIT

    nums = _find_numbers(raw)
    if not nums:
        return None, None, OrderType.MARKET
    if len(nums) >= 2 and re.search(r"[-–]|to\b", raw):
        low, high = sorted(nums[:2])
        return low, high, OrderType.LIMIT
    return nums[0], None, OrderType.LIMIT


def _parse_first_number(text: str, labels: list[str]) -> float | None:
    raw = _extract_labelled(text, labels)
    if raw is None:
        return None
    nums = _find_numbers(raw)
    return nums[0] if nums else None


def _parse_targets(text: str) -> list[float]:
    raw = _extract_labelled(text, ["targets", "target", r"\btp\b", "take\\s*profit"])
    if raw is None:
        return []
    # Stop at a newline that introduces another field so we don't swallow SL.
    raw = re.split(r"\n", raw)[0]
    return _find_numbers(raw)
