from pdfhtmlcompare.htmldoc import extract_html_lines, read_html_lines
from pdfhtmlcompare.model import NUMBER, WORD, build_line
from pdfhtmlcompare.numbers import is_numberish, parse_number


def test_table_rows_become_lines():
    html = """
    <table>
      <tr><td>Cash and cash equivalents</td><td>8,750</td></tr>
      <tr><td>Trade receivables</td><td>3,400</td></tr>
    </table>"""
    lines = extract_html_lines(html)
    assert "Cash and cash equivalents 8,750" in lines
    assert "Trade receivables 3,400" in lines


def test_body_gating_skips_wrapper():
    # Content before <body> (e.g. EDGAR wrapper) is ignored.
    html = "<document><type>EX-99</type><body><p>Revenue 100</p></body></document>"
    assert extract_html_lines(html) == ["Revenue 100"]


def test_script_and_style_dropped():
    html = "<body><style>x{}</style><p>Revenue 100</p><script>var a='x 9';</script></body>"
    assert extract_html_lines(html) == ["Revenue 100"]


def _line(raw):
    triples = []
    for tok in raw.split():
        value = parse_number(tok) if is_numberish(tok) else None
        triples.append((tok, value, None))
    return build_line(0, raw, triples)


def test_tokens_words_and_numbers():
    line = _line("Cash and cash equivalents 8,750")
    assert [t.kind for t in line.tokens] == [WORD, WORD, WORD, WORD, NUMBER]
    assert line.tokens[-1].key == "8750.00"


def test_hyphen_and_space_fold():
    # "Non-current" and "Non current" produce the same word tokens
    assert [t.key for t in _line("Non-current").tokens] == ["non", "current"]
    assert [t.key for t in _line("Non current").tokens] == ["non", "current"]


def test_date_day_and_footnote_skipped():
    line = _line("Balance as at April 1, 2025 (1) 2,073")
    keys = [t.key for t in line.tokens if t.kind == NUMBER]
    assert "1.00" not in keys          # the day after "April" is not a number
    assert "-1.00" not in keys         # the (1) footnote marker is skipped
    assert "2073.00" in keys
