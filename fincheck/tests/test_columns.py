"""Tests for reading a table's columns off the page.

A PDF states no table structure. These fixtures draw cells at chosen positions,
right-aligned as a financial statement sets them, so what is under test is the
geometry the tool has to work from rather than a structure handed to it.
"""

import fitz
import pytest

from fincheck.align import group, units_of, align
from fincheck.blocks import segment
from fincheck.columns import build_grid, grid_for, match_columns
from fincheck.sidebyside import _table_layout


def make_table(path, headings, rows, columns, label_x=60, size=9):
    """Draw a table with its headings and figures right-aligned to ``columns``.

    ``rows`` are ``(label, [(column index, text), ...])``, so a line item can
    state one amount under Total and leave the rest of the row empty, the way a
    segment note does.
    """
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    y = 80

    def right(text, x, at):
        width = fitz.get_text_length(text, fontname="helv", fontsize=size)
        page.insert_text((x - width, at), text, fontsize=size, fontname="helv")

    for line in headings:
        for index, text in line:
            if index is None:
                page.insert_text((label_x, y), text, fontsize=size, fontname="helv")
            else:
                right(text, columns[index], y)
        y += 13
    for label, cells in rows:
        if label:
            page.insert_text((label_x, y), label, fontsize=size, fontname="helv")
        for index, text in cells:
            right(text, columns[index], y)
        y += 13

    doc.save(str(path))
    doc.close()
    return str(path)


COLUMNS = [300, 400, 500, 600, 720]
LINES = [
    ("Revenue from operations", ["11796", "6804", "5742", "5651", "29993"]),
    ("Identifiable operating expenses", ["6662", "4274", "3281", "2914", "17131"]),
    ("Segment operating income", ["2973", "1416", "1437", "1691", "7517"]),
]


def statement(path, order, columns=COLUMNS):
    """The same statement with its segment columns printed in ``order``.

    The figures stay in the same places on the page; only the headings move, so
    a document that shuffles its columns states different amounts against the
    same names while reading identically down the page.
    """
    names = ["Financial Services", "Manufacturing", "Retail", "Communication"]
    headings = [[(None, "Particulars")]]
    headings[0] += [(i, names[order[i]]) for i in range(4)]
    headings[0].append((4, "Total"))
    rows = [(label, list(enumerate(cells))) for label, cells in LINES]
    return make_table(path, headings, rows, columns)


# --------------------------------------------------------------------------
# Recovering the grid
# --------------------------------------------------------------------------


def test_a_tables_columns_are_recovered_from_where_its_figures_end(tmp_path):
    blocks = segment(statement(tmp_path / "a.pdf", [0, 1, 2, 3]))
    table = next(b for b in blocks if b.kind == "table")

    grid = build_grid(table.rows)

    assert len(grid) == 5
    for edge, drawn in zip(grid.edges, COLUMNS):
        assert edge == pytest.approx(drawn, abs=1.5)


def test_the_headings_above_a_table_are_read_onto_its_columns(tmp_path):
    blocks = segment(statement(tmp_path / "a.pdf", [0, 1, 2, 3]))
    table = next(b for b in blocks if b.kind == "table")

    grid = grid_for(table)

    assert grid.headings == [
        "Financial Services",
        "Manufacturing",
        "Retail",
        "Communication",
        "Total",
    ]
    assert grid.label == "Particulars"
    assert grid.is_named


def test_a_wrapped_heading_is_joined_back_together(tmp_path):
    path = make_table(
        tmp_path / "a.pdf",
        [
            [(1, "Energy,")],
            [(0, "Financial"), (1, "Utilities and")],
            [(None, "Particulars"), (0, "Services"), (1, "Resources"), (2, "Total")],
        ],
        [("Revenue", [(0, "11796"), (1, "6804"), (2, "18600")])],
        [300, 400, 500],
    )

    grid = grid_for(next(b for b in segment(path) if b.kind == "table"))

    assert grid.headings[1] == "Energy, Utilities and Resources"


def test_a_footnote_marker_on_a_heading_is_not_part_of_the_name(tmp_path):
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Particulars"), (0, "Retail(2)"), (1, "Total")]],
        [("Revenue", [(0, "11796"), (1, "11796")])],
        [300, 400],
    )

    grid = grid_for(next(b for b in segment(path) if b.kind == "table"))

    assert grid.headings[0] == "Retail"


def test_a_line_of_footnote_markers_is_not_a_row_of_negative_figures(tmp_path):
    """"(1) (4) (5)" over a segment table is three notes, not -1, -4 and -5."""
    path = make_table(
        tmp_path / "a.pdf",
        [
            [(0, "(1)"), (1, "(4)"), (2, "(5)")],
            [(None, "Particulars"), (0, "Retail"), (1, "Hi-Tech"), (2, "Total")],
        ],
        [("Revenue", [(0, "11796"), (1, "6804"), (2, "18600")])],
        [300, 400, 500],
    )

    figures = [f.value for b in segment(path) for f in b.figures]

    assert -1.0 not in figures and -4.0 not in figures and -5.0 not in figures


def test_the_title_above_a_table_is_not_mistaken_for_its_headings(tmp_path):
    """A short line at the label column heads the section, not the columns."""
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Balance Sheet")]],
        [("Property, plant and equipment", [(0, "11596"), (1, "11778")])],
        [300, 400],
    )
    block = next(b for b in segment(path) if b.kind == "table")

    assert not grid_for(block).is_named
    # …so the line stays in the prose, where it can still title the section.
    titles = [u.text for u in units_of(segment(path)) if u.kind == "paragraph"]
    assert "Balance Sheet" in titles


# --------------------------------------------------------------------------
# Placing a row into the grid
# --------------------------------------------------------------------------


def test_a_figure_standing_alone_under_total_is_placed_under_total(tmp_path):
    """Read by position it lands under the first segment, beside amounts it has
    nothing to do with, and every column below it is then out of step."""
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Particulars"), (0, "Retail"), (1, "Hi-Tech"), (2, "Total")]],
        [
            ("Revenue", [(0, "11796"), (1, "6804"), (2, "18600")]),
            ("Unallocable expenses", [(2, "1140")]),
        ],
        [300, 400, 500],
    )
    table = next(b for b in segment(path) if b.kind == "table")
    grid = grid_for(table)
    row = next(r for r in table.rows if r.label == "Unallocable expenses")

    assert grid.place(row) == [None, None, 1140.0]


# --------------------------------------------------------------------------
# Keeping a table a table
# --------------------------------------------------------------------------


def test_a_dash_inside_a_label_does_not_make_the_row_prose(tmp_path):
    """"units - carried at fair value" is punctuation, not a nil cell.

    Read as a figure the dash puts a number in the middle of the line, the row
    reads as prose, and its amounts end up strung through a sentence.
    """
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Particulars"), (0, "June 30"), (1, "March 31")]],
        [("Liquid mutual fund units - carried at fair value", [(0, "3510"), (1, "1957")])],
        [300, 400],
    )

    table = next(b for b in segment(path) if b.kind == "table")

    assert [f.value for f in table.figures] == [3510.0, 1957.0]


def test_a_dash_standing_in_a_column_is_still_a_nil_cell(tmp_path):
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Particulars"), (0, "June 30"), (1, "March 31")]],
        [("Commercial papers", [(0, "-"), (1, "3641")])],
        [300, 400],
    )

    table = next(b for b in segment(path) if b.kind == "table")

    assert [f.value for f in table.figures] == [0.0, 3641.0]


def test_a_row_too_long_to_be_a_label_is_kept_by_its_alignment(tmp_path):
    """A middle column of prose runs past any length a label can be given.

    Rejected for length, the whole table dissolves into a paragraph with its
    amounts strung through it. The figures still stand in their columns, and
    that is what says the line belongs to the table.
    """
    long_label = (
        "Tax free bonds and government bonds carried at amortized cost "
        "Quoted price and market observable inputs"
    )
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Class of investment"), (0, "June 30"), (1, "March 31")]],
        [
            (long_label, [(0, "1408"), (1, "1812")]),
            ("Quoted price", [(0, "5956"), (1, "4869")]),
        ],
        [640, 740],
    )

    table = next(b for b in segment(path) if b.kind == "table")
    labels = [r.label for r in table.rows]

    assert any(r.startswith("Tax free bonds") for r in labels)
    assert [f.value for f in table.figures] == [1408.0, 1812.0, 5956.0, 4869.0]


def test_a_sentence_ending_on_a_figure_is_still_a_sentence(tmp_path):
    """The rescue must not sweep prose near a table into it."""
    path = make_table(
        tmp_path / "a.pdf",
        [[(None, "Particulars"), (0, "June 30"), (1, "March 31")]],
        [("Cash and cash equivalents", [(0, "1408"), (1, "1812")])],
        [300, 400],
    )
    doc = fitz.open(path)
    page = doc[0]
    page.insert_text(
        (60, 160),
        "The Group holds no further commitments as at June 30, 2025 and 2024.",
        fontsize=9,
        fontname="helv",
    )
    doc.save(str(tmp_path / "b.pdf"))
    doc.close()

    prose = " ".join(
        b.text for b in segment(str(tmp_path / "b.pdf")) if b.kind == "paragraph"
    )

    assert "no further commitments" in prose


# --------------------------------------------------------------------------
# Matching two documents' columns
# --------------------------------------------------------------------------


def compare(a, b):
    ua, ub = units_of(segment(a)), units_of(segment(b))
    return group(align(ua, ub))


def test_columns_printed_in_the_same_order_are_left_alone(tmp_path):
    a = statement(tmp_path / "a.pdf", [0, 1, 2, 3])
    b = statement(tmp_path / "b.pdf", [0, 1, 2, 3], columns=[280, 390, 495, 610, 700])
    section = next(s for s in compare(a, b) if s.kind == "table")

    assert section.reordered_columns == []
    assert all(p.status == "same" for p in section.pairs if p.kind == "row")


def test_a_statement_that_shuffles_its_columns_is_not_a_perfect_match(tmp_path):
    """The figures read identically down the page, so position says they agree.

    They do not: the same amounts stand against different segments, which is the
    difference that matters and the one comparing by position cannot see.
    """
    a = statement(tmp_path / "a.pdf", [0, 1, 2, 3])
    b = statement(tmp_path / "b.pdf", [0, 2, 3, 1])
    section = next(s for s in compare(a, b) if s.kind == "table")

    assert "Manufacturing" in section.reordered_columns
    rows = [p for p in section.pairs if p.kind == "row"]
    assert rows and all(p.status == "figures-differ" for p in rows)
    assert section.status == "changed"


def test_the_reordered_columns_are_named_in_the_report(tmp_path):
    a = statement(tmp_path / "a.pdf", [0, 1, 2, 3])
    b = statement(tmp_path / "b.pdf", [0, 2, 3, 1])
    section = next(s for s in compare(a, b) if s.kind == "table")

    layout = _table_layout(section)

    assert "Columns reordered" in layout.notice
    assert "Manufacturing" in layout.notice
    assert ">Financial Services</span>" in layout.head
    assert "fc--moved" in layout.head


def test_columns_are_named_even_when_nothing_moved(tmp_path):
    a = statement(tmp_path / "a.pdf", [0, 1, 2, 3])
    b = statement(tmp_path / "b.pdf", [0, 1, 2, 3])
    section = next(s for s in compare(a, b) if s.kind == "table")

    layout = _table_layout(section)

    assert layout.notice == ""
    assert ">Total</span>" in layout.head


def test_two_tables_with_different_columns_fall_back_to_position(tmp_path):
    a = statement(tmp_path / "a.pdf", [0, 1, 2, 3])
    b = make_table(
        tmp_path / "b.pdf",
        [[(None, "Particulars"), (0, "Group"), (1, "Total")]],
        [("Revenue from operations", [(0, "11796"), (1, "11796")])],
        [300, 400],
    )
    grid_a = grid_for(next(x for x in segment(a) if x.kind == "table"))
    grid_b = grid_for(next(x for x in segment(b) if x.kind == "table"))

    match = match_columns(grid_a, grid_b)

    assert not match.named and not match.reordered
