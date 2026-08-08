"""Reading historical exports that this project did not produce.

Each test plants one specific defect that has been seen in real vendor files
and asserts it is *detected*, not that it is silently repaired. Repairing is
the caller's decision: a file whose close was back-adjusted while its
open/high/low were left raw can be fixed, but only by someone who knows which
of the two series the vendor meant.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from nifty50.data.vendor_csv import (
    VendorCsvError,
    conform_daily_to_session_open,
    infer_timeframe,
    load_vendor_csv,
    load_vendor_file,
    scan_directory,
    suspected_split_dates,
)
from nifty50.domain import Timeframe


def write_csv(path: Path, rows: dict[str, list[object]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def intraday_rows(count: int = 40, *, start: float = 100.0) -> dict[str, list[object]]:
    stamps = pd.date_range("2025-08-04 09:15", periods=count, freq="15min")
    closes = [start + index * 0.5 for index in range(count)]
    return {
        "Datetime": [stamp.strftime("%Y-%m-%d %H:%M:%S") for stamp in stamps],
        "Open": closes,
        "High": [value + 1.0 for value in closes],
        "Low": [value - 1.0 for value in closes],
        "Close": closes,
        "Volume": [1000 + index for index in range(count)],
    }


class TestColumnMapping:
    def test_it_reads_a_plain_export(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "RELIANCE_15min.csv", intraday_rows())
        result = load_vendor_csv(path)
        assert result.rows == 40
        assert result.timeframe is Timeframe.M15
        assert result.symbol_hint == "RELIANCE"
        assert list(result.frame.columns[:5]) == ["open", "high", "low", "close", "volume"]

    def test_prices_survive_the_load(self, tmp_path: Path) -> None:
        """The regression test for the all-NaN frame.

        ``pd.DataFrame(dict_of_series, index=new_index)`` reindexes each Series
        from its RangeIndex onto the new DatetimeIndex, which matches nothing.
        The result is a full-length frame of NaN with no error raised — it
        reads downstream as "every price is missing".
        """
        path = write_csv(tmp_path / "X.csv", intraday_rows(start=250.0))
        frame = load_vendor_csv(path).frame
        assert frame[["open", "high", "low", "close"]].notna().all().all()
        assert frame["close"].iloc[0] == pytest.approx(250.0)

    def test_nse_bhavcopy_column_names_are_recognised(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "bhav.csv",
            {
                "SYMBOL": ["RELIANCE"] * 3,
                "SERIES": ["EQ"] * 3,
                "DATE1": ["01-Aug-2025", "04-Aug-2025", "05-Aug-2025"],
                "OPEN_PRICE": [100.0, 101.0, 102.0],
                "HIGH_PRICE": [103.0, 104.0, 105.0],
                "LOW_PRICE": [99.0, 100.0, 101.0],
                "CLOSE_PRICE": [101.0, 102.0, 103.0],
                "TTL_TRD_QNTY": [5000, 6000, 7000],
                "DELIV_PER": [45.5, 51.2, 48.0],
            },
        )
        result = load_vendor_csv(path)
        assert result.symbol_hint == "RELIANCE"
        assert "delivery_pct" in result.frame.columns
        assert result.frame["close"].iloc[0] == pytest.approx(101.0)

    def test_a_file_without_prices_is_refused(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "notes.csv", {"date": ["2025-08-04"], "note": ["hello"]})
        with pytest.raises(VendorCsvError, match="no column matched"):
            load_vendor_csv(path)

    def test_a_file_without_a_timestamp_is_refused(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "x.csv",
            {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5]},
        )
        with pytest.raises(VendorCsvError, match="no column matched"):
            load_vendor_csv(path)


class TestDateAmbiguity:
    def test_iso_dates_are_not_flagged_as_ambiguous(self, tmp_path: Path) -> None:
        """A leading four-digit year fixes the field order by definition.

        pandas asked to parse ``2019-01-02`` with ``dayfirst=True`` reads it as
        YYYY-DD-MM, so a naive day-first/month-first comparison disagrees with
        itself and flags every ISO file ever written.
        """
        path = write_csv(tmp_path / "iso.csv", intraday_rows())
        result = load_vendor_csv(path)
        assert not any("AMBIGUOUS" in note for note in result.inferences)
        assert result.frame.index[0].month == 8
        assert result.frame.index[0].day == 4

    def test_a_named_month_is_not_ambiguous(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "named.csv",
            {
                "Date": ["03-Jan-2025", "06-Jan-2025", "07-Jan-2025"],
                "Open": [1.0, 1.0, 1.0], "High": [2.0, 2.0, 2.0],
                "Low": [0.5, 0.5, 0.5], "Close": [1.5, 1.5, 1.5],
            },
        )
        result = load_vendor_csv(path)
        assert not any("AMBIGUOUS" in note for note in result.inferences)
        assert result.frame.index[0].day == 3

    def test_genuinely_ambiguous_numeric_dates_are_flagged(self, tmp_path: Path) -> None:
        # Every day is <= 12, so both readings parse and they disagree.
        path = write_csv(
            tmp_path / "amb.csv",
            {
                "Date": ["03-01-2025", "05-02-2025", "07-03-2025"],
                "Open": [1.0, 1.0, 1.0], "High": [2.0, 2.0, 2.0],
                "Low": [0.5, 0.5, 0.5], "Close": [1.5, 1.5, 1.5],
            },
        )
        result = load_vendor_csv(path)
        assert any("AMBIGUOUS DATES" in note for note in result.inferences)

    def test_a_day_above_twelve_disambiguates(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "clear.csv",
            {
                "Date": ["25-01-2025", "26-01-2025", "27-01-2025"],
                "Open": [1.0, 1.0, 1.0], "High": [2.0, 2.0, 2.0],
                "Low": [0.5, 0.5, 0.5], "Close": [1.5, 1.5, 1.5],
            },
        )
        result = load_vendor_csv(path)
        assert not any("AMBIGUOUS DATES" in note for note in result.inferences)
        assert result.frame.index[0].day == 25


class TestTimezone:
    def test_naive_timestamps_are_localised_to_ist_and_it_is_recorded(
        self, tmp_path: Path
    ) -> None:
        path = write_csv(tmp_path / "naive.csv", intraday_rows())
        result = load_vendor_csv(path)
        assert str(result.frame.index.tz) == "Asia/Kolkata"
        assert any("localised to IST" in note for note in result.inferences)

    def test_an_offset_bearing_file_is_converted_not_relabelled(self, tmp_path: Path) -> None:
        # 03:45 UTC is 09:15 IST. Relabelling instead of converting would put
        # the open at 03:45 IST and silently invent a pre-dawn session.
        path = write_csv(
            tmp_path / "utc.csv",
            {
                "Datetime": ["2025-08-04T03:45:00+00:00", "2025-08-04T04:00:00+00:00"],
                "Open": [1.0, 1.0], "High": [2.0, 2.0],
                "Low": [0.5, 0.5], "Close": [1.5, 1.5],
            },
        )
        result = load_vendor_csv(path)
        assert result.frame.index[0].hour == 9
        assert result.frame.index[0].minute == 15


class TestAdjustmentDetection:
    def test_an_adjusted_close_that_differs_marks_the_file_unadjusted(
        self, tmp_path: Path
    ) -> None:
        rows = intraday_rows()
        rows["Adj Close"] = [float(value) * 0.5 for value in rows["Close"]]  # type: ignore[arg-type]
        path = write_csv(tmp_path / "unadj.csv", rows)
        result = load_vendor_csv(path)
        assert any("UNADJUSTED" in concern for concern in result.concerns)

    def test_a_close_outside_its_own_range_is_the_half_adjusted_signature(
        self, tmp_path: Path
    ) -> None:
        rows = intraday_rows()
        rows["Close"] = [float(value) * 0.5 for value in rows["Close"]]  # type: ignore[arg-type]
        path = write_csv(tmp_path / "half.csv", rows)
        result = load_vendor_csv(path)
        assert any("outside their own high-low" in concern for concern in result.concerns)

    def test_a_clean_file_raises_no_adjustment_concern(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "clean.csv", intraday_rows())
        result = load_vendor_csv(path)
        assert not any("outside their own" in concern for concern in result.concerns)
        assert not any("UNADJUSTED" in concern for concern in result.concerns)

    def test_a_split_sized_gap_is_surfaced_as_a_candidate(self, tmp_path: Path) -> None:
        stamps = pd.date_range("2025-01-01", periods=10, freq="D")
        closes = [100.0] * 5 + [50.0] * 5  # a 1:2 split, unadjusted
        frame = pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1},
            index=stamps,
        )
        assert len(suspected_split_dates(frame)) == 1


class TestDataQualityFlags:
    def test_a_missing_volume_column_is_called_out(self, tmp_path: Path) -> None:
        rows = intraday_rows()
        del rows["Volume"]
        path = write_csv(tmp_path / "novol.csv", rows)
        result = load_vendor_csv(path)
        assert any("no volume column" in concern for concern in result.concerns)

    def test_non_eq_series_rows_are_flagged(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path / "series.csv",
            {
                "SYMBOL": ["X"] * 3, "SERIES": ["EQ", "BE", "EQ"],
                "DATE1": ["2025-08-01", "2025-08-04", "2025-08-05"],
                "OPEN_PRICE": [1.0] * 3, "HIGH_PRICE": [2.0] * 3,
                "LOW_PRICE": [0.5] * 3, "CLOSE_PRICE": [1.5] * 3,
                "TTL_TRD_QNTY": [1, 2, 3],
            },
        )
        result = load_vendor_csv(path)
        assert any("only EQ" in concern for concern in result.concerns)

    def test_a_negative_price_is_flagged(self, tmp_path: Path) -> None:
        rows = intraday_rows(count=5)
        rows["Low"][2] = -1.0
        path = write_csv(tmp_path / "neg.csv", rows)
        result = load_vendor_csv(path)
        assert any("zero or negative price" in concern for concern in result.concerns)


class TestTimeframeInference:
    @pytest.mark.parametrize(
        ("freq", "expected"),
        [("1min", Timeframe.M1), ("5min", Timeframe.M5),
         ("15min", Timeframe.M15), ("60min", Timeframe.H1)],
    )
    def test_intraday_spacings(self, freq: str, expected: Timeframe) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=30, freq=freq, tz="Asia/Kolkata")
        assert infer_timeframe(index) is expected

    def test_daily_spacing(self) -> None:
        index = pd.date_range("2025-01-01", periods=30, freq="D", tz="Asia/Kolkata")
        assert infer_timeframe(index) is Timeframe.D1

    def test_the_modal_gap_wins_over_overnight_gaps(self) -> None:
        # Two sessions of 15m bars: most gaps are 15m, one is 17.75 hours.
        first = pd.date_range("2025-08-04 09:15", periods=25, freq="15min", tz="Asia/Kolkata")
        second = pd.date_range("2025-08-05 09:15", periods=25, freq="15min", tz="Asia/Kolkata")
        assert infer_timeframe(first.append(second)) is Timeframe.M15

    def test_too_few_bars_yields_no_guess(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=2, freq="15min", tz="Asia/Kolkata")
        assert infer_timeframe(index) is None


class TestDailyConformance:
    def test_midnight_dated_bars_move_to_the_session_open(self) -> None:
        index = pd.date_range("2025-08-04", periods=5, freq="D", tz="Asia/Kolkata")
        frame = pd.DataFrame({"close": [1.0] * 5}, index=index)
        conformed, note = conform_daily_to_session_open(frame)
        assert note is not None
        assert conformed.index[0].hour == 9
        assert conformed.index[0].minute == 15

    def test_bars_with_real_times_are_left_alone(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=5, freq="D", tz="Asia/Kolkata")
        frame = pd.DataFrame({"close": [1.0] * 5}, index=index)
        conformed, note = conform_daily_to_session_open(frame)
        assert note is None
        assert conformed.index.equals(index)


class TestDirectoryScan:
    def test_it_separates_readable_from_unreadable(self, tmp_path: Path) -> None:
        write_csv(tmp_path / "good.csv", intraday_rows())
        write_csv(tmp_path / "bad.csv", {"a": [1], "b": [2]})
        scan = scan_directory(tmp_path)
        assert len(scan.loaded) == 1
        assert len(scan.failed) == 1
        assert scan.failed[0][0].name == "bad.csv"

    def test_it_recurses_into_subdirectories(self, tmp_path: Path) -> None:
        nested = tmp_path / "2025" / "08"
        nested.mkdir(parents=True)
        write_csv(nested / "RELIANCE.csv", intraday_rows())
        assert len(scan_directory(tmp_path).loaded) == 1

    def test_files_can_be_selected_by_symbol(self, tmp_path: Path) -> None:
        write_csv(tmp_path / "RELIANCE_15min.csv", intraday_rows())
        write_csv(tmp_path / "TCS_15min.csv", intraday_rows())
        scan = scan_directory(tmp_path)
        assert len(scan.for_symbol("RELIANCE")) == 1
        assert len(scan.for_symbol("reliance")) == 1
        assert scan.for_symbol("INFY") == []

    def test_a_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            scan_directory(tmp_path / "nope")

    def test_non_tabular_files_are_recorded_as_skipped(self, tmp_path: Path) -> None:
        write_csv(tmp_path / "good.csv", intraday_rows())
        (tmp_path / "notes.pdf").write_bytes(b"%PDF-1.4")
        scan = scan_directory(tmp_path)
        assert [path.name for path in scan.skipped] == ["notes.pdf"]


class TestThirtyMinuteBars:
    """30-minute bars do not divide the NSE session evenly.

    375 minutes / 30 = 12.5, so a session is twelve full bars plus a fifteen
    minute stub at 15:15. Kite Connect serves exactly that layout. A timeframe
    table that assumed clean division would either reject the stub or silently
    drop the last half hour of every session.
    """

    def test_thirty_minute_spacing_is_recognised(self) -> None:
        index = pd.date_range("2026-06-15 09:15", periods=13, freq="30min", tz="Asia/Kolkata")
        assert infer_timeframe(index) is Timeframe.M30

    def test_the_session_is_twelve_full_bars_plus_a_stub(self, calendar) -> None:
        import datetime as dt

        starts = calendar.bar_starts(dt.date(2026, 6, 15), Timeframe.M30)
        assert len(starts) == 13
        assert starts[0].time() == dt.time(9, 15)
        assert starts[-1].time() == dt.time(15, 15)
        assert calendar.is_partial_bar(starts[-1], Timeframe.M30)
        assert not calendar.is_partial_bar(starts[-2], Timeframe.M30)


class TestParquet:
    """Parquet is the right container for a full index history.

    Fifty symbols across five timeframes is tens of millions of rows. Parquet
    stores that at roughly a tenth of CSV's size with dtypes preserved, and it
    removes the whole class of text-parsing hazards this module guards against:
    a Parquet timestamp carries its own type and timezone, so there is no
    DD/MM ambiguity to resolve and no delimiter to sniff.
    """

    def test_a_timestamp_column_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "TRENT_15minute.parquet"
        pd.DataFrame(intraday_rows()).to_parquet(path, index=False)
        result = load_vendor_file(path)
        assert result.symbol_hint == "TRENT"
        assert result.timeframe is Timeframe.M15
        assert result.frame["close"].iloc[0] == pytest.approx(100.0)

    def test_a_datetime_index_is_promoted_to_a_column(self, tmp_path: Path) -> None:
        """Parquet writers routinely persist the bar timestamp as the index.

        Left there, the column mapper sees only OHLCV and rejects the file for
        having no timestamp -- so a whole vendor's export reads as unloadable
        for a reason that has nothing to do with its contents.
        """
        frame = pd.DataFrame(intraday_rows())
        frame["Datetime"] = pd.to_datetime(frame["Datetime"])
        path = tmp_path / "WIPRO_minute.parquet"
        frame.set_index("Datetime").to_parquet(path)
        result = load_vendor_file(path)
        assert result.rows == 40
        assert result.symbol_hint == "WIPRO"
        assert str(result.frame.index.tz) == "Asia/Kolkata"

    def test_scan_directory_picks_up_parquet(self, tmp_path: Path) -> None:
        pd.DataFrame(intraday_rows()).to_parquet(tmp_path / "A_15minute.parquet", index=False)
        write_csv(tmp_path / "B_15minute.csv", intraday_rows())
        scan = scan_directory(tmp_path)
        assert sorted(r.symbol_hint or "" for r in scan.loaded) == ["A", "B"]

    def test_the_csv_alias_still_works(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "C.csv", intraday_rows())
        assert load_vendor_csv(path).rows == 40


class TestSymbolExtraction:
    """Real NSE symbols contain characters that naive splitting destroys.

    BAJAJ-AUTO has a hyphen and M&M an ampersand. Splitting the filename on
    every separator reduced "BAJAJ-AUTO_15minute" to "BAJAJ" -- a symbol that
    does not exist. The panel then carries a phantom name and silently drops
    the real one, and nothing raises, because "BAJAJ" is a perfectly plausible
    string.
    """

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("BAJAJ-AUTO_15minute.parquet", "BAJAJ-AUTO"),
            ("M&M_15minute.parquet", "M&M"),
            ("BAJAJ-AUTO_day.csv", "BAJAJ-AUTO"),
            ("SBIN_15minute.parquet", "SBIN"),
            ("WIPRO_minute.parquet", "WIPRO"),
            ("TCS_60minute.parquet", "TCS"),
            ("RELIANCE_day.csv", "RELIANCE"),
            ("INFY.parquet", "INFY"),
        ],
    )
    def test_the_symbol_survives_the_filename(
        self, tmp_path: Path, filename: str, expected: str
    ) -> None:
        path = tmp_path / filename
        if path.suffix == ".parquet":
            pd.DataFrame(intraday_rows()).to_parquet(path, index=False)
        else:
            write_csv(path, intraday_rows())
        assert load_vendor_file(path).symbol_hint == expected

    def test_a_non_timeframe_suffix_is_kept(self, tmp_path: Path) -> None:
        # Only a recognised timeframe token is stripped; anything else is part
        # of the name, because guessing wrong invents a symbol.
        path = tmp_path / "SOMECO_LTD.csv"
        write_csv(path, intraday_rows())
        assert load_vendor_file(path).symbol_hint == "SOMECO_LTD"
