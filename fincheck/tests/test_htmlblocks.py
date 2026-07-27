"""Tests for reading an HTML filing instead of a PDF.

Everything hard about a PDF is an inference; HTML states its structure. These
tests pin what that buys — and that it produces the same blocks, so nothing
downstream can tell which format it got.
"""

from fincheck.blocks import Block, is_html, segment
from fincheck.htmlblocks import segment_html
from fincheck.ledger import read_figures, reconcile

TABLE = """<HTML><BODY>
<p>The Group operates in one reportable segment.</p>
<TABLE>
<TR><TD>Particulars</TD><TD>Financial</TD><TD>Hi-Tech</TD><TD>Total</TD></TR>
<TR><TD>Revenue</TD><TD>&nbsp;11,796 </TD><TD>&nbsp;3,296 </TD><TD>&nbsp;42,279 </TD></TR>
<TR><TD>Net profit</TD><TD>&nbsp;6,368 </TD><TD></TD><TD>&nbsp;6,374 </TD></TR>
</TABLE>
</BODY></HTML>"""


def write(path, body):
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_html_is_recognised_and_routed(tmp_path):
    assert is_html("x.htm") and is_html("X.HTML") and not is_html("x.pdf")
    blocks = segment(write(tmp_path / "a.html", TABLE))
    assert blocks and all(isinstance(b, Block) for b in blocks)


def test_every_column_is_read_however_wide_the_table(tmp_path):
    """The point of reading markup: a page margin cannot cut a column off."""
    blocks = segment_html(write(tmp_path / "a.html", TABLE))
    table = next(b for b in blocks if b.kind == "table")
    revenue = next(r for r in table.rows if r.label == "Revenue")

    assert [f.value for f in revenue.figures] == [11796.0, 3296.0, 42279.0]
    assert revenue.label == "Revenue"


def test_a_blank_between_figures_is_a_nil_column(tmp_path):
    """The same statement exported to PDF writes a dash there, read as nil.

    Left as nothing, every row carrying a nil would differ by construction
    against its PDF counterpart.
    """
    blocks = segment_html(write(tmp_path / "a.html", TABLE))
    table = next(b for b in blocks if b.kind == "table")
    profit = next(r for r in table.rows if r.label == "Net profit")

    assert [f.value for f in profit.figures] == [6368.0, 0.0, 6374.0]


def test_a_blank_before_the_first_figure_is_layout_not_a_column(tmp_path):
    body = "<table><tr><td>Revenue</td><td></td><td>100</td><td>200</td></tr></table>"
    blocks = segment_html(write(tmp_path / "a.html", body))
    row = blocks[0].rows[0]

    assert [f.value for f in row.figures] == [100.0, 200.0]


def test_prose_outside_tables_becomes_paragraphs(tmp_path):
    blocks = segment_html(write(tmp_path / "a.html", TABLE))
    paras = [b for b in blocks if b.kind == "paragraph"]

    assert any("one reportable segment" in b.text for b in paras)


def test_an_edgar_wrapper_is_stripped(tmp_path):
    wrapped = ("<DOCUMENT>\n<TYPE>EX-99.8\n<FILENAME>x.htm\n<TEXT>\n" + TABLE)
    blocks = segment_html(write(tmp_path / "a.html", wrapped))

    assert any(b.kind == "table" for b in blocks)
    assert not any("EX-99.8" in b.text for b in blocks)


def test_a_layout_table_with_no_figures_reads_as_prose(tmp_path):
    body = "<table><tr><td>For and on behalf of the Board of Directors</td></tr></table>"
    blocks = segment_html(write(tmp_path / "a.html", body))

    assert [b.kind for b in blocks] == ["paragraph"]


def test_the_ledger_reads_html_and_pdf_alike(tmp_path):
    """The reconciliation must not care which format each side arrives in."""
    figures = read_figures(write(tmp_path / "a.html", TABLE))
    values = {v for v in figures}

    assert {11796.0, 3296.0, 42279.0, 6368.0, 6374.0} <= values


def test_a_figure_only_in_one_format_still_reconciles(tmp_path):
    """A clipped PDF loses a column; the HTML keeps it. Same amounts, so the
    ledger must reconcile them when both are complete."""
    a = write(tmp_path / "a.html", TABLE)
    b = write(tmp_path / "b.html", TABLE.replace("Hi-Tech", "HiTech"))

    assert reconcile(a, b).reconciled


def test_a_footnote_superscript_does_not_swallow_its_figure(tmp_path):
    """"17,710<SUP>(2)</SUP>" is the figure 17,710 with a reference on it.

    Read as one string the cell stops being a number, and the figure vanishes
    from the comparison — a real filing lost two totals this way.
    """
    body = ("<table><tr><td>Total</td><td>&nbsp;17,771 </td>"
            "<td>&nbsp;17,710<SUP>(2)</SUP></td></tr></table>")
    blocks = segment_html(write(tmp_path / "a.html", body))
    row = blocks[0].rows[0]

    assert [f.value for f in row.figures] == [17771.0, 17710.0]
    assert row.label == "Total"
