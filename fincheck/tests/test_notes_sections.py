from fincheck.extract import Cell, Page, Row
from fincheck.notes.sections import (
    narrative_text,
    split_into_sections,
)


def _row(label, label_x0=70.0, cells=None, top=0.0):
    return Row(
        page_index=0,
        top=top,
        bottom=top + 10,
        label=label,
        label_x0=label_x0,
        cells=cells or {},
    )


def _num_cell(text, x0):
    return Cell(column=0, value=0.0, text=text, x0=x0, x1=x0 + 10, top=0.0, bottom=10.0)


def _page(rows):
    return Page(index=0, width=600, height=800, rows=rows, n_columns=1)


def test_heading_with_left_number_cell():
    # "2.7 TRADE RECEIVABLES" extracts as label "TRADE RECEIVABLES" + cell "2.7".
    rows = [
        _row("TRADE RECEIVABLES", label_x0=78, cells={0: _num_cell("2.7", 78)}),
        _row("Some long narrative sentence describing the receivables policy here."),
    ]
    secs = split_into_sections([_page(rows)])
    assert len(secs) == 1
    assert secs[0].number == "2.7"
    assert secs[0].title == "TRADE RECEIVABLES"


def test_heading_with_embedded_glyph_prefix():
    rows = [_row("XXX2.1 BUSINESS COMBINATIONS"), _row("X10AO2.7 Property, plant and equipment")]
    secs = split_into_sections([_page(rows)])
    assert [s.number for s in secs] == ["2.1", "2.7"]
    assert secs[0].title == "BUSINESS COMBINATIONS"
    assert secs[1].title == "Property, plant and equipment"


def test_toc_and_container_headings_are_not_sections():
    rows = [
        _row("2.7 Trade receivables ……………………………… 14"),  # TOC leader line
        _row("1. Overview"),  # one-level container heading
        _row("Some intro text that should attach to nothing yet."),
    ]
    secs = split_into_sections([_page(rows)])
    assert secs == []


def test_narrative_drops_table_scaffolding():
    rows = [
        _row("INVESTMENTS", label_x0=78, cells={0: _num_cell("2.4", 78)}),
        _row("Investments are measured at fair value through profit or loss in line with policy."),
        _row("Particulars", cells={0: _num_cell("100", 400)}),  # table data row
        _row("Current", label_x0=80),  # short label beside a table row
        _row("(In ₹ crore)"),  # units caption
    ]
    secs = split_into_sections([_page(rows)])
    text = narrative_text(secs[0])
    assert "Investments are measured at fair value" in text
    assert "Particulars" not in text
    assert "Current" not in text
    assert "crore" not in text
