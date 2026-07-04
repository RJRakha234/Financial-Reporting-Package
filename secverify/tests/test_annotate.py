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
    "Condensed Balance Sheet as at June 30, 2025. "
    "Property, plant and equipment 9,868 10,070. "
    "Total assets 1,23,696. Trade receivables 27,751."
)


def run(html: str):
    return Annotator(make_corpus([PAGE])).run(html, "ref.pdf", "doc.html")


def test_matching_figures_and_text_go_green():
    result = run(
        "<html><body><p>Property, plant and equipment</p>"
        "<table><tr><td>Total assets</td><td>1,23,696</td></tr></table>"
        "</body></html>"
    )
    assert not result.issues
    assert result.figures_total == 1
    assert result.figures_ok == 1
    assert 'class="secv-num-ok"' in result.html_out
    assert "secv-text-ok" in result.html_out


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
