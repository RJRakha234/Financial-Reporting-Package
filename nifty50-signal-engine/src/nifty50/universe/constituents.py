"""Point-in-time index membership.

The Nifty 50 is reconstituted semi-annually. Backtesting today's fifty names
over seven years is textbook survivorship bias: the basket was selected
*because* it survived, so the strategy is handed foreknowledge of which
companies did not blow up. This module makes that mistake structurally hard.

Three properties carry the weight:

**As-of queries only.** :meth:`PointInTimeUniverse.members_on` answers "who was
in the index on this date", never "who is in it now". A name that joined in 2023
is simply absent from a 2020 query — there is no code path that returns it.

**Fail closed on unverified data.** Membership history cannot be pulled from any
broker API; it comes from NSE's index-reconstitution press releases. Rows carry
a :class:`Provenance`, and a universe containing any ``seed`` (recalled,
unverified) row refuses to serve a backtest. An empty file refuses too. Silently
falling back to the current constituent list is exactly the bug this phase
exists to eliminate, so there is no fallback.

**Overlap is an error, re-entry is not.** A symbol may legitimately leave the
index and rejoin later — that is two disjoint intervals. Two *overlapping*
intervals for one symbol is a data-entry error and is rejected at load.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final

from nifty50.logging_setup import get_logger
from nifty50.trading_calendar.calendar import TradingCalendar

log = get_logger(__name__)

_COMMENT_PREFIX: Final[str] = "#"
# Sampling stride for the size invariant; checking every session of seven years
# is wasteful when a reconstitution can only land on a handful of dates.
_SIZE_CHECK_STRIDE_DAYS: Final[int] = 5


class Provenance(StrEnum):
    """Where a membership row came from. Determines whether it can be trusted."""

    NSE_CIRCULAR = "nse_circular"
    VENDOR = "vendor"
    SEED = "seed"

    @property
    def is_verified(self) -> bool:
        """Seed rows are recall, not evidence, and must not reach a backtest."""
        return self is not Provenance.SEED


class UnverifiedUniverseError(RuntimeError):
    """Raised when a backtest is attempted against unverified membership data.

    Do not work around this by relaxing the check. The failure mode it prevents
    — a survivorship-biased universe silently producing flattering results — is
    the single most common reason a retail Indian equity backtest looks good and
    is worthless.
    """


class OverlappingMembershipError(ValueError):
    """Raised when one symbol has two overlapping membership intervals."""


@dataclass(frozen=True, slots=True)
class Membership:
    """One continuous spell of index membership for one symbol.

    ``start_date`` is the first session on which the symbol is a constituent.
    ``end_date`` is the last such session, inclusive; ``None`` means it is still
    a member. Both bounds are inclusive so that a reconstitution effective on a
    given morning reads the same way the NSE press release states it.
    """

    symbol: str
    start_date: dt.date
    end_date: dt.date | None
    provenance: Provenance
    note: str = ""

    def __post_init__(self) -> None:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError(
                f"{self.symbol}: end_date {self.end_date} precedes start_date {self.start_date}"
            )

    def covers(self, day: dt.date) -> bool:
        if day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    def overlaps(self, other: Membership) -> bool:
        latest_start = max(self.start_date, other.start_date)
        earliest_end = min(
            self.end_date or dt.date.max,
            other.end_date or dt.date.max,
        )
        return latest_start <= earliest_end


@dataclass(frozen=True, slots=True)
class UniverseChange:
    """A single add or drop, as of the date it took effect."""

    date: dt.date
    symbol: str
    joined: bool

    def __str__(self) -> str:
        arrow = "+" if self.joined else "-"
        return f"{self.date.isoformat()} {arrow}{self.symbol}"


class PointInTimeUniverse:
    """Index membership queryable as of any date."""

    def __init__(
        self,
        memberships: list[Membership],
        *,
        index_name: str = "NIFTY50",
        expected_size: int = 50,
    ) -> None:
        self._memberships = list(memberships)
        self._index_name = index_name
        self._expected_size = expected_size
        self._reject_overlaps()

    # ------------------------------------------------------------------ load

    @classmethod
    def from_csv(
        cls, path: Path, *, index_name: str = "NIFTY50", expected_size: int = 50
    ) -> PointInTimeUniverse:
        memberships: list[Membership] = []
        with path.open("r", encoding="utf-8") as handle:
            lines = [ln for ln in handle if not ln.lstrip().startswith(_COMMENT_PREFIX)]
        for row in csv.DictReader(lines):
            symbol = row["symbol"].strip().upper()
            if not symbol:
                continue
            memberships.append(
                Membership(
                    symbol=symbol,
                    start_date=dt.date.fromisoformat(row["start_date"].strip()),
                    end_date=_optional_date(row.get("end_date")),
                    provenance=Provenance(row["provenance"].strip().lower()),
                    note=(row.get("note") or "").strip(),
                )
            )
        universe = cls(memberships, index_name=index_name, expected_size=expected_size)
        if not universe.is_verified:
            log.warning(
                "universe.unverified",
                index=index_name,
                memberships=len(memberships),
                hint="backtests will refuse to run; see README 'Data provenance'",
            )
        return universe

    @classmethod
    def from_config(cls, config: object) -> PointInTimeUniverse:
        from nifty50.config import Config

        assert isinstance(config, Config)
        return cls.from_csv(
            config.path(config.universe.constituents_file),
            index_name=config.universe.index,
            expected_size=config.universe.expected_size,
        )

    # ------------------------------------------------------------- queries

    def members_on(self, day: dt.date) -> frozenset[str]:
        """Constituents as of ``day``.

        This is the only sanctioned way to ask what the universe was. It cannot
        return a symbol whose membership began after ``day``.
        """
        return frozenset(m.symbol for m in self._memberships if m.covers(day))

    def is_member(self, symbol: str, day: dt.date) -> bool:
        target = symbol.strip().upper()
        return any(m.symbol == target and m.covers(day) for m in self._memberships)

    def size_on(self, day: dt.date) -> int:
        return len(self.members_on(day))

    def symbols_ever(self) -> frozenset[str]:
        """Every symbol that was ever a member — the set to backfill data for."""
        return frozenset(m.symbol for m in self._memberships)

    def memberships_for(self, symbol: str) -> list[Membership]:
        target = symbol.strip().upper()
        return sorted(
            (m for m in self._memberships if m.symbol == target),
            key=lambda m: m.start_date,
        )

    def entered_on(self, day: dt.date) -> frozenset[str]:
        return frozenset(m.symbol for m in self._memberships if m.start_date == day)

    def exited_on(self, day: dt.date) -> frozenset[str]:
        """Symbols whose last session as a member was ``day``."""
        return frozenset(m.symbol for m in self._memberships if m.end_date == day)

    def changes_between(self, start: dt.date, end: dt.date) -> list[UniverseChange]:
        """Adds and drops taking effect in ``[start, end]``, chronologically."""
        changes: list[UniverseChange] = []
        for membership in self._memberships:
            if start <= membership.start_date <= end:
                changes.append(
                    UniverseChange(
                        date=membership.start_date, symbol=membership.symbol, joined=True
                    )
                )
            if membership.end_date is not None and start <= membership.end_date <= end:
                changes.append(
                    UniverseChange(date=membership.end_date, symbol=membership.symbol, joined=False)
                )
        return sorted(changes, key=lambda c: (c.date, not c.joined, c.symbol))

    # ---------------------------------------------------------- provenance

    @property
    def is_verified(self) -> bool:
        """True only when every row came from a citable source."""
        return bool(self._memberships) and all(m.provenance.is_verified for m in self._memberships)

    @property
    def is_empty(self) -> bool:
        return not self._memberships

    def unverified_symbols(self) -> frozenset[str]:
        return frozenset(m.symbol for m in self._memberships if not m.provenance.is_verified)

    @property
    def coverage(self) -> tuple[dt.date, dt.date | None] | None:
        """Earliest start and latest end across all memberships."""
        if not self._memberships:
            return None
        earliest = min(m.start_date for m in self._memberships)
        if any(m.end_date is None for m in self._memberships):
            return earliest, None
        return earliest, max(m.end_date for m in self._memberships if m.end_date)

    # --------------------------------------------------------- validation

    def _reject_overlaps(self) -> None:
        by_symbol: dict[str, list[Membership]] = {}
        for membership in self._memberships:
            by_symbol.setdefault(membership.symbol, []).append(membership)
        for symbol, spells in by_symbol.items():
            ordered = sorted(spells, key=lambda m: m.start_date)
            for earlier, later in pairwise(ordered):
                if earlier.overlaps(later):
                    raise OverlappingMembershipError(
                        f"{symbol} has overlapping membership spells: "
                        f"{earlier.start_date}..{earlier.end_date} and "
                        f"{later.start_date}..{later.end_date}. Re-entry is fine; "
                        "overlap is a data-entry error."
                    )

    def validate(self, calendar: TradingCalendar, start: dt.date, end: dt.date) -> list[str]:
        """Structural problems over ``[start, end]``. Empty list means healthy."""
        problems: list[str] = []
        if self.is_empty:
            problems.append("universe is empty: no membership rows loaded")
            return problems

        for day in calendar.trading_days(start, end)[::_SIZE_CHECK_STRIDE_DAYS]:
            size = self.size_on(day)
            if size != self._expected_size:
                problems.append(
                    f"{day.isoformat()}: {size} constituents, expected {self._expected_size}"
                )
        if not self.is_verified:
            unverified = sorted(self.unverified_symbols())
            problems.append(
                f"{len(unverified)} symbol(s) have seed (unverified) provenance: "
                + ", ".join(unverified[:10])
            )
        return problems

    def assert_backtest_ready(
        self, calendar: TradingCalendar, start: dt.date, end: dt.date
    ) -> None:
        """Refuse to proceed unless membership is real and complete.

        Called by the backtest engine before a single bar is read. The whole
        point of Phase 2 is that a survivorship-biased run should be impossible
        to start by accident, not merely discouraged in the README.
        """
        problems = self.validate(calendar, start, end)
        if problems:
            detail = "\n  ".join(problems[:10])
            raise UnverifiedUniverseError(
                f"{self._index_name} membership is not backtest-ready:\n  {detail}\n"
                "Populate data/reference/nifty50_constituents.csv from NSE index "
                "reconstitution press releases (provenance=nse_circular) before "
                "running a backtest. Using the current constituent list over "
                "history is survivorship bias and will overstate every result."
            )

    def __len__(self) -> int:
        return len(self._memberships)

    def __repr__(self) -> str:
        status = "verified" if self.is_verified else "UNVERIFIED"
        return (
            f"<PointInTimeUniverse {self._index_name} "
            f"{len(self._memberships)} memberships, {status}>"
        )


def _optional_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    stripped = value.strip()
    return dt.date.fromisoformat(stripped) if stripped else None
