"""Data-integrity checks run after every ingest.

PART 10 of the spec says that when results look too good you should assume a bug
first, and it ranks the usual Indian-equity culprits: unadjusted corporate
actions, survivorship bias, understated charges, fills through circuit limits.
This module is the automated form of the first of those, plus the plumbing
failures (missing bars, duplicates, out-of-order ticks) that quietly corrupt a
series without ever raising.

Two checks here are worth calling out because they are unusual:

``find_unexpected_bars``
    Bars that exist where the calendar says the exchange was shut. This catches
    a *wrong holiday list* — the seeded ``nse_holidays.csv`` is recalled rather
    than scraped, and a phantom holiday silently deletes a real session from
    every backtest.

``find_suspected_unadjusted_actions``
    Overnight moves too large to be real, with no corporate action on file. Every
    hit is a missing row in ``corporate_actions.csv`` until proven otherwise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

import numpy as np
import pandas as pd

from nifty50.domain import Timeframe
from nifty50.frames import as_float, bar_index, ist_index
from nifty50.trading_calendar.calendar import TradingCalendar


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    severity: Severity
    message: str
    timestamps: tuple[dt.datetime, ...] = ()

    def __str__(self) -> str:
        return f"[{self.severity.value.upper():7s}] {self.check}: {self.message}"


@dataclass(slots=True)
class IntegrityReport:
    symbol: str
    timeframe: Timeframe
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def is_clean(self) -> bool:
        return not self.errors and not self.warnings

    def describe(self) -> str:
        header = f"{self.symbol} [{self.timeframe.value}]"
        if not self.findings:
            return f"{header}: clean"
        return "\n".join([header, *(f"  {finding}" for finding in self.findings)])


# --------------------------------------------------------------------- checks


def find_missing_bars(
    frame: pd.DataFrame,
    calendar: TradingCalendar,
    timeframe: Timeframe,
    start: dt.date,
    end: dt.date,
) -> list[dt.datetime]:
    """Bar starts the calendar expects but the data does not contain."""
    expected = calendar.expected_bar_starts(start, end, timeframe)
    if not expected:
        return []
    present = set(frame.index)
    return [ts for ts in expected if ts not in present]


def find_unexpected_bars(
    frame: pd.DataFrame,
    calendar: TradingCalendar,
    timeframe: Timeframe,
) -> list[dt.datetime]:
    """Bars at timestamps the calendar says cannot exist.

    Hits mean one of: a wrong holiday row, a missing special session (muhurat),
    or a vendor emitting bars outside the continuous session. All three are
    real bugs; none of them should be tolerated silently.
    """
    unexpected: list[dt.datetime] = []
    by_day: dict[dt.date, set[dt.datetime]] = {}
    for ts in frame.index:
        by_day.setdefault(ts.date(), set()).add(ts.to_pydatetime())
    for day, timestamps in by_day.items():
        if not calendar.has_any_session(day):
            unexpected.extend(sorted(timestamps))
            continue
        valid = set(calendar.bar_starts(day, timeframe))
        unexpected.extend(sorted(timestamps - valid))
    return sorted(unexpected)


def find_duplicate_bars(frame: pd.DataFrame) -> list[dt.datetime]:
    if frame.empty:
        return []
    duplicated = frame.index[frame.index.duplicated(keep=False)]
    return sorted({ts.to_pydatetime() for ts in duplicated})


def find_out_of_order(frame: pd.DataFrame) -> list[dt.datetime]:
    """Timestamps that arrive after a later one. Should always be empty on read."""
    if len(frame) < 2:
        return []
    index = bar_index(frame)
    decreasing = np.flatnonzero(np.asarray(index[1:] < index[:-1]))
    return [index[int(position) + 1].to_pydatetime() for position in decreasing]


def find_invalid_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows violating the OHLC contract, annotated with the reason."""
    if frame.empty:
        return frame.assign(reason=pd.Series(dtype="object"))
    # Positional rather than label-based: a frame under inspection may well have
    # duplicate timestamps, which is one of the things we are checking for.
    reasons: list[tuple[int, str]] = []
    for position in range(len(frame)):
        row = frame.iloc[position]
        problems: list[str] = []
        if row["high"] < row["low"]:
            problems.append("high<low")
        if not (row["low"] <= row["open"] <= row["high"]):
            problems.append("open outside range")
        if not (row["low"] <= row["close"] <= row["high"]):
            problems.append("close outside range")
        if row["volume"] < 0:
            problems.append("negative volume")
        if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
            problems.append("non-positive price")
        if problems:
            reasons.append((position, ", ".join(problems)))
    if not reasons:
        return frame.iloc[0:0].assign(reason=pd.Series(dtype="object"))
    result = frame.iloc[[position for position, _ in reasons]].copy()
    result["reason"] = [reason for _, reason in reasons]
    return result


def find_suspected_unadjusted_actions(
    frame: pd.DataFrame,
    *,
    threshold_abs_log_return: float,
    known_action_dates: Iterable[dt.date] = (),
) -> list[tuple[dt.date, float]]:
    """Close-to-close moves too large to be real, unexplained by a known action.

    Only *overnight* transitions are examined. An intraday 20% move is a circuit
    event or a news shock and is real; a 20% gap between one session's close and
    the next session's close, with nothing in the actions table, is almost always
    a split or bonus nobody recorded.
    """
    if len(frame) < 2:
        return []
    known = set(known_action_dates)
    daily_close = frame["close"].groupby(ist_index(frame).date).last()
    if len(daily_close) < 2:
        return []
    values = daily_close.to_numpy(dtype="float64")
    dates = list(daily_close.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        log_returns = np.diff(np.log(values))
    suspects: list[tuple[dt.date, float]] = []
    for position, move in enumerate(log_returns):
        day = dates[position + 1]
        if not np.isfinite(move) or abs(move) < threshold_abs_log_return:
            continue
        if day in known:
            continue
        suspects.append((day, float(move)))
    return suspects


def find_gaps_across_sessions(
    frame: pd.DataFrame, calendar: TradingCalendar
) -> list[tuple[dt.date, dt.date]]:
    """Consecutive stored sessions that skip one or more expected trading days."""
    if frame.empty:
        return []
    days = sorted({ts.date() for ts in ist_index(frame)})
    gaps: list[tuple[dt.date, dt.date]] = []
    for previous, current in pairwise(days):
        expected_next = calendar.next_trading_day(previous)
        if expected_next < current:
            gaps.append((previous, current))
    return gaps


@dataclass(frozen=True, slots=True)
class Mismatch:
    ts: dt.datetime
    field: str
    live: float
    rest: float
    relative_error: float


def reconcile_candles(
    live: pd.DataFrame,
    rest: pd.DataFrame,
    *,
    price_rel_tolerance: float,
    volume_rel_tolerance: float,
) -> list[Mismatch]:
    """Compare tick-assembled bars against the broker's own REST bars.

    Run at every bar close. Persistent mismatches mean the local aggregator is
    dropping ticks, the vendor is backfilling late prints, or both — and either
    way the signal layer is reading a series the exchange would not recognise.
    """
    shared = live.index.intersection(rest.index)
    mismatches: list[Mismatch] = []
    for ts in shared:
        for field_name, tolerance in (
            ("open", price_rel_tolerance),
            ("high", price_rel_tolerance),
            ("low", price_rel_tolerance),
            ("close", price_rel_tolerance),
            ("volume", volume_rel_tolerance),
        ):
            if field_name not in live.columns or field_name not in rest.columns:
                continue
            live_value = as_float(live.at[ts, field_name])
            rest_value = as_float(rest.at[ts, field_name])
            denominator = abs(rest_value)
            if denominator == 0.0:
                relative = 0.0 if live_value == 0.0 else float("inf")
            else:
                relative = abs(live_value - rest_value) / denominator
            if relative > tolerance:
                mismatches.append(
                    Mismatch(
                        ts=ts.to_pydatetime(),
                        field=field_name,
                        live=live_value,
                        rest=rest_value,
                        relative_error=relative,
                    )
                )
    return mismatches


def suggest_missing_holidays(
    frames: Mapping[str, pd.DataFrame],
    calendar: TradingCalendar,
    start: dt.date,
    end: dt.date,
    *,
    min_symbols: int = 5,
) -> list[dt.date]:
    """Trading days on which no symbol has a single bar.

    With enough symbols in the sample this is near-conclusive evidence of an
    unlisted exchange holiday. It is how the seeded holiday file gets corrected
    from data rather than from memory.
    """
    if len(frames) < min_symbols:
        return []
    observed: set[dt.date] = set()
    for frame in frames.values():
        if frame.empty:
            continue
        observed.update(ts.date() for ts in ist_index(frame))
    return [day for day in calendar.trading_days(start, end) if day not in observed]


def check_bars(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe,
    calendar: TradingCalendar,
    start: dt.date,
    end: dt.date,
    suspect_abs_log_return: float,
    known_action_dates: Sequence[dt.date] = (),
    max_missing_pct: float = 0.02,
) -> IntegrityReport:
    """Run the full check suite over one stored series."""
    report = IntegrityReport(symbol=symbol, timeframe=timeframe)

    duplicates = find_duplicate_bars(frame)
    if duplicates:
        report.add(
            Finding(
                check="duplicate_bars",
                severity=Severity.ERROR,
                message=f"{len(duplicates)} duplicated bar timestamps",
                timestamps=tuple(duplicates[:10]),
            )
        )

    out_of_order = find_out_of_order(frame)
    if out_of_order:
        report.add(
            Finding(
                check="out_of_order",
                severity=Severity.ERROR,
                message=f"{len(out_of_order)} bars out of chronological order",
                timestamps=tuple(out_of_order[:10]),
            )
        )

    invalid = find_invalid_ohlc(frame)
    if not invalid.empty:
        report.add(
            Finding(
                check="invalid_ohlc",
                severity=Severity.ERROR,
                message=f"{len(invalid)} bars violate the OHLC contract",
                timestamps=tuple(ts.to_pydatetime() for ts in invalid.index[:10]),
            )
        )

    unexpected = find_unexpected_bars(frame, calendar, timeframe)
    if unexpected:
        report.add(
            Finding(
                check="unexpected_bars",
                severity=Severity.WARNING,
                message=(
                    f"{len(unexpected)} bars at times the calendar says are closed; "
                    "suspect a wrong holiday row or a missing special session"
                ),
                timestamps=tuple(unexpected[:10]),
            )
        )

    missing = find_missing_bars(frame, calendar, timeframe, start, end)
    expected_total = len(calendar.expected_bar_starts(start, end, timeframe))
    if missing and expected_total:
        fraction = len(missing) / expected_total
        severity = Severity.ERROR if fraction > max_missing_pct else Severity.WARNING
        report.add(
            Finding(
                check="missing_bars",
                severity=severity,
                message=f"{len(missing)}/{expected_total} expected bars absent ({fraction:.2%})",
                timestamps=tuple(missing[:10]),
            )
        )

    suspects = find_suspected_unadjusted_actions(
        frame,
        threshold_abs_log_return=suspect_abs_log_return,
        known_action_dates=known_action_dates,
    )
    if suspects:
        report.add(
            Finding(
                check="suspected_unadjusted_action",
                severity=Severity.ERROR,
                message=(
                    f"{len(suspects)} overnight move(s) beyond "
                    f"{suspect_abs_log_return:.0%} with no corporate action on file: "
                    + ", ".join(f"{day} {move:+.1%}" for day, move in suspects[:5])
                ),
            )
        )

    return report
