from fincheck.htmlextract import extract_lines_from_html


def test_table_rows_become_lines():
    html = """
    <table>
      <tr><td>Cash and cash equivalents</td><td>8,750</td></tr>
      <tr><td>Trade receivables</td><td>3,400</td></tr>
    </table>
    """
    lines = extract_lines_from_html(html)
    assert "Cash and cash equivalents 8,750" in lines
    assert "Trade receivables 3,400" in lines


def test_script_and_style_are_dropped():
    html = (
        "<style>td{color:red}</style>"
        "<p>Revenue 100</p>"
        "<script>var x = 'Revenue 999';</script>"
    )
    lines = extract_lines_from_html(html)
    assert lines == ["Revenue 100"]


def test_entities_and_nbsp_normalised():
    html = "<p>Profit&nbsp;&amp;&nbsp;loss 7,000</p>"
    lines = extract_lines_from_html(html)
    assert lines == ["Profit & loss 7,000"]


def test_headings_separate_from_body():
    html = "<h1>Acme Ltd</h1><p>Balance sheet</p>"
    assert extract_lines_from_html(html) == ["Acme Ltd", "Balance sheet"]
