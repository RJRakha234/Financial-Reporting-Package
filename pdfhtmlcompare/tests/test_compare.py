"""Unit and end-to-end tests for the whole-content comparison."""

import subprocess
import sys
from pathlib import Path

import pytest

from pdfhtmlcompare.compare import ADDED, CHANGED, MISSING, _align, compare
from pdfhtmlcompare.model import build_line

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "sample_statement.pdf"
HTML = ROOT / "sample_filing.html"


def _tokens(words_and_nums, page=0):
    triples = []
    for x in words_and_nums:
        if isinstance(x, (int, float)):
            triples.append((str(x), float(x), (0, 0, 1, 1)))
        else:
            triples.append((x, None, (0, 0, 1, 1)))
    return build_line(0, "", triples, page=page).tokens


def test_align_detects_changed_missing_added():
    # anchors (cash, mid, end) let the aligner separate the three kinds
    pdf = _tokens(["cash", 8750, "mid", "goodwill", 1200, "end"])
    html = _tokens(["cash", 8570, "mid", "end", "prepaid", 50])
    kinds = sorted(f.kind for f in _align(pdf, html))
    assert CHANGED in kinds       # 8750 -> 8570
    assert MISSING in kinds       # goodwill 1200 dropped
    assert ADDED in kinds         # prepaid 50 added


def test_align_clean_when_identical():
    pdf = _tokens(["revenue", 100, "total", 100])
    html = _tokens(["revenue", 100, "total", 100])
    assert _align(pdf, html) == []


@pytest.fixture(scope="session", autouse=True)
def ensure_samples():
    if not PDF.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample.py")], check=True)
    if not HTML.exists():
        subprocess.run([sys.executable, str(ROOT / "make_sample_html.py")], check=True)


def test_end_to_end_catches_each_planted_issue():
    result = compare(str(PDF), str(HTML))
    assert any(f.pdf_text == "8,750" and f.html_text == "8,570" for f in result.by_kind(CHANGED))
    assert any("Goodwill" in f.pdf_text and "1,200" in f.pdf_text for f in result.by_kind(MISSING))
    assert any("Prepaid" in f.html_text and "250" in f.html_text for f in result.by_kind(ADDED))
    assert any("receivable" in f.html_text for f in result.by_kind(CHANGED))  # wording
    assert 0.0 < result.coverage < 1.0


def test_single_pdf_as_list_matches_string():
    a = compare(str(PDF), str(HTML))
    b = compare([str(PDF)], str(HTML))
    assert len(a.findings) == len(b.findings)
    assert a.matched_tokens == b.matched_tokens


def test_multiple_pdfs_concatenated():
    from pdfhtmlcompare.pdfdoc import merge_pdfs
    import fitz, os

    merged = merge_pdfs([str(PDF), str(PDF)])
    try:
        assert fitz.open(merged).page_count == 2 * fitz.open(str(PDF)).page_count
    finally:
        os.remove(merged)


def test_end_to_end_writes_both_outputs(tmp_path):
    out_pdf = tmp_path / "validated.pdf"
    out_html = tmp_path / "commented.html"
    result = compare(str(PDF), str(HTML), output_pdf=str(out_pdf), output_html=str(out_html))
    assert out_pdf.exists() and out_html.exists()

    import fitz

    doc = fitz.open(str(out_pdf))
    assert doc.page_count == 2  # summary page + the 1-page statement
    contents = [a.info.get("content", "") for a in (doc[1].annots() or [])]
    assert any("8,570" in c for c in contents)

    text = out_html.read_text(encoding="utf-8")
    assert "✓" in text and "✗" in text
    assert "pdfhtmlcompare" in text
    assert "8,570" in text


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
    assert len(greens) >= 3  # fully-matched lines highlighted green
