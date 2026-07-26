"""Tests for reviewer-supplied section numbers.

Content matching can decline to pair two passages that plainly correspond,
leaving a blank where a comparison belongs. These tests cover the escape hatch:
numbering the corresponding regions in both PDFs.
"""

import fitz

from fincheck import side_by_side
from fincheck.align import align, align_by_section, units_of
from fincheck.blocks import segment
from fincheck.marks import read_marks


def write(path, lines, marks=(), width=595, height=842, pitch=16, start=80):
    """Draw lines, then highlight the given line ranges with a section number."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    boxes = []
    y = start
    for line in lines:
        if line is None:  # blank line, as a document leaves above a heading
            y += pitch
            boxes.append(fitz.Rect(58, y - 10, width - 40, y + 4))
            continue
        page.insert_text((60, y), line, fontsize=10, fontname="helv")
        boxes.append(fitz.Rect(58, y - 10, width - 40, y + 4))
        y += pitch
    for label, first, last in marks:
        rect = fitz.Rect(boxes[first])
        for i in range(first, last + 1):
            rect |= boxes[i]
        annot = page.add_highlight_annot(rect)
        annot.set_info(content=label)
        annot.update()
    doc.save(str(path))
    doc.close()
    return str(path)


def test_marks_are_read_with_their_numbers(tmp_path):
    path = write(
        tmp_path / "a.pdf",
        ["alpha one", "alpha two", "beta one"],
        marks=[("1", 0, 1), ("2", 2, 2)],
    )

    marks = read_marks(path)

    assert marks.labels == ["1", "2"]
    assert bool(marks) is True


def test_an_unmarked_pdf_yields_no_marks(tmp_path):
    marks = read_marks(write(tmp_path / "a.pdf", ["nothing highlighted here"]))

    assert not marks
    assert marks.labels == []


def test_sections_order_two_before_ten(tmp_path):
    path = write(
        tmp_path / "a.pdf",
        ["one", "two", "ten"],
        marks=[("1", 0, 0), ("2", 1, 1), ("10", 2, 2)],
    )

    assert read_marks(path).labels == ["1", "2", "10"]


def test_rows_are_assigned_to_the_section_marked_over_them(tmp_path):
    path = write(
        tmp_path / "a.pdf",
        ["first section line", "second section line"],
        marks=[("1", 0, 0), ("2", 1, 1)],
    )

    units = units_of(segment(path), read_marks(path))

    assert {u.section for u in units} == {"1", "2"}


def test_a_paragraph_spanning_two_marks_is_split_between_them(tmp_path):
    """A block reconstructed from geometry can run straight through two marks."""
    path = write(
        tmp_path / "a.pdf",
        ["a continuous run of prose that", "carries on past the mark boundary"],
        marks=[("1", 0, 0), ("2", 1, 1)],
    )

    blocks = segment(path)
    units = units_of(blocks, read_marks(path))

    assert len(blocks) == 1, "geometry alone sees one paragraph"
    assert sorted(u.section for u in units) == ["1", "2"]


# --------------------------------------------------------------------------
# The point of the feature
# --------------------------------------------------------------------------

WORDS_A = [
    "Guidance for the year",
    "Revenue growth of 1%-3%",
    "Operating margin of 20%-22%",
]
# The same content, but split differently and reworded enough that similarity
# alone pairs it badly.
WORDS_B = [
    "Guidance for the year Revenue growth",
    "of 1%-3% Operating margin of 20%-22% and",
    "an additional sentence appears only here",
]


def test_a_numbered_section_never_comes_out_against_a_blank(tmp_path):
    a = write(tmp_path / "a.pdf", WORDS_A, marks=[("1", 0, 2)])
    b = write(tmp_path / "b.pdf", WORDS_B, marks=[("1", 0, 2)])

    ua = units_of(segment(a), read_marks(a))
    ub = units_of(segment(b), read_marks(b))
    pairs = align_by_section(ua, ub)

    assert pairs, "the section must produce a comparison"
    assert all(p.a is not None and p.b is not None for p in pairs)


def test_the_numbering_changes_nothing_when_it_is_absent(tmp_path):
    """An unmarked pair must compare exactly as it did before."""
    a = write(tmp_path / "a.pdf", WORDS_A)
    b = write(tmp_path / "b.pdf", WORDS_A)

    plain = side_by_side(a, b, use_marks=False).summary
    with_marks = side_by_side(a, b, use_marks=True).summary

    assert (plain.matched, plain.changed) == (with_marks.matched, with_marks.changed)
    assert with_marks.only_in_a == plain.only_in_a


def test_differences_inside_a_numbered_section_are_still_reported(tmp_path):
    """Removing blanks must not mean hiding real differences."""
    a = write(tmp_path / "a.pdf", WORDS_A, marks=[("1", 0, 2)])
    b = write(tmp_path / "b.pdf", WORDS_B, marks=[("1", 0, 2)])

    result = side_by_side(a, b)
    marked = [s for s in result.sections if s.marked == "1"]

    assert len(marked) == 1
    assert marked[0].status == "changed"
    added = " ".join(
        t for p in marked[0].pairs for op, t in p.words if op == "+"
    )
    assert "additional sentence" in added


def test_content_outside_the_marks_still_matches_on_similarity(tmp_path):
    a = write(tmp_path / "a.pdf", WORDS_A + ["a trailing note"], marks=[("1", 0, 2)])
    b = write(tmp_path / "b.pdf", WORDS_A + ["a trailing note"], marks=[("1", 0, 2)])

    result = side_by_side(a, b)
    unmarked = [s for s in result.sections if s.marked is None]

    assert unmarked, "the trailing note falls outside the marks"
    assert all(s.status == "same" for s in unmarked)


def test_a_section_numbered_in_only_one_document_is_reported_one_sided(tmp_path):
    """The guarantee covers sections numbered in both; one side alone is a finding."""
    a = write(tmp_path / "a.pdf", WORDS_A + ["extra clause only here"],
              marks=[("1", 0, 2), ("2", 3, 3)])
    b = write(tmp_path / "b.pdf", WORDS_A, marks=[("1", 0, 2)])

    result = side_by_side(a, b)
    section_two = [s for s in result.sections if s.marked == "2"]

    assert len(section_two) == 1
    assert section_two[0].status == "removed"


def test_the_page_names_the_numbering_and_offers_to_hide_the_rest(tmp_path):
    a = write(tmp_path / "a.pdf", WORDS_A + ["a trailing note"], marks=[("1", 0, 2)])
    b = write(tmp_path / "b.pdf", WORDS_B + ["a trailing note"], marks=[("1", 0, 2)])
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out))
    page = out.read_text()

    assert result.marked_sections == ["1"]
    assert "Using your section numbers" in page
    assert 'class="snum"' in page  # the section badge
    assert 'id="hide-unmarked"' in page


def test_figures_in_a_numbered_section_are_still_compared_per_row(tmp_path):
    """Numbering must not collapse a table into prose and lose the cell diff."""
    rows_a = ["Revenue        4,941   4,714", "Operating margin   1,028   1,041"]
    rows_b = ["Revenue        4,941   4,714", "Operating margin   1,028   1,099"]
    a = write(tmp_path / "a.pdf", rows_a, marks=[("1", 0, 1)])
    b = write(tmp_path / "b.pdf", rows_b, marks=[("1", 0, 1)])

    result = side_by_side(a, b)
    changes = result.figure_changes()

    assert len(changes) == 1
    assert changes[0][1] == [(1, 1041.0, 1099.0)]


# --------------------------------------------------------------------------
# Unmarked documents: the same treatment, derived from their own headings
# --------------------------------------------------------------------------

RELEASE = [
    "Guidance for FY26",
    "Revenue growth of 1%-3% in constant currency",
    None,
    "Client wins & Testimonials",
    "1. A collaboration was extended to drive operational efficiency.",
    None,
    "About Infosys",
    "Infosys is a global leader in next-generation digital services.",
]


def test_headings_cut_an_unmarked_document_into_sections(tmp_path):
    from fincheck.align import derive_sections, units_of
    from fincheck.blocks import segment

    units = units_of(segment(write(tmp_path / "a.pdf", RELEASE)))
    found = derive_sections(units)

    assert found >= 3, "the document's own headings should start sections"
    keys = [u.section for u in units if u.section]
    assert any("client wins" in k for k in keys)
    assert any("about infosys" in k for k in keys)


def test_a_heading_key_ignores_punctuation_and_bullets(tmp_path):
    """One file writes "Key highlights :", the other "• Key highlights:"."""
    from fincheck.align import _heading_key

    assert _heading_key("Key highlights :") == _heading_key("Key highlights:")
    assert _heading_key("• Industry & Solutions") == _heading_key("Industry & Solutions")


def test_a_sentence_is_not_mistaken_for_a_heading():
    from fincheck.align import _is_section_heading

    assert _is_section_heading("Client wins & Testimonials")
    assert _is_section_heading("About Infosys")
    assert _is_section_heading("Guidance for FY26 :")
    # Body text, however short, is not a heading.
    assert not _is_section_heading(
        "Infosys is a global leader in next-generation digital services."
    )
    assert not _is_section_heading("")


def test_an_unmarked_pair_is_compared_section_by_section(tmp_path):
    """The point: unhighlighted files get the same treatment as marked ones."""
    a = write(tmp_path / "a.pdf", RELEASE)
    # Same content, re-wrapped and with one sentence reworded.
    b = write(
        tmp_path / "b.pdf",
        [
            "Guidance for FY26",
            "Revenue growth of 1%-3%",
            "in constant currency",
            None,
            "Client wins & Testimonials",
            "1. A collaboration was extended",
            "to drive operational efficiency.",
            None,
            "About Infosys",
            "Infosys is a global leader in",
            "next-generation digital services and consulting.",
        ],
    )

    result = side_by_side(a, b)
    named = [s for s in result.sections if s.marked is not None]

    assert named, "sections should be derived without any marks"
    assert any("about infosys" in (s.marked or "") for s in named)
    # A section found in both must not be reported one-sided.
    both = [s for s in named if s.status in ("same", "changed")]
    assert both
    assert all(
        p.a is not None and p.b is not None for s in both for p in s.pairs
    )


def test_auto_sectioning_can_be_turned_off(tmp_path):
    a = write(tmp_path / "a.pdf", RELEASE)
    b = write(tmp_path / "b.pdf", RELEASE)

    off = side_by_side(a, b, auto_sections=False)

    assert all(s.marked is None for s in off.sections)


def test_reviewer_marks_take_precedence_over_derived_headings(tmp_path):
    a = write(tmp_path / "a.pdf", RELEASE, marks=[("1", 0, 1), ("2", 3, 7)])
    b = write(tmp_path / "b.pdf", RELEASE, marks=[("1", 0, 1), ("2", 3, 7)])

    result = side_by_side(a, b)

    assert result.marked_sections == ["1", "2"]
    assert {s.marked for s in result.sections if s.marked} == {"1", "2"}
