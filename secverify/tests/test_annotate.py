from secverify.annotate import Annotator
from secverify.pdfside import PdfCorpus, _merged_numeric_words
from secverify.numbers import iter_tokens
from secverify.textnorm import canonicalize


def make_corpus(pages: list[str]) -> PdfCorpus:
    corpus = PdfCorpus()
    for page_idx, raw in enumerate(pages):
        corpus.pages_raw.append(raw)
        for word in raw.split():
            for _s, _e, token, key in iter_tokens(word):
                corpus._add_number(token, key, page_idx + 1)
        for view, letters_only in ((corpus.alnum, False), (corpus.letters, True)):
            view.page_starts.append(len(view.canon))
            canon, index_map = canonicalize(raw, letters_only=letters_only)
            view.canon += canon
            view.index_map.extend((page_idx, off) for off in index_map)
    corpus.page_letters = [
        canonicalize(raw, letters_only=True)[0] for raw in pages
    ]
    return corpus


PAGE = (
    "Condensed Balance Sheet as at June 30, 2025\n"
    "Property, plant and equipment 9,868 10,070\n"
    "Total assets 1,23,696\n"
    "Trade receivables 27,751"
)

#: an HTML rendering that fully reflects PAGE, line by line
FULL_HTML = (
    "<html><body><p>Condensed Balance Sheet as at June 30, 2025</p>"
    "<table><tr><td>Property, plant and equipment</td><td>9,868</td><td>10,070</td></tr>"
    "<tr><td>Total assets</td><td>1,23,696</td></tr>"
    "<tr><td>Trade receivables</td><td>27,751</td></tr></table>"
    "</body></html>"
)


def run(html: str):
    return Annotator(make_corpus([PAGE])).run(html, "ref.pdf", "doc.html")


def test_full_reflection_goes_green_with_no_issues():
    result = run(FULL_HTML)
    assert not result.issues
    assert result.figures_total == result.figures_ok == 6
    assert result.coverage.missing == 0
    assert result.coverage.ok == result.coverage.total == 4
    assert 'class="secv-num-ok"' in result.html_out
    assert "secv-text-ok" in result.html_out


def test_omitted_pdf_row_is_reported():
    # Drop the Trade receivables row from the HTML entirely.
    html = FULL_HTML.replace(
        "<tr><td>Trade receivables</td><td>27,751</td></tr>", ""
    )
    result = run(html)
    omissions = [i for i in result.issues if i.kind == "omission"]
    assert len(omissions) == 1
    assert "Trade receivables" in omissions[0].excerpt
    assert result.coverage.missing == 1
    assert "secv-cov-bad" in result.html_out
    assert 'id="secv-coverage"' in result.html_out


def test_wrong_figure_goes_red_with_remark():
    result = run("<html><body><p>Total assets 1,23,969</p></body></html>")
    figure_issues = [i for i in result.issues if i.kind == "figure"]
    assert len(figure_issues) == 1
    assert "123969" in figure_issues[0].remark
    assert "1,23,696" in figure_issues[0].remark  # transposition suggested
    assert "secv-num-bad" in result.html_out
    assert "SECVERIFY REMARK" in result.html_out


def test_missing_text_goes_red():
    result = run(
        "<html><body><p>This sentence exists only in the HTML filing.</p>"
        "</body></html>"
    )
    text_issues = [i for i in result.issues if i.kind == "text"]
    assert len(text_issues) == 1
    assert text_issues[0].severity == "error"
    assert "secv-text-bad" in result.html_out


def test_reordered_words_downgrade_to_review():
    result = run("<html><body><p>Balance Sheet Condensed</p></body></html>")
    text_issues = [i for i in result.issues if i.kind == "text"]
    assert len(text_issues) == 1
    assert text_issues[0].severity == "review"


def test_own_markers_are_not_treated_as_figures():
    # A red text block gets a [1] marker; the figure pass must not flag it.
    result = run("<html><body><p>Nonexistent auditor paragraph here.</p></body></html>")
    assert not [i for i in result.issues if i.kind == "figure"]


def test_dropped_instance_of_repeated_content_is_flagged():
    # The same row appears on two PDF pages (e.g. balance sheet + note) but
    # only once in the HTML: presence checks pass, counts must not.
    corpus = make_corpus(
        ["Right-of-use assets 3,201", "Right-of-use assets 3,201"]
    )
    html = "<html><body><p>Right-of-use assets 3,201</p></body></html>"
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    escalated = [
        i for i in result.issues if i.kind == "omission" and i.severity == "review"
    ]
    assert len(escalated) == 1  # deduplicated: one report per distinct string
    assert "2× in the PDF" in escalated[0].remark
    counts = [i for i in result.issues if i.kind == "figure-count"]
    assert len(counts) == 1 and "3,201" in counts[0].excerpt


def test_summary_banner_injected():
    result = run("<html><body><p>Total assets 1,23,696</p></body></html>")
    assert 'id="secv-summary"' in result.html_out


def test_letter_spaced_digit_fragments_merge():
    words = [
        {"text": "executive", "x0": 278.3, "x1": 304.6, "top": 100.0},
        {"text": "3", "x0": 481.8, "x1": 485.3, "top": 100.0},
        {"text": "0", "x0": 485.3, "x1": 488.7, "top": 100.0},
        {"text": "2", "x0": 540.2, "x1": 543.7, "top": 100.0},
        {"text": "8", "x0": 543.7, "x1": 547.2, "top": 100.0},
    ]
    assert _merged_numeric_words(words) == ["30", "28"]
