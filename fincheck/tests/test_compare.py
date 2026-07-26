"""Tests for the exact two-PDF comparison.

Fixtures are built with PyMuPDF so every test controls precisely what the file
draws — which is what the comparison claims to compare.
"""

import fitz
import pytest

from fincheck import compare
from fincheck.compare import compare_pdfs
from fincheck.compare_report import comparison_to_console, comparison_to_dict

ROWS = [
    (72, 100, "Inventories"),
    (400, 100, "6,800"),
    (72, 120, "Trade receivables"),
    (400, 120, "3,400"),
    (72, 140, "Total current assets"),
    (400, 140, "10,200"),
]


def make_pdf(path, rows=ROWS, draw=None, metadata=None, extra_pages=0):
    doc = fitz.open()
    page = doc.new_page()
    for x, y, text in rows:
        page.insert_text((x, y), text, fontsize=11, fontname="helv")
    if draw is not None:
        draw(page)
    for _ in range(extra_pages):
        doc.new_page().insert_text((72, 100), "appendix", fontsize=11, fontname="helv")
    if metadata is not None:
        doc.set_metadata(metadata)
    doc.save(str(path))
    doc.close()
    return str(path)


def edited(rows, old, new):
    return [(x, y, new if text == old else text) for x, y, text in rows]


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_same_file_is_identical_byte_for_byte(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = tmp_path / "b.pdf"
    b.write_bytes((tmp_path / "a.pdf").read_bytes())

    result = compare_pdfs(a, str(b))

    assert result.bytes_identical
    assert result.identical
    assert result.text_identical
    assert result.content_identical
    assert result.visually_identical
    assert result.changed_pages == []


def test_same_content_different_bytes_is_identical_document(tmp_path):
    """Metadata-only differences must not read as a content change."""
    a = make_pdf(tmp_path / "a.pdf", metadata={"title": "draft"})
    b = make_pdf(tmp_path / "b.pdf", metadata={"title": "final"})

    result = compare_pdfs(a, b)

    assert not result.bytes_identical
    assert result.content_identical
    assert result.visually_identical
    assert result.identical
    assert result.changed_pages == []


# --------------------------------------------------------------------------
# Text differences
# --------------------------------------------------------------------------


def test_changed_figure_is_reported_with_exact_delta(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "10,100"))

    result = compare_pdfs(a, b)

    assert not result.identical
    assert not result.text_identical
    numeric = result.numeric_changes
    assert len(numeric) == 1
    _, change = numeric[0]
    assert change.kind == "edited"
    assert (change.before.value, change.after.value) == (10200.0, 10100.0)
    assert change.delta == -100.0


def test_changed_figure_carries_its_position_and_nearby_label(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "10,100"))

    _, change = compare_pdfs(a, b, dpi=None).numeric_changes[0]

    assert change.after.y0 == pytest.approx(change.before.y0)
    assert change.context == "Total current assets"


def test_inserted_row_shifts_rather_than_rewrites_everything_below(tmp_path):
    """A single insertion must not cascade into an edit per following row."""
    a = make_pdf(tmp_path / "a.pdf")
    shifted = [
        (x, y + 20 if y >= 120 else y, text) for x, y, text in ROWS
    ] + [(72, 120, "Prepayments"), (400, 120, "500")]
    b = make_pdf(tmp_path / "b.pdf", rows=shifted)

    result = compare_pdfs(a, b, dpi=None)
    kinds = [c.kind for c in result.pages[0].span_changes]

    assert sorted(kinds) == ["added", "added", "moved", "moved", "moved", "moved"]
    assert result.numeric_changes == []
    added = {
        c.after.text.strip()
        for c in result.pages[0].span_changes
        if c.kind == "added"
    }
    assert added == {"Prepayments", "500"}


def test_moved_text_is_not_reported_as_added_and_removed(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=[(x + 30, y, t) for x, y, t in ROWS])

    changes = compare_pdfs(a, b, dpi=None).pages[0].span_changes

    assert changes
    assert {c.kind for c in changes} == {"moved"}


def test_restyled_text_differs_even_though_the_characters_match(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = tmp_path / "b.pdf"

    doc = fitz.open()
    page = doc.new_page()
    for x, y, text in ROWS:
        font = "hebo" if text == "Total current assets" else "helv"
        page.insert_text((x, y), text, fontsize=11, fontname=font)
    doc.save(str(b))
    doc.close()

    result = compare_pdfs(a, str(b), dpi=None)

    assert result.text_identical  # same characters
    assert not result.content_identical  # different font
    assert [c.kind for c in result.pages[0].span_changes] == ["restyled"]


# --------------------------------------------------------------------------
# Graphics and pixels
# --------------------------------------------------------------------------


def test_added_ruling_line_is_caught_though_the_text_is_identical(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(
        tmp_path / "b.pdf",
        draw=lambda page: page.draw_line(
            fitz.Point(390, 130), fitz.Point(460, 130), width=0.5
        ),
    )

    result = compare_pdfs(a, b)

    assert result.text_identical
    assert not result.content_identical
    assert result.visually_identical is False
    assert result.pages[0].paths_added == 1
    assert result.pages[0].paths_removed == 0


def test_pixel_regions_locate_the_change_on_the_page(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "99,900"))

    pixels = compare_pdfs(a, b, dpi=100).pages[0].pixels

    assert not pixels.identical
    assert pixels.changed_pixels > 0
    assert pixels.regions
    # The only edit sits on the y=140 baseline, so every changed region must.
    assert all(120 <= r.y0 and r.y1 <= 160 for r in pixels.regions)


def test_scanned_pages_are_compared_by_pixels_and_image_hash(tmp_path):
    """With no text layer at all, the image and pixel layers still answer."""

    def scan(path, ink):
        page_pdf = make_pdf(tmp_path / "src.pdf")
        pix = fitz.open(page_pdf)[0].get_pixmap(dpi=72)
        data = bytearray(pix.samples)
        for y in range(40, 60):
            for x in range(40, 120):
                offset = y * pix.stride + x * pix.n
                data[offset : offset + pix.n] = bytes([ink]) * pix.n
        painted = fitz.Pixmap(fitz.csRGB, pix.width, pix.height, bytes(data), False)
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, pixmap=painted)
        doc.save(str(path))
        doc.close()
        return str(path)

    a = scan(tmp_path / "a.pdf", 0x10)
    b = scan(tmp_path / "b.pdf", 0x90)

    result = compare_pdfs(a, b, dpi=100)

    assert result.text_identical  # neither file has any text
    assert result.spans_identical
    assert not result.graphics_identical  # the embedded image hash differs
    assert result.visually_identical is False
    assert not result.identical
    assert result.pages[0].images_added == 1
    assert result.pages[0].images_removed == 1
    # The layer labels must not blame the text for an image-only change.
    console = comparison_to_console(result)
    assert "text, every character ............... same" in console
    assert "vector graphics and images .......... DIFFERS" in console


def test_changed_pixel_count_matches_a_pixel_by_pixel_scan():
    """The C-level counter must agree with the obvious slow implementation."""
    import random

    from fincheck.compare import _count_changed_pixels

    random.seed(7)
    for _ in range(200):
        n = random.choice([1, 2, 3, 4])
        pixels = random.randint(1, 80)
        a = bytes(random.randrange(256) for _ in range(pixels * n))
        b = bytearray(a)
        for _ in range(random.randint(0, pixels)):
            b[random.randrange(pixels * n)] ^= random.randrange(1, 256)
        b = bytes(b)

        brute = sum(1 for i in range(0, pixels * n, n) if a[i : i + n] != b[i : i + n])
        assert _count_changed_pixels(a, b, n) == brute


def test_a_wholly_retypeset_page_is_left_unmarked_not_marked_partly(tmp_path):
    """Past the cap a page was re-typeset, not edited; marking it teaches nothing."""
    from fincheck.diffmark import write_diff_pdf

    a = make_pdf(
        tmp_path / "a.pdf",
        rows=[(72, 60 + i * 9, f"left row {i}") for i in range(80)],
    )
    b = make_pdf(
        tmp_path / "b.pdf",
        rows=[(300, 65 + i * 9, f"right row {i}") for i in range(80)],
    )

    result = compare_pdfs(a, b, dpi=None)
    assert len(result.pages[0].span_changes) == 160  # every span differs

    markup = write_diff_pdf(result, str(tmp_path / "diff.pdf"), max_marks_per_page=25)

    assert (markup.unmarked_pages, markup.omitted_changes) == (1, 160)
    doc = fitz.open(markup.path)
    try:
        assert len(list(doc[1].annots())) == 0
        assert "unmarked" in doc[0].get_text()
    finally:
        doc.close()


def test_a_lightly_edited_page_is_marked_in_full(tmp_path):
    """The cap must not bite on the case the mark-up exists for."""
    from fincheck.diffmark import write_diff_pdf

    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "10,100"))

    result = compare_pdfs(a, b, dpi=None)
    markup = write_diff_pdf(result, str(tmp_path / "diff.pdf"))

    assert (markup.unmarked_pages, markup.omitted_changes) == (0, 0)
    doc = fitz.open(markup.path)
    try:
        assert len(list(doc[1].annots())) == len(result.pages[0].span_changes)
    finally:
        doc.close()


def test_skipping_the_render_leaves_visual_equality_undetermined(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", metadata={"title": "other"})

    result = compare_pdfs(a, b, dpi=None)

    assert result.visually_identical is None
    assert not result.pixels_compared
    assert "not checked" in comparison_to_console(result)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def test_extra_page_is_reported_rather_than_silently_ignored(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", extra_pages=1)

    result = compare_pdfs(a, b, dpi=None)

    assert not result.identical
    assert (result.page_count_a, result.page_count_b) == (1, 2)
    assert not result.page_counts_equal
    assert [p.only_in_one for p in result.pages] == [False, True]


def test_alignment_matches_pages_around_an_insertion(tmp_path):
    """With a page prepended, page 1 of A is page 2 of B."""
    a = make_pdf(tmp_path / "a.pdf")

    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "cover", fontsize=11, fontname="helv")
    doc.insert_pdf(fitz.open(a))
    b = tmp_path / "b.pdf"
    doc.save(str(b))
    doc.close()

    strict = compare_pdfs(a, str(b), dpi=None)
    aligned = compare_pdfs(a, str(b), dpi=None, align_pages=True)

    # Positionally, page 1 of A is compared with the cover and differs wholesale.
    assert strict.pages[0].span_changes
    # Aligned, the shared page matches exactly and only the cover is unpaired.
    matched = [p for p in aligned.pages if not p.only_in_one]
    assert len(matched) == 1
    assert matched[0].identical
    assert [p.page_b for p in aligned.pages if p.only_in_one] == [0]


# --------------------------------------------------------------------------
# Tolerance
# --------------------------------------------------------------------------


def test_positions_are_compared_exactly_by_default(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    nudged = [(x + 0.4, y, text) for x, y, text in ROWS]
    b = make_pdf(tmp_path / "b.pdf", rows=nudged)

    exact = compare_pdfs(a, b, dpi=None)
    slack = compare_pdfs(a, b, dpi=None, position_tolerance=2.0)

    assert not exact.content_identical
    assert exact.text_identical  # the characters never changed
    assert slack.content_identical


# --------------------------------------------------------------------------
# Reporting and the public API
# --------------------------------------------------------------------------


def test_report_dict_records_the_verdict_of_every_layer(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "6,800", "6,900"))

    payload = comparison_to_dict(compare_pdfs(a, b, dpi=100))

    verdict = payload["verdict"]
    assert verdict["identical"] is False
    assert verdict["bytes_identical"] is False
    assert verdict["geometry_identical"] is True
    assert verdict["text_identical"] is False
    assert verdict["visually_identical"] is False
    assert payload["changed_page_count"] == 1
    change = payload["pages"][0]["text_changes"][0]
    assert change["value_change"] == {
        "before": 6800.0,
        "after": 6900.0,
        "delta": 100.0,
    }


def test_compare_writes_a_marked_up_pdf(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "6,800", "6,900"))
    out = tmp_path / "diff.pdf"

    result = compare(a, b, output_pdf=str(out), dpi=None)

    assert not result.identical
    assert out.is_file()
    doc = fitz.open(str(out))
    try:
        # One prepended summary page, and the edit annotated on the content page.
        assert doc.page_count == 2
        assert len(list(doc[1].annots())) >= 1
        assert "Exact PDF Comparison" in doc[0].get_text()
    finally:
        doc.close()


def test_console_report_leads_with_the_verdict(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "10,100"))

    text = comparison_to_console(compare_pdfs(a, b, dpi=100))

    assert "NOT identical" in text
    assert "Figures that changed (1)" in text
    assert "10,200 -> 10,100" in text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_exits_zero_when_the_documents_match(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "a.pdf", metadata={"title": "draft"})
    b = make_pdf(tmp_path / "b.pdf", metadata={"title": "final"})

    assert main(["compare", a, b, "-o", "none"]) == 0
    assert "identical documents" in capsys.readouterr().out


def test_cli_exits_one_when_the_documents_differ(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", rows=edited(ROWS, "10,200", "10,100"))
    out = tmp_path / "diff.pdf"

    assert main(["compare", a, b, "-o", str(out)]) == 1
    assert out.is_file()
    assert "NOT identical" in capsys.readouterr().out


def test_cli_reports_a_missing_file_without_a_traceback(tmp_path, capsys):
    from fincheck.cli import main

    a = make_pdf(tmp_path / "a.pdf")

    assert main(["compare", a, str(tmp_path / "nope.pdf"), "-o", "none"]) == 2
    assert "file not found" in capsys.readouterr().err


def test_pages_of_different_size_are_reported_as_not_comparable(tmp_path):
    """A page-width difference of a fraction of a point makes pixels unmeasurable.

    Folding those pages into the changed-pixel percentage produced "0.000% of
    pixels", which reads as "differs by almost nothing" rather than "could not
    be compared".
    """
    a = make_pdf(tmp_path / "a.pdf")
    doc = fitz.open()
    page = doc.new_page(width=595.32, height=842)  # 0.32pt wider than A
    for x, y, text in ROWS:
        page.insert_text((x, y), text, fontsize=11, fontname="helv")
    b = tmp_path / "b.pdf"
    doc.save(str(b))
    doc.close()

    result = compare_pdfs(a, str(b), dpi=150)
    pixels = result.pages[0].pixels
    console = comparison_to_console(result)

    assert pixels.comparable is False
    assert "different rendered size" in console
    assert "0.000% of pixels" not in console
