"""Point-in-time membership, and the survivorship-bias tests that justify it."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nifty50.config import Config
from nifty50.trading_calendar import TradingCalendar
from nifty50.universe import (
    Membership,
    OverlappingMembershipError,
    PointInTimeUniverse,
    Provenance,
    UniverseChange,
    UnverifiedUniverseError,
)

# A miniature index of three names with one real reconstitution, so the
# assertions stay readable. BETA is dropped and GAMMA replaces it, effective
# 2021-10-01. ALPHA and DELTA are there throughout.
SWAP_LAST_DAY = dt.date(2021, 9, 30)
SWAP_FIRST_DAY = dt.date(2021, 10, 1)
WINDOW_START = dt.date(2019, 1, 1)
WINDOW_END = dt.date(2023, 12, 31)


def verified_universe(expected_size: int = 3) -> PointInTimeUniverse:
    return PointInTimeUniverse(
        [
            Membership("ALPHA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
            Membership("DELTA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
            Membership("BETA", WINDOW_START, SWAP_LAST_DAY, Provenance.NSE_CIRCULAR),
            Membership("GAMMA", SWAP_FIRST_DAY, None, Provenance.NSE_CIRCULAR),
        ],
        index_name="MINI",
        expected_size=expected_size,
    )


class TestSurvivorshipBias:
    """The reason this module exists.

    Every assertion here fails if the universe ever answers an as-of query with
    the *current* constituent list.
    """

    def test_a_delisted_name_is_still_a_member_before_it_left(self) -> None:
        universe = verified_universe()
        # BETA is gone today. A 2020 backtest must still be able to trade it —
        # dropping it is precisely how survivorship bias enters.
        assert universe.is_member("BETA", dt.date(2020, 6, 15))
        assert "BETA" in universe.members_on(dt.date(2020, 6, 15))

    def test_a_delisted_name_disappears_the_day_after_it_left(self) -> None:
        universe = verified_universe()
        assert universe.is_member("BETA", SWAP_LAST_DAY)
        assert not universe.is_member("BETA", SWAP_FIRST_DAY)

    def test_no_future_constituent_leaks_into_a_past_query(self) -> None:
        """The headline test: GAMMA joined in 2021 and must not exist in 2020."""
        universe = verified_universe()
        for day in (
            dt.date(2019, 1, 1),
            dt.date(2020, 6, 15),
            SWAP_LAST_DAY,
        ):
            members = universe.members_on(day)
            assert "GAMMA" not in members, f"future constituent leaked into {day}"
            assert not universe.is_member("GAMMA", day)

    def test_a_future_constituent_appears_exactly_on_its_start_date(self) -> None:
        universe = verified_universe()
        assert universe.is_member("GAMMA", SWAP_FIRST_DAY)

    def test_the_basket_swaps_cleanly_across_the_reconstitution(self) -> None:
        universe = verified_universe()
        assert universe.members_on(SWAP_LAST_DAY) == {"ALPHA", "DELTA", "BETA"}
        assert universe.members_on(SWAP_FIRST_DAY) == {"ALPHA", "DELTA", "GAMMA"}

    def test_symbols_ever_includes_names_that_left(self) -> None:
        # This is the set to backfill price data for. Omitting departed names
        # makes a point-in-time universe unusable: you would hold membership
        # records for bars you never downloaded.
        universe = verified_universe()
        assert universe.symbols_ever() == {"ALPHA", "BETA", "GAMMA", "DELTA"}
        assert len(universe.symbols_ever()) > len(universe.members_on(SWAP_FIRST_DAY))

    def test_membership_before_the_windows_start_is_not_assumed(self) -> None:
        universe = verified_universe()
        assert universe.members_on(dt.date(2018, 12, 31)) == frozenset()


class TestQueries:
    def test_size_on(self) -> None:
        universe = verified_universe()
        assert universe.size_on(dt.date(2020, 1, 1)) == 3
        assert universe.size_on(SWAP_FIRST_DAY) == 3

    def test_entered_and_exited(self) -> None:
        universe = verified_universe()
        assert universe.entered_on(SWAP_FIRST_DAY) == {"GAMMA"}
        assert universe.exited_on(SWAP_LAST_DAY) == {"BETA"}
        assert universe.entered_on(dt.date(2020, 5, 5)) == frozenset()

    def test_changes_between_is_chronological(self) -> None:
        universe = verified_universe()
        changes = universe.changes_between(dt.date(2021, 1, 1), dt.date(2021, 12, 31))
        assert changes == [
            UniverseChange(date=SWAP_LAST_DAY, symbol="BETA", joined=False),
            UniverseChange(date=SWAP_FIRST_DAY, symbol="GAMMA", joined=True),
        ]

    def test_changes_between_excludes_events_outside_the_window(self) -> None:
        universe = verified_universe()
        assert universe.changes_between(dt.date(2022, 1, 1), dt.date(2022, 12, 31)) == []

    def test_memberships_for_a_symbol(self) -> None:
        universe = verified_universe()
        spells = universe.memberships_for("beta")  # case-insensitive
        assert len(spells) == 1
        assert spells[0].end_date == SWAP_LAST_DAY

    def test_coverage(self) -> None:
        universe = verified_universe()
        coverage = universe.coverage
        assert coverage is not None
        earliest, latest = coverage
        assert earliest == WINDOW_START
        assert latest is None  # some members are still current


class TestReEntryAndOverlap:
    def test_a_name_may_leave_and_rejoin(self) -> None:
        universe = PointInTimeUniverse(
            [
                Membership("X", dt.date(2019, 1, 1), dt.date(2020, 3, 31), Provenance.VENDOR),
                Membership("X", dt.date(2022, 4, 1), None, Provenance.VENDOR),
            ],
            expected_size=1,
        )
        assert universe.is_member("X", dt.date(2019, 6, 1))
        assert not universe.is_member("X", dt.date(2021, 6, 1))  # the gap
        assert universe.is_member("X", dt.date(2023, 6, 1))

    def test_overlapping_spells_are_rejected_at_construction(self) -> None:
        with pytest.raises(OverlappingMembershipError, match="overlapping"):
            PointInTimeUniverse(
                [
                    Membership("X", dt.date(2019, 1, 1), dt.date(2021, 1, 1), Provenance.VENDOR),
                    Membership("X", dt.date(2020, 1, 1), None, Provenance.VENDOR),
                ]
            )

    def test_adjacent_spells_touching_on_one_day_are_an_overlap(self) -> None:
        # Bounds are inclusive, so end_date == next start_date means the symbol
        # is counted twice that day.
        with pytest.raises(OverlappingMembershipError):
            PointInTimeUniverse(
                [
                    Membership("X", dt.date(2019, 1, 1), dt.date(2020, 1, 1), Provenance.VENDOR),
                    Membership("X", dt.date(2020, 1, 1), None, Provenance.VENDOR),
                ]
            )

    def test_backwards_dates_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="precedes start_date"):
            Membership("X", dt.date(2021, 1, 1), dt.date(2020, 1, 1), Provenance.VENDOR)


class TestProvenanceAndFailClosed:
    def test_a_verified_universe_passes(self, calendar: TradingCalendar) -> None:
        universe = verified_universe()
        assert universe.is_verified
        universe.assert_backtest_ready(calendar, dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    def test_seed_provenance_blocks_a_backtest(self, calendar: TradingCalendar) -> None:
        universe = PointInTimeUniverse(
            [
                Membership("ALPHA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
                Membership("DELTA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
                Membership("BETA", WINDOW_START, None, Provenance.SEED),
            ],
            expected_size=3,
        )
        assert not universe.is_verified
        assert universe.unverified_symbols() == {"BETA"}
        with pytest.raises(UnverifiedUniverseError, match="seed"):
            universe.assert_backtest_ready(calendar, dt.date(2020, 1, 1), dt.date(2020, 6, 30))

    def test_an_empty_universe_blocks_a_backtest(self, calendar: TradingCalendar) -> None:
        universe = PointInTimeUniverse([])
        assert universe.is_empty
        with pytest.raises(UnverifiedUniverseError, match="empty"):
            universe.assert_backtest_ready(calendar, dt.date(2020, 1, 1), dt.date(2020, 6, 30))

    def test_a_wrong_sized_index_blocks_a_backtest(self, calendar: TradingCalendar) -> None:
        # Two names when fifty are expected means the file is incomplete, and an
        # incomplete universe silently changes what the strategy can trade.
        universe = PointInTimeUniverse(
            [
                Membership("ALPHA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
                Membership("DELTA", WINDOW_START, None, Provenance.NSE_CIRCULAR),
            ],
            expected_size=50,
        )
        with pytest.raises(UnverifiedUniverseError, match="expected 50"):
            universe.assert_backtest_ready(calendar, dt.date(2020, 1, 1), dt.date(2020, 6, 30))

    def test_validate_reports_the_short_dates(self, calendar: TradingCalendar) -> None:
        universe = verified_universe(expected_size=4)  # one too many
        problems = universe.validate(calendar, dt.date(2020, 1, 1), dt.date(2020, 3, 31))
        assert problems
        assert all("expected 4" in problem for problem in problems)

    def test_provenance_verified_flags(self) -> None:
        assert Provenance.NSE_CIRCULAR.is_verified
        assert Provenance.VENDOR.is_verified
        assert not Provenance.SEED.is_verified


class TestCsvLoading:
    def write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "constituents.csv"
        path.write_text(
            "# a comment line that must be skipped\n"
            "symbol,start_date,end_date,provenance,note\n" + body,
            encoding="utf-8",
        )
        return path

    def test_round_trip_with_a_blank_end_date(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            "ALPHA,2019-01-01,,nse_circular,still a member\n"
            "BETA,2019-01-01,2021-09-30,nse_circular,replaced by GAMMA\n",
        )
        universe = PointInTimeUniverse.from_csv(path, expected_size=2)
        assert len(universe) == 2
        assert universe.memberships_for("ALPHA")[0].end_date is None
        assert universe.memberships_for("BETA")[0].end_date == SWAP_LAST_DAY
        assert universe.is_verified

    def test_symbols_are_upper_cased_and_blank_rows_skipped(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            "alpha,2019-01-01,,vendor,lower case in the file\n"
            ",2019-01-01,,vendor,blank symbol is skipped\n",
        )
        universe = PointInTimeUniverse.from_csv(path)
        assert universe.symbols_ever() == {"ALPHA"}

    def test_an_unknown_provenance_is_rejected(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, "ALPHA,2019-01-01,,vibes,made up\n")
        with pytest.raises(ValueError, match="vibes"):
            PointInTimeUniverse.from_csv(path)


class TestShippedFile:
    """The file in the repo must fail closed rather than ship a plausible lie."""

    def test_the_shipped_constituents_file_exists_and_is_empty(self, real_config: Config) -> None:
        universe = PointInTimeUniverse.from_config(real_config)
        assert universe.is_empty, (
            "the shipped constituents file has data rows; if they were "
            "transcribed from NSE press releases, update this test"
        )

    def test_the_shipped_file_cannot_start_a_backtest(
        self, real_config: Config, calendar: TradingCalendar
    ) -> None:
        universe = PointInTimeUniverse.from_config(real_config)
        with pytest.raises(UnverifiedUniverseError):
            universe.assert_backtest_ready(calendar, dt.date(2020, 1, 1), dt.date(2020, 12, 31))

    def test_config_expects_fifty_names(self, real_config: Config) -> None:
        assert real_config.universe.expected_size == 50
        assert real_config.universe.require_point_in_time is True
