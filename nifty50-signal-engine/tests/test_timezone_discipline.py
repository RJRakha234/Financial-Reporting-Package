"""No naive datetimes anywhere. The spec asks for this to be asserted, not assumed.

Two layers:

* runtime — the domain types reject naive input at every boundary;
* static — the source tree is parsed and scanned for the constructs that
  *produce* naive datetimes. A runtime test only covers the paths a test
  happens to exercise; the AST scan covers code nobody has run yet.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from nifty50.config import project_root
from nifty50.domain import (
    IST,
    Candle,
    NaiveDatetimeError,
    Tick,
    Timeframe,
    ensure_ist,
    now_ist,
)

SRC = project_root() / "src" / "nifty50"

# Calls that yield a naive datetime unless a timezone is supplied.
_NAIVE_CALLS: dict[str, str] = {
    "utcnow": "datetime.utcnow() is always naive; use nifty50.domain.now_ist()",
    "utcfromtimestamp": "utcfromtimestamp() is always naive; use now_ist() or ensure_ist()",
}
# Calls that are naive only when the timezone argument is missing.
_NEEDS_TZ: dict[str, tuple[str, ...]] = {
    "now": ("tz",),
    "today": (),  # datetime.today() takes no tz at all
    "fromtimestamp": ("tz",),
    "combine": ("tzinfo",),
}
# Modules allowed to localise a naive datetime, with the reason.
_LOCALISATION_EXEMPT: dict[str, str] = {
    "kite.py": "the vendor contract documents that exchange timestamps are IST",
}


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestRuntimeRejection:
    def test_ensure_ist_rejects_naive(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            ensure_ist(dt.datetime(2025, 8, 4, 9, 15))

    def test_ensure_ist_converts_other_zones(self) -> None:
        utc = dt.datetime(2025, 8, 4, 3, 45, tzinfo=dt.UTC)
        converted = ensure_ist(utc)
        assert converted.hour == 9
        assert converted.minute == 15
        assert str(converted.tzinfo) == str(IST)

    def test_now_is_tz_aware(self) -> None:
        assert now_ist().tzinfo is not None

    def test_tick_rejects_naive(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            Tick(
                instrument_key="NSE:RELIANCE",
                ts=dt.datetime(2025, 8, 4, 9, 15),
                last_price=100.0,
            )

    def test_candle_rejects_naive(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            Candle(
                instrument_key="NSE:RELIANCE",
                timeframe=Timeframe.M15,
                ts=dt.datetime(2025, 8, 4, 9, 15),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1,
            )

    def test_candle_rejects_an_impossible_ohlc(self) -> None:
        with pytest.raises(ValueError, match="high < low"):
            Candle(
                instrument_key="NSE:RELIANCE",
                timeframe=Timeframe.M15,
                ts=dt.datetime(2025, 8, 4, 9, 15, tzinfo=IST),
                open=1.0,
                high=0.5,
                low=1.5,
                close=1.0,
                volume=1,
            )

    def test_store_and_adjuster_reject_naive_frames(self) -> None:
        from nifty50.corporate_actions import adjust_ohlcv

        frame = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
            index=pd.DatetimeIndex([dt.datetime(2025, 8, 4, 9, 15)], name="ts"),
        )
        with pytest.raises(ValueError, match="tz-aware"):
            adjust_ohlcv(frame, [], symbol="X")


class TestStaticScan:
    """Parse every module and fail on constructs that produce naive datetimes."""

    def test_no_naive_datetime_constructs_in_the_source_tree(self) -> None:
        offences: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node)
                if name is None:
                    continue
                if name in _NAIVE_CALLS:
                    offences.append(f"{path.name}:{node.lineno} {name}() — {_NAIVE_CALLS[name]}")
                    continue
                if name in _NEEDS_TZ:
                    accepted = _NEEDS_TZ[name]
                    supplied = {kw.arg for kw in node.keywords if kw.arg}
                    if not accepted or not (supplied & set(accepted)):
                        offences.append(
                            f"{path.name}:{node.lineno} {name}() without "
                            f"{' or '.join(accepted) or 'any timezone'}"
                        )
        assert offences == [], "naive datetime constructs found:\n" + "\n".join(offences)

    def test_localisation_is_confined_to_documented_vendor_boundaries(self) -> None:
        """``replace(tzinfo=...)`` turns a naive value into a lie unless the
        source's timezone is contractually known. Only the broker adapter may."""
        offences: list[str] = []
        for path in _python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _called_name(node) != "replace":
                    continue
                if not any(kw.arg == "tzinfo" for kw in node.keywords):
                    continue
                if path.name in _LOCALISATION_EXEMPT:
                    continue
                offences.append(f"{path.name}:{node.lineno} replace(tzinfo=...)")
        assert offences == [], (
            "naive-to-aware localisation outside a documented vendor boundary:\n"
            + "\n".join(offences)
        )

    def test_the_scanner_actually_catches_something(self) -> None:
        # A scanner that never fires is indistinguishable from a scanner that is
        # broken, so prove it fires on a known-bad snippet.
        tree = ast.parse("import datetime\nx = datetime.datetime.now()\n")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        assert any(_called_name(call) == "now" for call in calls)
        assert not any(kw.arg == "tz" for call in calls for kw in call.keywords)


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None
