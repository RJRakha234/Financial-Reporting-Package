"""Turning a directory of downloaded vendor files into a replay root.

The download tool writes ``SYMBOL_INTERVAL.parquet`` flat; the replay adapter
reads ``EXCHANGE/SYMBOL/timeframe.parquet``. Everything here is about the gap
between those two conventions, and the one detail that is not merely cosmetic:
a futures contract filed under NSE instead of NFO is a symbol the adapter will
never find.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from nifty50.domain import IST, Exchange, InstrumentKind
from nifty50.scripts.build_replay import classify, main


def vendor_file(path: Path, *, minutes: int = 15, rows: int = 40, daily: bool = False) -> None:
    start = dt.datetime(2026, 8, 7, 9, 15, tzinfo=IST)
    step = dt.timedelta(days=1) if daily else dt.timedelta(minutes=minutes)
    index = pd.DatetimeIndex([start + step * i for i in range(rows)], name="date")
    price = pd.Series([100.0 + i * 0.1 for i in range(rows)], index=index)
    frame = pd.DataFrame(
        {
            "open": price, "high": price + 0.5, "low": price - 0.5,
            "close": price, "volume": [1000] * rows,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def read_manifest(dest: Path) -> dict[str, dict[str, str]]:
    with (dest / "instruments.csv").open("r", encoding="utf-8", newline="") as handle:
        return {row["symbol"]: row for row in csv.DictReader(handle)}


class TestClassify:
    def test_futures_go_to_nfo(self) -> None:
        """Filed under NSE, the replay adapter would never find them."""
        assert classify("NIFTY26OCTFUT") == (Exchange.NFO, InstrumentKind.FUTURE)
        assert classify("SBINFUT") == (Exchange.NFO, InstrumentKind.FUTURE)

    def test_indices_are_not_equities(self) -> None:
        """An index has no volume and cannot be held; sizing it is meaningless."""
        assert classify("NIFTY 50") == (Exchange.NSE, InstrumentKind.INDEX)
        assert classify("BANKNIFTY") == (Exchange.NSE, InstrumentKind.INDEX)

    def test_ordinary_names_are_nse_equity(self) -> None:
        for symbol in ("RELIANCE", "BAJAJ-AUTO", "M&M", "TCS"):
            assert classify(symbol) == (Exchange.NSE, InstrumentKind.EQUITY)


class TestBuild:
    def test_it_lays_files_out_where_the_adapter_looks(self, tmp_path) -> None:
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet", minutes=15)
        vendor_file(source / "NIFTY26OCTFUT_15minute.parquet", minutes=15)

        assert main(["--source", str(source), "--dest", str(dest)]) == 0
        assert (dest / "NSE" / "RELIANCE" / "15m.parquet").is_file()
        assert (dest / "NFO" / "NIFTY26OCTFUT" / "15m.parquet").is_file()

        manifest = read_manifest(dest)
        assert manifest["RELIANCE"]["exchange"] == "NSE"
        assert manifest["NIFTY26OCTFUT"]["kind"] == "future"

    def test_a_second_run_does_not_drop_the_first_run_s_symbols(self, tmp_path) -> None:
        """Building the root one download at a time is the normal case:
        equities today, futures next week."""
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet")
        main(["--source", str(source), "--dest", str(dest)])

        later = tmp_path / "dl2"
        vendor_file(later / "TCS_15minute.parquet")
        main(["--source", str(later), "--dest", str(dest)])

        assert set(read_manifest(dest)) == {"RELIANCE", "TCS"}

    def test_hand_edited_broker_tokens_survive_a_rebuild(self, tmp_path) -> None:
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet")
        main(["--source", str(source), "--dest", str(dest)])

        path = dest / "instruments.csv"
        path.write_text(
            path.read_text(encoding="utf-8").replace(",1,", ",738561,"), encoding="utf-8"
        )
        main(["--source", str(source), "--dest", str(dest), "--overwrite"])
        assert read_manifest(dest)["RELIANCE"]["token"] == "738561"

    def test_existing_bars_are_kept_unless_overwrite_is_asked_for(self, tmp_path) -> None:
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet", rows=40)
        main(["--source", str(source), "--dest", str(dest)])

        vendor_file(source / "RELIANCE_15minute.parquet", rows=10)
        main(["--source", str(source), "--dest", str(dest)])
        assert len(pd.read_parquet(dest / "NSE" / "RELIANCE" / "15m.parquet")) == 40

        main(["--source", str(source), "--dest", str(dest), "--overwrite"])
        assert len(pd.read_parquet(dest / "NSE" / "RELIANCE" / "15m.parquet")) == 10

    def test_only_the_ohlcv_contract_is_carried_across(self, tmp_path) -> None:
        """Vendor files arrive with open interest, a symbol column and an
        unnamed index; the adapter reads positionally enough that a stray
        column is a hazard."""
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet")
        frame = pd.read_parquet(source / "RELIANCE_15minute.parquet")
        frame["oi"] = 12345
        frame["symbol"] = "RELIANCE"
        frame.to_parquet(source / "RELIANCE_15minute.parquet")

        main(["--source", str(source), "--dest", str(dest)])
        written = pd.read_parquet(dest / "NSE" / "RELIANCE" / "15m.parquet")
        assert list(written.columns) == ["open", "high", "low", "close", "volume"]

    def test_an_empty_source_is_an_error_not_an_empty_root(self, tmp_path) -> None:
        source, dest = tmp_path / "dl", tmp_path / "replay"
        source.mkdir()
        assert main(["--source", str(source), "--dest", str(dest)]) == 1

    def test_a_missing_source_directory_is_reported(self, tmp_path) -> None:
        assert main(["--source", str(tmp_path / "nope"), "--dest", str(tmp_path / "r")]) == 2

    def test_symbols_can_be_filtered(self, tmp_path) -> None:
        source, dest = tmp_path / "dl", tmp_path / "replay"
        vendor_file(source / "RELIANCE_15minute.parquet")
        vendor_file(source / "TCS_15minute.parquet")
        main(["--source", str(source), "--dest", str(dest), "--symbols", "TCS"])
        assert set(read_manifest(dest)) == {"TCS"}


class TestFromStore:
    """The route that needs no loose files: backfill, then export.

    The store is Hive-partitioned and the replay adapter is not, which is the
    only reason an export step exists at all.
    """

    def store_with(self, root: Path, symbol: str = "RELIANCE"):  # type: ignore[no-untyped-def]
        from nifty50.data.store import BarStore
        from nifty50.domain import Timeframe

        store = BarStore(root)
        start = dt.datetime(2026, 8, 7, 9, 15, tzinfo=IST)
        index = pd.DatetimeIndex(
            [start + dt.timedelta(minutes=5 * i) for i in range(75)], name="ts"
        )
        price = pd.Series([100.0 + i * 0.1 for i in range(75)], index=index)
        frame = pd.DataFrame(
            {
                "open": price, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": [1000] * 75,
            }
        )
        store.write(Exchange.NSE, symbol, Timeframe.M5, frame)
        return store

    def test_it_exports_the_store_into_the_replay_layout(self, tmp_path) -> None:
        store, dest = tmp_path / "store", tmp_path / "replay"
        self.store_with(store)
        assert main(["--from-store", "--store", str(store), "--dest", str(dest)]) == 0
        assert (dest / "NSE" / "RELIANCE" / "5m.parquet").is_file()
        assert read_manifest(dest)["RELIANCE"]["exchange"] == "NSE"

    def test_an_empty_store_points_at_backfill(self, tmp_path, capsys) -> None:
        store, dest = tmp_path / "store", tmp_path / "replay"
        store.mkdir()
        assert main(["--from-store", "--store", str(store), "--dest", str(dest)]) == 1
        assert "backfill" in capsys.readouterr().err

    def test_source_and_from_store_are_mutually_exclusive(self, tmp_path) -> None:
        """Two origins for the same destination is a question, not a default."""
        with pytest.raises(SystemExit):
            main(["--from-store", "--source", str(tmp_path)])

    def test_one_origin_is_required(self) -> None:
        with pytest.raises(SystemExit):
            main([])


@pytest.mark.parametrize("interval,expected", [("5minute", "5m"), ("60minute", "1h"), ("day", "1d")])
def test_the_bar_size_is_inferred_from_the_bars_not_the_filename(
    tmp_path, interval: str, expected: str
) -> None:
    """The filename is a hint, not evidence. Kite writes ``60minute`` and the
    engine calls it ``1h``; a file mislabelled by the downloader would
    otherwise be filed under a timeframe it is not."""
    source, dest = tmp_path / "dl", tmp_path / "replay"
    minutes = {"5minute": 5, "60minute": 60, "day": 0}[interval]
    vendor_file(
        source / f"RELIANCE_{interval}.parquet",
        minutes=minutes, rows=30, daily=interval == "day",
    )
    main(["--source", str(source), "--dest", str(dest)])
    assert (dest / "NSE" / "RELIANCE" / f"{expected}.parquet").is_file()
