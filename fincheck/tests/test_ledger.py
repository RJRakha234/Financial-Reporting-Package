"""Tests for the figure reconciliation that does not trust the alignment.

The side-by-side view pairs content by similarity, so a mis-paired row can
compare a figure against the wrong counterpart — or against none. These tests
pin the property that makes the ledger worth having: its answer does not depend
on the pairing being right.
"""

import fitz

from fincheck import side_by_side
from fincheck.ledger import describe, reconcile


def write(path, lines, width=595):
    doc = fitz.open()
    page = doc.new_page(width=width, height=842)
    y = 80
    for line in lines:
        page.insert_text((60, y), line, fontsize=10, fontname="helv")
        y += 16
    doc.save(str(path))
    doc.close()
    return str(path)


STATEMENT = [
    "Property, plant and equipment            11,596      11,778",
    "Goodwill                                 11,502      10,106",
    "Total non-current assets                 54,613      51,804",
]


def test_identical_documents_reconcile_exactly(tmp_path):
    a = write(tmp_path / "a.pdf", STATEMENT)
    b = write(tmp_path / "b.pdf", STATEMENT)

    ledger = reconcile(a, b)

    assert ledger.reconciled
    assert ledger.only_in_a == [] and ledger.only_in_b == []
    assert ledger.coverage == 100.0
    assert ledger.total_a == ledger.total_b == 6


def test_a_dropped_column_is_reported_without_any_pairing(tmp_path):
    """The finding that matters: a total column the other document never prints."""
    wide = [
        "Balance as at April 1        2,071      68,405      88,116",
        "Profit for the period            0      12,874      12,874",
    ]
    clipped = [line.rsplit(None, 1)[0] for line in wide]
    a = write(tmp_path / "a.pdf", wide, width=842)
    b = write(tmp_path / "b.pdf", clipped, width=842)

    ledger = reconcile(a, b)

    missing = {x.value for x in ledger.only_in_a}
    assert 88116.0 in missing, "a figure printed only in the benchmark"
    assert not ledger.reconciled
    assert ledger.coverage < 100.0
    # 88,116 is printed nowhere in B: absent, the strongest reading.
    by_value = {x.value: x for x in ledger.only_in_a}
    assert by_value[88116.0].absent
    assert 88116.0 in {x.value for x in ledger.absent_amounts}
    # 12,874 is printed twice in A and once in B: a surplus of one, not absent.
    assert by_value[12874.0].surplus == 1
    assert by_value[12874.0].here == 2 and by_value[12874.0].there == 1
    assert not by_value[12874.0].absent


def test_formatting_differences_do_not_break_reconciliation(tmp_path):
    """(1,234.56), 1234.56 and 1,234.56 are the same amount."""
    a = write(tmp_path / "a.pdf", ["Provision   (1,234.56)   7,000"])
    b = write(tmp_path / "b.pdf", ["Provision   (1234.56)    7,000.00"])

    assert reconcile(a, b).reconciled


def test_the_ledger_survives_a_wrong_pairing(tmp_path):
    """The property the whole module exists for.

    Labels are made deliberately unmatchable so the similarity alignment
    cannot pair the rows. The ledger must still report the figures as
    reconciling, because they are all printed in both documents.
    """
    a = write(tmp_path / "a.pdf", ["Alpha beta gamma   4,941   4,714"])
    b = write(tmp_path / "b.pdf", ["Zeta eta theta     4,941   4,714"])

    result = side_by_side(a, b)
    ledger = reconcile(a, b)

    # However the pairing came out, every figure is present on both sides.
    assert ledger.reconciled
    assert ledger.coverage == 100.0
    assert result.ledger.reconciled


def test_a_changed_figure_is_never_hidden_by_the_ledger(tmp_path):
    a = write(tmp_path / "a.pdf", STATEMENT)
    b = write(
        tmp_path / "b.pdf",
        [l.replace("11,502      10,106", "11,502      10,999") for l in STATEMENT],
    )

    ledger = reconcile(a, b)

    assert 10106.0 in {x.value for x in ledger.only_in_a}
    assert 10999.0 in {x.value for x in ledger.only_in_b}


def test_the_report_states_the_reconciliation(tmp_path):
    a = write(tmp_path / "a.pdf", STATEMENT)
    b = write(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out))
    html = out.read_text()

    assert result.ledger is not None
    assert "Independent figure reconciliation" in html
    assert "the same number of times" in html


def test_the_ledger_can_be_switched_off(tmp_path):
    a = write(tmp_path / "a.pdf", STATEMENT)
    b = write(tmp_path / "b.pdf", STATEMENT)
    out = tmp_path / "sbs.html"

    result = side_by_side(a, b, output_html=str(out), figure_ledger=False)

    assert result.ledger is None
    assert "Independent figure reconciliation" not in out.read_text()


def test_a_footnote_marker_is_not_read_as_a_negative_figure(tmp_path):
    """"(1)" alone on a line is a marker, not minus one.

    Read as a figure it deviates against every counterpart — a false alarm,
    and a reconciliation may not raise those.
    """
    from fincheck.blocks import segment

    blocks = segment(write(tmp_path / "a.pdf", ["Claims against the Group", "(1)"]))

    assert all(not r.figures for b in blocks for r in b.rows), (
        "a bare footnote marker must not become a figure"
    )


def test_the_console_output_survives_a_narrow_terminal_encoding(tmp_path, capsys):
    """The run must not die *after* writing every file, while printing results.

    A Windows console may be cp1252, cp437 or cp850. The tool prints document
    text — rupee signs, en-dashes, the multiplication sign in the ledger — and
    an unencodable character there raised UnicodeEncodeError at the very last
    step, after all the work had succeeded.
    """
    import io
    import sys

    from fincheck.cli import _make_output_safe

    a = write(tmp_path / "a.pdf", STATEMENT)
    b = write(tmp_path / "b.pdf", STATEMENT)

    # A stream that can only carry ASCII, as a narrow code page effectively is.
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    original = sys.stdout
    sys.stdout = narrow
    try:
        _make_output_safe()
        print(describe(reconcile(a, b), "benchmark", "compared"))
        print("printed 3× here, 2× there — ₹4,185")
    finally:
        sys.stdout = original

    # It printed rather than raising; that is the whole requirement.
    assert narrow.encoding in ("utf-8", "ascii")
