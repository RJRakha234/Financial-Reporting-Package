"""Tests for the reconstructed side-by-side comparison.

Fixtures are built with PyMuPDF so each test controls exactly how the content is
drawn — which matters here, because the whole point is that the two documents
lay the same content out differently.
"""

import fitz
import pytest

from fincheck import side_by_side
from fincheck.align import group, units_of, align, summarise
from fincheck.blocks import segment

STATEMENT = [
    "Condensed Consolidated Balance Sheet",
    "ASSETS",
    "Non-current assets",
    "Property, plant and equipment            11,596      11,778",
    "Goodwill                                 11,502      10,106",
    "Total non-current assets                 54,613      51,804",
]


def make_pdf(path, lines, width=595, height=842, size=10, start=80, pitch=16):
    """Draw each line as one show-text operation, as a spreadsheet export does."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    y = start
    for line in lines:
        if line is None:  # a blank line, i.e. a block break
            y += pitch
            continue
        if y > height - 60:
            page = doc.new_page(width=width, height=height)
            y = start
        page.insert_text((60, y), line, fontsize=size, fontname="helv")
        y += pitch
    doc.save(str(path))
    doc.close()
    return str(path)


def compare(a, b):
    ua, ub = units_of(segment(a)), units_of(segment(b))
    pairs = align(ua, ub)
    return pairs, group(pairs), summarise(pairs, ua, ub)


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------


def test_figures_are_found_even_when_a_row_is_one_text_operation(tmp_path):
    """A spreadsheet export writes a whole row at once; per-span parsing fails."""
    blocks = segment(make_pdf(tmp_path / "a.pdf", STATEMENT))
    tables = [b for b in blocks if b.kind == "table"]

    assert tables, "the statement rows should form a table"
    ppe = next(r for b in tables for r in b.rows if "Property" in r.label)
    assert [f.value for f in ppe.figures] == [11596.0, 11778.0]
    assert ppe.label == "Property, plant and equipment"


def test_prose_keeps_a_figure_quoted_mid_sentence(tmp_path):
    lines = [
        "The Group completed two business combinations during the period for a",
        "total consideration of 1,234 crore, excluding contingent consideration.",
    ]
    blocks = segment(make_pdf(tmp_path / "a.pdf", lines))

    assert [b.kind for b in blocks] == ["paragraph"]
    assert "1,234" in blocks[0].text
    assert blocks[0].figures == []


def test_a_section_heading_inside_a_table_does_not_split_it(tmp_path):
    """"Current assets" sits between figure rows; the table must survive it."""
    lines = STATEMENT + [
        "Current assets",
        "Trade receivables                        33,968      31,158",
        "Total current assets                     78,627      74,309",
    ]
    blocks = segment(make_pdf(tmp_path / "a.pdf", lines))
    tables = [b for b in blocks if b.kind == "table"]

    assert len(tables) == 1, "the interior heading must not start a second table"
    assert "Current assets" in [r.label for r in tables[0].rows]
    assert len([r for r in tables[0].rows if r.is_figure_row]) == 5


def test_the_page_folio_is_not_glued_onto_a_paragraph(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "Some narrative text about the period.", fontsize=10,
                     fontname="helv")
    page.insert_text((300, 800), "7", fontsize=9, fontname="helv")  # centred folio
    path = tmp_path / "a.pdf"
    doc.save(str(path))
    doc.close()

    blocks = segment(str(path))

    assert len(blocks) == 1
    assert "7" not in blocks[0].text


# --------------------------------------------------------------------------
# Matching content across different layouts
# --------------------------------------------------------------------------


def test_a_paragraph_matches_though_the_two_files_wrap_it_differently(tmp_path):
    sentence = (
        "The Group uses the percentage-of-completion method in accounting for "
        "other fixed-price contracts, which requires the use of estimates."
    )
    wide = make_pdf(tmp_path / "a.pdf", [sentence], width=842)
    narrow = make_pdf(
        tmp_path / "b.pdf",
        ["The Group uses the percentage-of-completion",
         "method in accounting for other fixed-price",
         "contracts, which requires the use of estimates."],
        width=420,
    )

    pairs, _, summary = compare(wide, narrow)
    matched = [p for p in pairs if p.a and p.b and p.kind == "paragraph"]

    assert len(matched) == 1
    assert matched[0].status == "same"
    assert summary.changed == 0


def test_table_rows_match_though_one_file_splits_the_table_across_pages(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    # Same rows, but forced onto two pages with a heading repeated at the break.
    b = make_pdf(
        tmp_path / "b.pdf",
        STATEMENT[:4] + [None] * 40 + STATEMENT[4:],
        height=400,
    )

    pairs, _, summary = compare(a, b)
    rows = [p for p in pairs if p.kind == "row" and p.a and p.b]

    assert len(rows) >= 3
    assert all(p.status == "same" for p in rows)
    assert summary.changed_figures == 0


# --------------------------------------------------------------------------
# Detecting differences
# --------------------------------------------------------------------------


def test_a_changed_figure_is_reported_with_both_values(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    changed = [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT]
    b = make_pdf(tmp_path / "b.pdf", changed)

    pairs, _, summary = compare(a, b)
    differing = [p for p in pairs if p.status == "figures-differ"]

    assert len(differing) == 1
    assert differing[0].changed_figures == [(1, 10106.0, 10999.0)]
    assert summary.changed_figures == 1


def test_a_dropped_column_is_reported_as_a_column_difference(tmp_path):
    """The case that matters: a wide statement losing its rightmost columns."""
    wide = [
        "Balance as at April 1              2,071      68,405      88,116",
        "Profit for the period                  0      12,874      12,874",
    ]
    clipped = [line.rsplit(None, 1)[0] for line in wide]
    a = make_pdf(tmp_path / "a.pdf", wide, width=842)
    b = make_pdf(tmp_path / "b.pdf", clipped, width=842)

    pairs, _, _ = compare(a, b)
    differing = [p for p in pairs if p.status == "columns-differ"]

    assert len(differing) == 2
    for pair in differing:
        assert len(pair.a.values) - len(pair.b.values) == 1
        # The lost column reports B as absent rather than as a changed value.
        assert pair.changed_figures[-1][2] is None


def test_a_row_present_in_only_one_document_is_reported_as_such(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        STATEMENT[:5] + ["Other intangible assets   3,168   2,766"] + STATEMENT[5:],
    )

    pairs, _, summary = compare(a, b)
    added = [p for p in pairs if p.status == "added" and p.a is None]

    assert any("intangible" in p.b.text for p in added)
    assert summary.only_in_b >= 1


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def test_an_inserted_row_does_not_shatter_the_table_into_sections(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        STATEMENT[:4] + ["Right-of-use assets   6,390   6,311"] + STATEMENT[4:],
    )

    _, sections, _ = compare(a, b)
    tables = [s for s in sections if s.kind == "table"]

    # One table on each side, so one section — not three around the insertion.
    assert len(tables) == 1
    assert any(p.a is None for p in tables[0].pairs)


def test_a_table_is_titled_from_the_heading_above_it(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)

    _, sections, _ = compare(a, b)
    table = next(s for s in sections if s.kind == "table")

    assert "Balance Sheet" in table.title


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_html_is_written_and_shows_both_documents(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    changed = [l.replace("54,613      51,804", "54,613      51,900") for l in STATEMENT]
    b = make_pdf(tmp_path / "b.pdf", changed)
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert result.output_html == str(out)
    assert "a.pdf" in html and "b.pdf" in html
    # Both values of the changed figure appear, and the change is flagged.
    assert "51,804" in html and "51,900" in html
    assert "fig--changed" in html
    # The page must say that its structure is inferred.
    assert "infers structure" in html
    # Theme-aware in both directions.
    assert "prefers-color-scheme" in html
    assert 'data-theme="dark"' in html or "data-theme=dark" in html


def test_wide_tables_stack_a_over_b_instead_of_scrolling(tmp_path):
    wide = ["Balance " + "  ".join(f"{i:,}00" for i in range(1, 12))]
    a = make_pdf(tmp_path / "a.pdf", wide, width=842)
    b = make_pdf(tmp_path / "b.pdf", wide, width=842)
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert 'class="stack"' in html, "a wide table should use the stacked layout"


def test_figure_changes_are_available_on_the_result(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,596      11,778", "11,596      11,700") for l in STATEMENT],
    )

    result = side_by_side(a, b)
    changes = result.figure_changes()

    assert len(changes) == 1
    pair, cells = changes[0]
    assert cells == [(1, 11778.0, 11700.0)]
    assert "Property" in pair.a.text


def test_cli_writes_the_side_by_side_page(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )
    out = tmp_path / "sbs.html"

    code = main(
        ["compare", a, b, "-o", "none", "--no-render", "--side-by-side", str(out)]
    )
    printed = capsys.readouterr().out

    assert code == 1  # the documents differ
    assert out.is_file()
    assert "Side-by-side written to" in printed
    assert "worksheet" in printed


def test_identical_documents_produce_no_differences(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)

    pairs, sections, summary = compare(a, b)

    assert summary.changed == 0
    assert summary.changed_figures == 0
    assert summary.only_in_a == 0
    assert summary.only_in_b == 0
    assert all(s.status == "same" for s in sections)
    assert summary.matched == summary.unchanged
