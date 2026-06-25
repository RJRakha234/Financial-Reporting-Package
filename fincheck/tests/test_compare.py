"""Unit and end-to-end tests for the PDF↔HTML comparison."""

import subprocess
import sys
from pathlib import Path

import pytest

from fincheck.compare import (
    CHANGED,
    EXTRA_IN_HTML,
    MISSING_IN_HTML,
    ROW_EXTRA,
    ROW_MISSING,
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

    # changed figure inside an otherwise-matching row
    assert any(
        d.pdf_text == "8,750" and d.html_text == "8,570"
        for d in result.by_kind(CHANGED)
    )
    # a whole table row dropped from the HTML (table-formatting miss)
    assert any("1,200" in d.pdf_text and "Goodwill" in d.pdf_text
               for d in result.by_kind(ROW_MISSING))
    # a row present only in the HTML
    assert any("250" in d.html_text and "Prepaid" in d.html_text
               for d in result.by_kind(ROW_EXTRA))
    # wording change inside a matched row
    assert any("Manufacturing" in d.pdf_text and "Manufacturers" in d.html_text
               for d in result.by_kind(TEXT_CHANGED))
    # every row difference is anchored to a real PDF page (missing rows) or
    # the nearest one (extra rows)
    assert all(d.page is not None for d in result.by_kind(ROW_MISSING))


def test_end_to_end_writes_both_outputs(tmp_path):
    out_pdf = tmp_path / "validated.pdf"
    out_html = tmp_path / "commented.html"
    result = compare(
        str(PDF), str(HTML), output_pdf=str(out_pdf), output_html=str(out_html)
    )
    assert out_pdf.exists() and out_html.exists()
    assert result.output_pdf == str(out_pdf)
    assert result.output_html == str(out_html)

    import fitz

    doc = fitz.open(str(out_pdf))
    assert doc.page_count == 2  # summary page prepended to the 1-page sample
    annots = list(doc[1].annots() or [])
    contents = [a.info.get("content", "") for a in annots]
    # the changed figure carries its comment, and validated greens are present
    assert any("8,570" in c for c in contents)
    assert any("Validated" in c for c in contents)

    text = out_html.read_text(encoding="utf-8")
    assert "✓" in text and "✗" in text          # pass/fail comments present
    assert "fincheck comparison" in text        # legend injected
    assert "8,570" in text                       # original HTML content preserved


def test_validated_pdf_has_green_coverage(tmp_path):
    out = tmp_path / "validated.pdf"
    compare(str(PDF), str(HTML), output_pdf=str(out))
    import fitz

    doc = fitz.open(str(out))
    greens = [
        a
        for a in (doc[1].annots() or [])
        if a.type[1] == "Highlight"
        and tuple(round(c, 2) for c in a.colors["stroke"]) == (0.3, 0.66, 0.36)
    ]
    assert len(greens) > 5  # many validated figures highlighted green


def test_text_comparison_can_be_disabled():
    result = compare(str(PDF), str(HTML), compare_text=False)
    assert result.text_differences == []
    assert result.differences  # rows/figures still compared
