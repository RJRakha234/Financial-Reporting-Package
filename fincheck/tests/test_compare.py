"""Unit and end-to-end tests for the PDF↔HTML comparison."""

import subprocess
import sys
from pathlib import Path

import pytest

from fincheck.compare import (
    CHANGED,
    EXTRA_IN_HTML,
    MISSING_IN_HTML,
    TEXT_CHANGED,
    Num,
    Word,
    _diff_numbers,
    _diff_words,
    _match_block,
    compare,
)

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "sample_financials.pdf"
HTML = ROOT / "sample_filing.html"


def _pnums(values, context="", page=0):
    """PDF-side figures (carry a page)."""
    return [Num(v, str(v), context, i, page, (0, i, 1, i + 1)) for i, v in enumerate(values)]


def _hnums(values, context=""):
    """HTML-side figures (no page/bbox)."""
    return [Num(v, str(v), context, i) for i, v in enumerate(values)]


# --- number alignment -------------------------------------------------------


def test_identical_numbers_have_no_differences():
    diffs, matched = _diff_numbers(_pnums([1, 2, 3]), _hnums([1, 2, 3]))
    assert diffs == []
    assert matched == 3


def test_changed_figure_is_flagged_with_both_values():
    diffs, _ = _diff_numbers(_pnums([100, 200]), _hnums([100, 250]))
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == CHANGED
    assert d.pdf_text == "200" and d.html_text == "250"
    assert d.page == 0  # anchored to the PDF page


def test_figure_missing_from_html():
    diffs, _ = _diff_numbers(_pnums([100, 200, 300]), _hnums([100, 300]))
    assert [d.kind for d in diffs] == [MISSING_IN_HTML]
    assert diffs[0].pdf_text == "200"


def test_figure_only_in_html_anchored_to_a_page():
    diffs, _ = _diff_numbers(_pnums([100, 300]), _hnums([100, 200, 300]))
    assert [d.kind for d in diffs] == [EXTRA_IN_HTML]
    assert diffs[0].html_text == "200"
    assert diffs[0].page == 0  # nearest PDF page, not None


def test_match_block_pairs_by_context_not_position():
    # PDF [cash 8750]; HTML [prepaid 250, cash 8570] in the same mismatched run.
    pa = [Num(8750, "8,750", "Cash and cash equivalents", 0, 0, (0, 0, 1, 1))]
    ha = [
        Num(250, "250", "Prepaid expenses", 0),
        Num(8570, "8,570", "Cash and cash equivalents", 1),
    ]
    pairs, leftover_p, leftover_h = _match_block(pa, ha)
    assert len(pairs) == 1
    p, h = pairs[0]
    assert p.value == 8750 and h.value == 8570  # paired with its own line
    assert leftover_p == []
    assert [n.value for n in leftover_h] == [250]  # the invented figure


# --- word alignment ---------------------------------------------------------


def _words(text, page=0, html=False):
    out = []
    for i, w in enumerate(text.split()):
        out.append(Word(w.lower(), w, i, None if html else page, None if html else (0, i, 1, i + 1)))
    return out


def test_wording_change_is_flagged():
    pdf = _words("Acme Manufacturing Limited")
    html = _words("Acme Manufacturers Limited", html=True)
    diffs = _diff_words(pdf, html)
    assert len(diffs) == 1
    assert diffs[0].kind == TEXT_CHANGED
    assert "Manufacturing" in diffs[0].pdf_text
    assert "Manufacturers" in diffs[0].html_text


def test_identical_words_have_no_text_difference():
    pdf = _words("balance sheet as at")
    html = _words("balance sheet as at", html=True)
    assert _diff_words(pdf, html) == []


# --- end to end -------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def ensure_samples():
    if not PDF.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample.py")], check=True)
    if not HTML.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample_html.py")], check=True)


def test_end_to_end_catches_every_planted_discrepancy():
    result = compare(str(PDF), str(HTML))
    kinds = {d.kind for d in result.differences}
    assert {CHANGED, MISSING_IN_HTML, EXTRA_IN_HTML, TEXT_CHANGED} <= kinds

    changed = result.by_kind(CHANGED)
    assert any(d.pdf_text == "8,750" and d.html_text == "8,570" for d in changed)
    assert any(d.pdf_text == "1,200" for d in result.by_kind(MISSING_IN_HTML))
    assert any(d.html_text == "250" for d in result.by_kind(EXTRA_IN_HTML))
    # every difference points at a real PDF page
    assert all(d.page is not None for d in result.number_differences)


def test_end_to_end_writes_annotated_pdf(tmp_path):
    out = tmp_path / "compared.pdf"
    result = compare(str(PDF), str(HTML), output_pdf=str(out))
    assert out.exists()
    import fitz

    doc = fitz.open(str(out))
    assert doc.page_count == 2  # summary page prepended to the 1-page sample
    contents = [a.info.get("content", "") for a in (doc[1].annots() or [])]
    assert any("8,570" in c for c in contents)
    assert result.output_pdf == str(out)


def test_text_comparison_can_be_disabled():
    result = compare(str(PDF), str(HTML), compare_text=False)
    assert result.text_differences == []
    assert result.number_differences  # figures still compared
