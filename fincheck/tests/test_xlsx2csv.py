"""Tests for the Excel-workbook -> CSV converter."""

import csv
import datetime as dt

import pytest

openpyxl = pytest.importorskip("openpyxl")

from fincheck.xlsx2csv import convert_workbook, main


def _read(path, encoding="utf-8-sig"):
    with open(path, newline="", encoding=encoding) as fh:
        return list(csv.reader(fh))


def _make_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Income Statement"
    ws.append(["Item", "Amount"])
    ws.append(["Revenue", 1000])
    ws.append(["Cost", 400])
    ws.append(["Profit", "=B2-B3"])  # formula; cached value set below

    ws2 = wb.create_sheet("Notes & Misc")
    ws2["A1"] = "As at"
    ws2["B1"] = dt.datetime(2026, 3, 31)
    ws2["A2"] = "Audited"
    ws2["B2"] = True

    wb.create_sheet("Empty")
    wb.save(path)


def _make_workbook_with_cached_formula(path):
    # Round-trip through openpyxl can't compute formulas, so we inject a cached
    # value by re-saving with data_only semantics simulated: write the value.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "x"
    ws["A2"] = 1234.0  # integer-valued float -> "1234"
    ws["A3"] = 3.5
    wb.save(path)


def test_converts_each_sheet_to_csv(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)

    written = convert_workbook(book, tmp_path / "out")
    names = sorted(p.name for p in written)

    # Empty sheet skipped by default; names slugified.
    assert names == ["Income_Statement.csv", "Notes_Misc.csv"]


def test_values_dates_and_bools(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    out = tmp_path / "out"
    convert_workbook(book, out)

    income = _read(out / "Income_Statement.csv")
    assert income[0] == ["Item", "Amount"]
    assert income[1] == ["Revenue", "1000"]

    notes = _read(out / "Notes_Misc.csv")
    assert notes[0] == ["As at", "2026-03-31"]  # datetime at midnight -> date
    assert notes[1] == ["Audited", "TRUE"]


def test_integer_floats_render_without_point(tmp_path):
    book = tmp_path / "nums.xlsx"
    _make_workbook_with_cached_formula(book)
    out = tmp_path / "out"
    convert_workbook(book, out)
    rows = _read(out / "Sheet1.csv")
    assert rows == [["x"], ["1234"], ["3.5"]]


def test_single_sheet_selection(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    written = convert_workbook(book, tmp_path / "out", sheet="Notes & Misc")
    assert [p.name for p in written] == ["Notes_Misc.csv"]


def test_unknown_sheet_raises(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    with pytest.raises(KeyError):
        convert_workbook(book, tmp_path / "out", sheet="Nope")


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert_workbook(tmp_path / "ghost.xlsx", tmp_path / "out")


def test_keep_empty_sheets(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    written = convert_workbook(
        book, tmp_path / "out", skip_empty_sheets=False
    )
    assert any(p.name == "Empty.csv" for p in written)


def test_no_clobber(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    out = tmp_path / "out"
    convert_workbook(book, out)
    with pytest.raises(FileExistsError):
        convert_workbook(book, out, overwrite=False)


def test_delimiter_and_default_output_dir(tmp_path):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    written = convert_workbook(book, delimiter=";")  # default <stem>_csv dir
    assert written[0].parent == tmp_path / "book_csv"
    with open(written[0], encoding="utf-8-sig") as fh:
        assert ";" in fh.readline()


def test_merged_cells_repeat_and_ragged_rows_pad(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "Header"
    ws.merge_cells("A1:C1")  # value should repeat across A,B,C
    ws["A2"] = "a"
    ws["D2"] = "d"  # wider row forces padding of row 1
    book = tmp_path / "m.xlsx"
    wb.save(book)

    convert_workbook(book, tmp_path / "out")
    rows = _read(tmp_path / "out" / "S.csv")
    assert rows[0] == ["Header", "Header", "Header", ""]
    assert rows[1] == ["a", "", "", "d"]


def test_cli(tmp_path, capsys):
    book = tmp_path / "book.xlsx"
    _make_workbook(book)
    out = tmp_path / "out"
    rc = main([str(book), "-o", str(out)])
    assert rc == 0
    assert (out / "Income_Statement.csv").is_file()
    assert "Wrote 2 CSV" in capsys.readouterr().out
