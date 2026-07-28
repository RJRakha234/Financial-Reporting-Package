import csv
import json
from pathlib import Path

from finround.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "segment_revenue.csv"


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return str(path)


SIMPLE = [
    ["Segment", "H1", "H2", "Total"],
    ["North", "33.3", "33.3", "66.6"],
    ["South", "33.4", "33.4", "66.8"],
    ["Total", "66.7", "66.7", "133.4"],
]


def test_reads_writes_and_reports(tmp_path, capsys):
    source = write_csv(tmp_path / "in.csv", SIMPLE)
    out = tmp_path / "out.csv"
    assert main([source, "-o", str(out)]) == 0

    printed = capsys.readouterr().out
    assert "foot exactly after rounding" in printed
    assert str(out) in printed

    written = list(csv.reader(out.open()))
    assert written[0] == ["", "H1", "H2", "Total"]
    body = {row[0]: [int(v) for v in row[1:]] for row in written[1:]}
    assert body["Total"] == [
        body["North"][0] + body["South"][0],
        body["North"][1] + body["South"][1],
        body["North"][2] + body["South"][2],
    ]
    for row in ("North", "South", "Total"):
        assert body[row][2] == body[row][0] + body[row][1]


def test_json_output_is_machine_readable(tmp_path, capsys):
    source = write_csv(tmp_path / "in.csv", SIMPLE)
    assert main([source, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["consistent"] is True
    assert payload["violations"] == []
    assert payload["row_groups"] == {"2": [0, 1]}
    assert payload["max_cell_movement_steps"] < 1


def test_scale_and_decimals_from_the_command_line(tmp_path, capsys):
    source = write_csv(
        tmp_path / "in.csv",
        [["Item", "FY25"], ["A", "1234567"], ["B", "2345678"], ["Total", "3580245"]],
    )
    assert main([source, "--scale", "1000", "-d", "1", "--quiet"]) == 0
    assert "1,234.5" in capsys.readouterr().out


def test_totals_given_by_position(tmp_path, capsys):
    source = write_csv(
        tmp_path / "in.csv",
        [["Item", "V"], ["A", "1.4"], ["B", "1.4"], ["Closing balance", "2.8"]],
    )
    assert main([source, "--total-rows", "3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["row_groups"] == {"2": [0, 1]}


def test_missing_file_is_an_error_not_a_traceback(capsys):
    assert main(["no_such_file.csv"]) == 2
    assert "finround:" in capsys.readouterr().err


def test_the_shipped_example_foots(capsys):
    assert main([str(EXAMPLE), "--scale", "10000000", "-d", "2"]) == 0
    assert "24 total checks foot exactly" in capsys.readouterr().out
