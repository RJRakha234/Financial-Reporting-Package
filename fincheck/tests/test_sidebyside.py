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


def test_the_column_header_names_the_file_and_how_it_was_made(tmp_path):
    a = make_pdf(tmp_path / "quarterly.pdf", STATEMENT)
    b = make_pdf(tmp_path / "filed.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert ">quarterly<" in html and ">filed<" in html
    # PyMuPDF stamps its own producer, so the subtitle is populated.
    assert "<i>" in html


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


# --------------------------------------------------------------------------
# Documents that are not financial statements
# --------------------------------------------------------------------------


def test_a_single_money_column_is_found_by_right_edge_alignment(tmp_path):
    """Figures that line up down the page form a column, wherever they sit.

    A statement with one narrow money column keeps it well left of centre, so
    asking whether a figure sits past some fraction of the page width missed it
    and reduced the whole statement to prose.
    """
    lines = [
        "Umsatzerloese          1.234.567,89",
        "Materialaufwand          456.789,12",
        "Personalaufwand          321.456,78",
    ]
    blocks = segment(make_pdf(tmp_path / "a.pdf", lines))
    tables = [b for b in blocks if b.kind == "table"]

    assert len(tables) == 1
    assert len(tables[0].rows) == 3
    assert [f.value for r in tables[0].rows for f in r.figures] == [
        1234567.89,
        456789.12,
        321456.78,
    ]


def test_a_leading_clause_number_is_not_read_as_a_table_cell(tmp_path):
    """Clause numbers align down the page too, but a cell follows its label."""
    lines = [
        "1.1 'Services' means the professional services described in a",
        "1.2 'Deliverables' means any work product furnished to the Client",
        "2.1 This Agreement commences on the Effective Date and continues",
    ]
    blocks = segment(make_pdf(tmp_path / "a.pdf", lines))

    assert all(b.kind == "paragraph" for b in blocks)
    assert not any(r.figures for b in blocks for r in b.rows)


def test_a_contract_amendment_is_reported(tmp_path):
    original = [
        "2. TERM AND TERMINATION",
        "2.1 This Agreement continues for a period of thirty-six (36) months.",
        "2.2 Either party may terminate for material breach on 30 days notice.",
    ]
    amended = [
        l.replace("thirty-six (36)", "twenty-four (24)").replace("30 days", "60 days")
        for l in original
    ]
    a = make_pdf(tmp_path / "a.pdf", original)
    b = make_pdf(tmp_path / "b.pdf", amended)

    pairs, _, summary = compare(a, b)
    changed = [p for p in pairs if p.a and p.b and p.changed]

    assert summary.only_in_a == 0 and summary.only_in_b == 0
    # The clauses run on consecutively, so they are one paragraph, and both
    # amendments show up as word-level edits inside it.
    assert len(changed) == 1
    added = " ".join(t for op, t in changed[0].words if op == "+")
    removed = " ".join(t for op, t in changed[0].words if op == "-")
    assert "twenty-four" in added and "60" in added
    assert "thirty-six" in removed and "30" in removed


def test_a_page_with_no_text_layer_yields_nothing_to_align(tmp_path):
    """A scan has no paragraphs to pair; only the exact pixel layer can speak."""
    doc = fitz.open()
    doc.new_page().insert_text((60, 100), "content", fontsize=12, fontname="helv")
    src = tmp_path / "src.pdf"
    doc.save(str(src))
    doc.close()

    rendered = fitz.open(str(src))[0].get_pixmap(dpi=72)
    out = fitz.open()
    page = out.new_page()
    page.insert_image(page.rect, pixmap=rendered)
    scan = tmp_path / "scan.pdf"
    out.save(str(scan))
    out.close()

    _, sections, summary = compare(str(scan), str(scan))

    assert summary.units_a == 0
    assert sections == []


def test_columns_are_named_after_the_file(tmp_path):
    """The filename is what a reader recognises, so it is the heading.

    Naming the columns after the producer instead ("HTML print", "Word export")
    is real information but nobody recognises their own file by it, and it left
    readers wondering which document they were looking at.
    """
    from fincheck.sidebyside import default_label, produced_by

    assert default_label("/x/statements_q2.pdf") == "statements_q2"
    assert default_label("/x/q2.pdf", "Microsoft® Excel® for Microsoft 365") == "q2"

    # How it was made is kept, as a subtitle rather than a heading.
    assert produced_by("Microsoft® Excel® for Microsoft 365") == "Excel export"
    assert produced_by("Skia/PDF m150", "Chrome/150") == "HTML print"
    assert produced_by("") == ""


def test_the_page_labels_its_two_columns(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out), label_a="Source PDF", label_b="HTML PDF")
    html = out.read_text()

    assert ">Source PDF<" in html
    assert ">HTML PDF<" in html
    assert "colhead" in html, "the column names should be a sticky header"


def test_short_tags_are_derived_for_the_stacked_layout(tmp_path):
    from fincheck.sidebyside import _short_tag

    assert _short_tag("Excel export") == "XLS"
    assert _short_tag("HTML print") == "HTML"
    assert _short_tag("Source PDF") == "SP"
    assert _short_tag("statements") == "STAT"


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


def test_a_sentence_ending_on_a_year_is_not_a_one_column_table(tmp_path):
    """Two documents wrapping the same sentence differently must still agree.

    "...PEAK Matrix Assessment 2025" ended on a bare year, so it read as a
    one-column table row; the other document wrapped the sentence elsewhere, so
    the same year was mid-line and stayed prose — and the figure then looked
    like it had disappeared.
    """
    sentence = "Positioned as a leader in the Everest Group PEAK Matrix Assessment 2025"
    wrapped = ["Positioned as a leader in the Everest Group PEAK Matrix",
               "Assessment 2025 and again the following year"]

    a = segment(make_pdf(tmp_path / "a.pdf", [sentence], width=842))
    b = segment(make_pdf(tmp_path / "b.pdf", wrapped, width=420))

    assert all(x.kind == "paragraph" for x in a)
    assert all(x.kind == "paragraph" for x in b)
    assert not any(r.figures for x in a for r in x.rows)


def test_a_short_labelled_year_column_still_reads_as_a_table(tmp_path):
    """The guard must not swallow a genuine period column."""
    blocks = segment(make_pdf(tmp_path / "a.pdf", ["Year ended           2025"]))

    assert [b.kind for b in blocks] == ["table"]
    assert blocks[0].values == [2025.0]


# --------------------------------------------------------------------------
# Numbered copies of the source PDFs, and links back to them
# --------------------------------------------------------------------------


def test_each_section_is_numbered_and_stamped_onto_both_sources(tmp_path):
    """The report says what differs; the copies say where it came from."""
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )
    out_a, out_b = tmp_path / "a.marked.pdf", tmp_path / "b.marked.pdf"

    result = side_by_side(
        a, b, output_html=str(tmp_path / "sbs.html"),
        marked_pdf_a=str(out_a), marked_pdf_b=str(out_b),
    )

    assert result.marked_pdfs == [str(out_a), str(out_b)]
    assert out_a.is_file() and out_b.is_file()
    # Serials run 1..N in report order, so a number in the report finds a
    # number on the page.
    serials = [s.serial for s in result.sections]
    assert serials == list(range(1, len(result.sections) + 1))
    for path in (out_a, out_b):
        doc = fitz.open(str(path))
        try:
            assert "1" in doc[0].get_text(), "the serial should be stamped on the page"
        finally:
            doc.close()


def test_a_section_knows_its_region_on_each_side(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)

    result = side_by_side(a, b)
    table = next(s for s in result.sections if s.kind == "table")

    regions = table.regions("a")
    assert regions, "the section must know where it sits"
    assert table.first_page("a") == 1
    x0, y0, x1, y1 = regions[1]
    assert x1 > x0 and y1 > y0


def test_page_numbers_link_into_the_numbered_copy(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(
        a, b, output_html=str(out),
        marked_pdf_a=str(tmp_path / "a.marked.pdf"),
        marked_pdf_b=str(tmp_path / "b.marked.pdf"),
    )
    html = out.read_text()

    # Relative, so the report and its copies can be moved together.
    assert 'href="a.marked.pdf#page=1' in html
    assert 'href="b.marked.pdf#page=1' in html
    assert "numbered and located" in html


def test_cells_link_to_the_exact_spot_in_the_marked_copy(tmp_path):
    """Not just the page: the copy opens scrolled to the passage itself.

    ``#page=N&zoom=scale,left,top`` is the PDF open-parameter syntax; ``top``
    is in PDF coordinates, so it must come out of the page height, not the
    extraction's top-down y.
    """
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(
        a, b, output_html=str(out),
        marked_pdf_a=str(tmp_path / "a.marked.pdf"),
        marked_pdf_b=str(tmp_path / "b.marked.pdf"),
    )
    html = out.read_text()

    assert "&amp;zoom=100,0," in html, "links carry the exact spot, not just the page"
    assert 'class="loc"' in html, "the cell content itself is the link"
    # The first table row sits near the top of an 842pt page, so its
    # destination must be near the top in PDF coordinates too (y up).
    import re

    tops = [int(m) for m in re.findall(r"zoom=100,0,(\d+)", html)]
    assert tops and all(0 <= t <= 842 for t in tops)
    assert max(tops) > 700, "content near the page top lands near height in PDF coords"


def test_every_section_offers_a_tracked_changes_view(tmp_path):
    """A Word-style redline: benchmark text struck where it was not carried over."""
    a = make_pdf(tmp_path / "a.pdf", ["The term is thirty-six months in total."])
    b = make_pdf(tmp_path / "b.pdf", ["The term is twenty-four months in total."])
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    # The view selector and the three alternate views.
    assert 'name="view"' in html
    assert "Tracked changes" in html and "Before" in html and "After" in html
    # The tracked pane carries the redline: what should have been there,
    # struck through, beside what is there.
    assert '<div class="side tracked"><p class="para">' in html
    assert "<del>thirty-six</del>" in html and "<ins>twenty-four</ins>" in html


def test_a_deviating_figure_row_gets_a_tracked_cell_and_a_tooltip(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    # The tracked cell shows old-struck, new-inserted, in one run of figures.
    assert '<td class="side tracked">' in html
    assert "<del>10,106</del><ins>10,999</ins>" in html
    # And each deviating cell names both readings without leaving the page.
    assert 'title="benchmark: 10,106 · compared: 10,999"' in html


def test_the_report_embeds_the_marked_pages_for_in_page_preview(tmp_path):
    """Clicking a cell must work even where PDF open-parameters do not."""
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(
        a, b, output_html=str(out),
        marked_pdf_a=str(tmp_path / "a.marked.pdf"),
        marked_pdf_b=str(tmp_path / "b.marked.pdf"),
    )
    html = out.read_text()

    assert '<script id="previews" type="application/json">' in html
    assert "data:image/" in html
    # Every spot link carries the region to spotlight on the rendered page.
    assert 'data-peek="a:1:' in html and 'data-peek="b:1:' in html


def test_no_pages_are_embedded_without_marked_copies(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert 'id="previews"' not in html
    assert 'data-peek="' not in html


def test_a_column_missing_from_the_benchmark_still_renders(tmp_path):
    """The tooltip must not assume the benchmark side has the value.

    A compared document can carry a column the benchmark lacks; formatting a
    ``None`` crashed the whole report.
    """
    a = make_pdf(tmp_path / "a.pdf", ["Balance " + "  ".join(f"{i:,}00" for i in range(1, 11))],
                 width=842)
    b = make_pdf(tmp_path / "b.pdf", ["Balance " + "  ".join(f"{i:,}00" for i in range(1, 12))],
                 width=842)
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))

    assert "absent" in out.read_text()


def test_composite_scores_follow_the_reviewer_bands(tmp_path):
    """100 exact; 99 formatting-only; <99 real change; 0 one-sided."""
    a = make_pdf(tmp_path / "a.pdf", STATEMENT + [None, "Guidance for FY26 : strong",
                                                  None, "an extra closing note only here"])
    changed = [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT]
    b = make_pdf(tmp_path / "b.pdf", changed + [None, "Guidance for FY26: strong"])

    pairs, _, summary = compare(a, b)
    by_status = {}
    for p in pairs:
        by_status.setdefault(p.status, p)

    assert by_status["same"].score == 100
    assert by_status["formatting"].score == 99
    assert 1 <= by_status["figures-differ"].score <= 98
    assert by_status["removed"].score == 0
    assert 0 < summary.overall < 100


def test_a_moved_section_is_flagged_but_still_matched(tmp_path):
    """Content-based matching pairs a relocated section; the flag says it moved."""
    from fincheck.align import flag_moved

    para = ["The Group operates in one reportable segment and evaluates",
            "performance on a consolidated basis every quarter."]
    other = ["Basic earnings per share is computed by dividing net profit",
             "by the weighted average number of shares outstanding."]
    third = ["The financial statements were authorised for issue by the",
             "board of directors at its meeting held in July."]
    gap = [None, None]
    a = make_pdf(tmp_path / "a.pdf", para + gap + other + gap + third)
    b = make_pdf(tmp_path / "b.pdf", other + gap + third + gap + para)

    result = side_by_side(a, b, use_marks=False, auto_sections=False)
    moved = [s for s in result.sections if s.moved]

    assert result.summary.moved == len(moved) >= 1
    for section in moved:
        assert all(p.a is not None and p.b is not None for p in section.pairs)


def test_the_report_shows_scores_and_the_composite(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert 'class="scorechip' in html, "each section shows its match score"
    assert f"<b>{result.summary.overall}%</b>" in html, "the seal shows the composite"
    assert 'title="match ' in html, "each segment's score is inspectable"


def test_the_auditor_cli_form_writes_the_named_outputs(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "source.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "compared.pdf",
        [l.replace("54,613      51,804", "54,613      51,900") for l in STATEMENT],
    )
    out = tmp_path / "results"

    code = main([
        "compare", "--source", str(a), "--compared", str(b),
        "--output", str(out),
    ])
    printed = capsys.readouterr().out

    assert code == 1  # the documents deviate
    assert (out / "comparison_report.html").is_file()
    assert (out / "source_annotated.pdf").is_file()
    assert (out / "compared_annotated.pdf").is_file()
    assert "Composite match with benchmark:" in printed


def test_the_left_document_is_presented_as_the_benchmark(tmp_path):
    a = make_pdf(tmp_path / "source.pdf", STATEMENT)
    b = make_pdf(tmp_path / "filed.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert 'class="btag"' in html, "the benchmark column is tagged"
    assert "benchmark" in html
    assert 'class="seal' in html, "the agreement seal is drawn"
    assert "Faithful to the benchmark" in html


def test_no_links_are_written_when_no_copies_were_asked_for(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert result.marked_pdfs == []
    assert "#page=" not in html
    assert "numbered and located" not in html


def test_the_cli_writes_both_copies_beside_the_report(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "quarterly.pdf", STATEMENT)
    b = make_pdf(
        tmp_path / "filed.pdf",
        [l.replace("54,613      51,804", "54,613      51,900") for l in STATEMENT],
    )
    out = tmp_path / "sbs.html"

    code = main(
        ["compare", a, b, "-o", "none", "--no-render",
         "--side-by-side", str(out), "--marked-pdfs"]
    )
    printed = capsys.readouterr().out

    assert code == 1
    assert (tmp_path / "quarterly.marked.pdf").is_file()
    assert (tmp_path / "filed.marked.pdf").is_file()
    assert "Numbered copy written to" in printed


def test_the_copies_show_what_the_report_covered(tmp_path):
    """Untinted content is content the report did not compare — visibly so."""
    from fincheck.sidemarks import coverage

    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)

    result = side_by_side(
        a, b, output_html=str(tmp_path / "sbs.html"),
        marked_pdf_a=str(tmp_path / "a.marked.pdf"),
        marked_pdf_b=str(tmp_path / "b.marked.pdf"),
    )

    passages, figures = coverage(result.sections, "a")
    assert passages > 0
    assert figures == 6, "both figures on each of the three money rows"

    html = (tmp_path / "sbs.html").read_text()
    assert "not</em> covered by this report" in html


def test_each_copy_carries_the_report_numbering_in_its_bookmarks(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", STATEMENT)
    b = make_pdf(tmp_path / "b.pdf", STATEMENT)

    side_by_side(
        a, b,
        marked_pdf_a=str(tmp_path / "a.marked.pdf"),
        marked_pdf_b=str(tmp_path / "b.marked.pdf"),
    )

    doc = fitz.open(str(tmp_path / "a.marked.pdf"))
    try:
        toc = doc.get_toc()
        assert toc, "a reader should be able to jump to a serial"
        assert toc[0][1].startswith("1."), "bookmarks are numbered as the report is"
    finally:
        doc.close()


def test_the_same_words_punctuated_differently_are_not_an_edit(tmp_path):
    """"FY26 :" and "FY26:" split into different tokens, but nothing changed.

    Reported as a deletion and an insertion, the whole of FY26 appeared to have
    been cut — alarming, and wrong.
    """
    from fincheck.align import diff_words

    ops = diff_words(
        "Guidance for FY26 : · Revenue growth of 1%-3%",
        "Guidance for FY26: • Revenue growth of 1%-3%",
    )

    assert not any(op in ("-", "+") for op, _ in ops), "nothing added or removed"
    assert any(op.startswith("~") for op, _ in ops), "the difference is still shown"
    # Each side keeps its own text, so the typographic difference is visible.
    assert ("~-", "FY26 : ·") in ops
    assert ("~+", "FY26: •") in ops
    # The word itself survives intact on both sides.
    assert "FY26" in " ".join(t for _, t in ops)


def test_a_formatting_difference_is_not_counted_as_a_change(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", ["Guidance for FY26 : Revenue growth of 1%-3%"])
    b = make_pdf(tmp_path / "b.pdf", ["Guidance for FY26: Revenue growth of 1%-3%"])

    result = side_by_side(a, b)
    pair = next(p for s in result.sections for p in s.pairs if p.a and p.b)

    assert pair.status == "formatting"
    assert pair.formatting_only
    assert not pair.changed
    assert result.summary.changed == 0
    assert result.summary.formatting == 1


def test_a_real_wording_change_is_still_a_change(tmp_path):
    """The quieter treatment must not swallow an actual edit."""
    a = make_pdf(tmp_path / "a.pdf", ["Revenue growth of 1%-3% in constant currency"])
    b = make_pdf(tmp_path / "b.pdf", ["Revenue growth of 2%-4% in constant currency"])

    result = side_by_side(a, b)
    pair = next(p for s in result.sections for p in s.pairs if p.a and p.b)

    assert pair.status == "changed"
    assert pair.changed
    assert any(op == "-" for op, _ in pair.words)
