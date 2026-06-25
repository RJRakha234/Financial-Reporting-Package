from pdfhtmlcompare.htmldoc import extract_html_lines, read_html_lines
from pdfhtmlcompare.model import build_line
from pdfhtmlcompare.numbers import is_figure


def test_table_rows_become_lines():
    html = """
    <table>
      <tr><td>Cash and cash equivalents</td><td>8,750</td></tr>
      <tr><td>Trade receivables</td><td>3,400</td></tr>
    </table>"""
    lines = extract_html_lines(html)
    assert "Cash and cash equivalents 8,750" in lines
    assert "Trade receivables 3,400" in lines


def test_script_and_style_dropped():
    html = "<style>x{}</style><p>Revenue 100</p><script>var a='x 9';</script>"
    assert extract_html_lines(html) == ["Revenue 100"]


def _line(raw):
    triples = []
    for tok in raw.split():
        real, val = is_figure(tok)
        triples.append((tok, val if real else None, None))
    return build_line(0, raw, triples)


def test_financial_classification():
    assert _line("Cash and cash equivalents 8,750").is_financial
    assert _line("Goodwill 1,200").is_financial
    # heading with no figure
    assert not _line("ASSETS").is_financial
    # table-of-contents dot leader
    assert not _line("Loans …………………… 14").is_financial
    # narrative sentence ending in a full stop
    assert not _line("Goodwill of ₹70 crore as on the date of acquisition.").is_financial


def test_date_day_is_not_a_figure():
    # "April 1, 2025" must not turn the day (1) into a figure
    line = _line("Balance as at April 1, 2025 2,073")
    assert [f.value for f in line.figures] == [2073]
