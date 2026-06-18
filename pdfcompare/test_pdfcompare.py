#!/usr/bin/env python3
"""Tests for pdfcompare. Pure standard library -- run with ``pytest`` or, if
pytest is not installed in your locked-down box, with ``python test_pdfcompare.py``.
"""

from pathlib import Path

import make_test_pdfs
import pdfcompare

HERE = Path(__file__).parent


def setup_module(module=None):
    make_test_pdfs.main()


def test_extract_uncompressed():
    pages, warnings = pdfcompare.extract_text(HERE / "sample_a.pdf")
    assert len(pages) == 1
    assert "Total current assets" in pages[0]
    assert "8,750" in pages[0]
    assert warnings == []


def test_extract_flate_compressed():
    pages, _ = pdfcompare.extract_text(HERE / "sample_b.pdf")
    assert "3,200" in pages[0]
    assert "18,750" in pages[0]


def test_identical_files_have_no_diff():
    result = pdfcompare.compare(HERE / "sample_a.pdf", HERE / "sample_a_copy.pdf")
    assert result["text_identical"] is True
    assert result["bytes_identical"] is True
    assert result["diffs"] == []


def test_changed_files_report_diff():
    result = pdfcompare.compare(HERE / "sample_a.pdf", HERE / "sample_b.pdf")
    assert result["text_identical"] is False
    assert result["bytes_identical"] is False
    diff_text = "\n".join(d["diff"] for d in result["diffs"])
    assert "-Trade receivables           3,400" in diff_text
    assert "+Trade receivables           3,200" in diff_text


def test_ignore_whitespace():
    # Same content, the comparison should be stable under whitespace squashing.
    result = pdfcompare.compare(
        HERE / "sample_a.pdf", HERE / "sample_a_copy.pdf", ignore_whitespace=True
    )
    assert result["text_identical"] is True


def test_by_page_diff():
    result = pdfcompare.compare(
        HERE / "sample_a.pdf", HERE / "sample_b.pdf", by_page=True
    )
    assert result["text_identical"] is False
    assert result["diffs"][0]["page"] == 1


def test_colorize_marks_changes():
    diff = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new"
    colored = pdfcompare._colorize(diff)
    assert pdfcompare._RED in colored
    assert pdfcompare._GREEN in colored
    assert pdfcompare._CYAN in colored


def test_lzw_roundtrip_not_required_but_decoder_runs():
    # The LZW decoder should at least handle the clear/EOD-only stream cleanly.
    assert pdfcompare._lzw_decode(b"") == b""


def _run_all():
    setup_module()
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")


if __name__ == "__main__":
    _run_all()
