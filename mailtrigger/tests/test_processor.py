import json

from mailtrigger.processor import process


def test_auto_detects_json():
    data = b'{"a": 1, "b": {"c": 2}}'
    out = process(data, "application/json", {"type": "auto"})
    assert out.ext == "json"
    assert json.loads(out.content)["a"] == 1


def test_json_field_selection():
    data = b'{"data": {"total": 42, "rows": [1, 2]}, "noise": 9}'
    out = process(data, "application/json", {"type": "json", "json_fields": ["data.total"]})
    assert json.loads(out.content) == {"data.total": 42}


def test_auto_detects_csv():
    data = b"Date,Amount,Note\n2026-01-01,100,x\n"
    out = process(data, "text/csv", {"type": "auto"})
    assert out.ext == "csv"


def test_csv_column_selection():
    data = b"Date,Amount,Note\n2026-01-01,100,x\n"
    out = process(data, "text/csv", {"type": "csv", "csv_columns": ["Date", "Amount"]})
    text = out.content.decode()
    assert "Note" not in text
    assert "Date,Amount" in text


def test_raw_passthrough():
    data = b"\x00\x01rawbytes"
    out = process(data, "application/octet-stream", {"type": "auto"})
    assert out.ext == "bin"
    assert out.content == data
