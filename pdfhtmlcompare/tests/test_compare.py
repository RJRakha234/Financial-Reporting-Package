"""Unit and end-to-end tests for the financial-table comparison."""

import subprocess
import sys
from pathlib import Path

import pytest

from pdfhtmlcompare.compare import (
    FIGURE_CHANGED,
    FIGURE_EXTRA,
    FIGURE_MISSING,
    LINE_EXTRA,
    LINE_MISSING,
    WORD_CHANGED,
    compare,
)
from pdfhtmlcompare.model import Figure, Line
from pdfhtmlcompare.compare import _diff_figures

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "sample_statement.pdf"
HTML = ROOT / "sample_filing.html"


def _figs(values, page=0):
    return [Figure(v, str(v), page, (0, i, 1, i + 1)) for i, v in enumerate(values)]


def test_diff_figures_changed_missing_extra():
    # PDF [100, 200, 300] vs HTML [100, 250]  -> 200 changed to 250, 300 missing
    d = _diff_figures(_figs([100, 200, 300]), _figs([100, 250]), page=0)
    kinds = sorted(f.kind for f in d)
    assert FIGURE_CHANGED in kinds
    assert FIGURE_MISSING in kinds
    assert any(f.pdf_text == "200" and f.html_text == "250" for f in d)


def test_diff_figures_extra_in_html():
    d = _diff_figures(_figs([100]), _figs([100, 999]), page=3)
    assert [f.kind for f in d] == [FIGURE_EXTRA]
    assert d[0].html_text == "999"


@pytest.fixture(scope="session", autouse=True)
def ensure_samples():
    if not PDF.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample.py")], check=True)
    if not HTML.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample_html.py")], check=True)


def test_end_to_end_catches_each_planted_issue():
    result = compare(str(PDF), str(HTML))

    assert any(f.pdf_text == "8,750" and f.html_text == "8,570"
               for f in result.by_kind(FIGURE_CHANGED))                 # number changed
    assert any("Goodwill" in f.pdf_text and "1,200" in f.pdf_text
               for f in result.by_kind(LINE_MISSING))                   # table line dropped
    assert any("Prepaid" in f.html_text and "250" in f.html_text
               for f in result.by_kind(LINE_EXTRA))                     # line only in HTML
    assert any("receivables" in f.pdf_text and "receivable" in f.html_text
               for f in result.by_kind(WORD_CHANGED))                   # wording differs
    # no spurious changes elsewhere
    assert result.by_kind(FIGURE_MISSING) == []


def test_end_to_end_writes_both_outputs(tmp_path):
    out_pdf = tmp_path / "validated.pdf"
    out_html = tmp_path / "commented.html"
    result = compare(str(PDF), str(HTML), output_pdf=str(out_pdf), output_html=str(out_html))
    assert out_pdf.exists() and out_html.exists()

    import fitz

    doc = fitz.open(str(out_pdf))
    assert doc.page_count == 2  # summary page + the 1-page statement
    contents = [a.info.get("content", "") for a in (doc[1].annots() or [])]
    assert any("8,570" in c for c in contents)        # the changed number is commented
    assert any("Validated" in c for c in contents)    # green coverage present

    text = out_html.read_text(encoding="utf-8")
    assert "✓" in text and "✗" in text
    assert "pdfhtmlcompare" in text                    # legend injected
    assert "8,570" in text                             # original HTML preserved


def test_single_pdf_as_list_matches_string():
    a = compare(str(PDF), str(HTML))
    b = compare([str(PDF)], str(HTML))
    assert len(a.findings) == len(b.findings)
    assert a.validated_rows == b.validated_rows


def test_multiple_pdfs_are_concatenated(tmp_path):
    # Two PDFs in -> page count is the sum; comparison still runs end to end.
    from pdfhtmlcompare.pdfdoc import merge_pdfs
    import fitz

    merged = merge_pdfs([str(PDF), str(PDF)])
    try:
        assert fitz.open(merged).page_count == 2 * fitz.open(str(PDF)).page_count
    finally:
        import os

        os.remove(merged)

    result = compare([str(PDF), str(PDF)], str(HTML))
    # the planted number change is still found when the statements are doubled
    assert result.by_kind(FIGURE_CHANGED)


def test_validated_pdf_has_green_coverage(tmp_path):
    out = tmp_path / "v.pdf"
    compare(str(PDF), str(HTML), output_pdf=str(out))
    import fitz

    doc = fitz.open(str(out))
    greens = [
        a for a in (doc[1].annots() or [])
        if a.type[1] == "Highlight"
        and tuple(round(c, 2) for c in a.colors["stroke"]) == (0.3, 0.66, 0.36)
    ]
    assert len(greens) >= 5
