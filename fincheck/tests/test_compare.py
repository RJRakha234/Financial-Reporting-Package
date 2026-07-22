"""Tests for the document-comparison tool.

These exercise the comparison logic on plain strings, so they run without any
PDF or OCR engine installed.
"""

from fincheck.compare import (
    ComparisonResult,
    compare_documents,
    compare_texts,
    extract_document_text,
    normalize,
)

REFERENCE = """Acme Manufacturing Ltd
Balance Sheet as at 31 December 2024 (INR '000)
Inventories 6,800
Trade receivables 3,400
Cash and cash equivalents 8,750
Total current assets 18,950
"""


def _kinds(result_or_changes):
    changes = getattr(result_or_changes, "number_changes", result_or_changes)
    return [c.kind for c in changes]


def test_identical_documents_match():
    similarity, segments, numbers = compare_texts(REFERENCE, REFERENCE)
    assert similarity == 1.0
    assert segments == []
    assert numbers == []


def test_case_and_whitespace_ignored_by_default():
    noisy = REFERENCE.upper().replace("\n", "   \n\n")
    similarity, segments, numbers = compare_texts(REFERENCE, noisy)
    assert similarity == 1.0
    assert segments == []
    assert numbers == []


def test_altered_digit_reported_as_changed():
    # 8,750 was mis-scanned as 8,150 (a single corrupted digit).
    scanned = REFERENCE.replace("8,750", "8,150")
    _, _, numbers = compare_texts(REFERENCE, scanned)
    changed = [c for c in numbers if c.kind == "changed"]
    assert len(changed) == 1
    assert changed[0].reference == 8750.0
    assert changed[0].scanned == 8150.0


def test_missing_figure_reported():
    scanned = REFERENCE.replace("Cash and cash equivalents 8,750\n", "")
    _, _, numbers = compare_texts(REFERENCE, scanned)
    missing = [c for c in numbers if c.kind == "missing_in_scan"]
    assert any(c.reference == 8750.0 for c in missing)


def test_extra_figure_reported():
    scanned = REFERENCE + "Rogue line 9,999\n"
    _, _, numbers = compare_texts(REFERENCE, scanned)
    extra = [c for c in numbers if c.kind == "extra_in_scan"]
    assert any(c.scanned == 9999.0 for c in extra)


def test_changed_word_produces_segment():
    scanned = REFERENCE.replace("Inventories", "lnventorles")  # OCR letter noise
    _, segments, _ = compare_texts(REFERENCE, scanned)
    assert any(s.tag == "replace" for s in segments)


def test_parenthesised_negative_and_currency_parse_equal():
    similarity, _, numbers = compare_texts(
        "Provision (1,200)\nBalance ₹12,450",
        "Provision (1,200)\nBalance ₹12,450",
    )
    assert similarity == 1.0
    assert numbers == []


def test_space_separated_thousands_treated_as_one_number():
    _, _, numbers = compare_texts("Total 1 234 567", "Total 1234567")
    assert numbers == []


def test_normalize_folds_dashes_and_case():
    assert normalize("Foo—Bar") == "foo-bar"
    assert normalize("Foo—Bar", ignore_case=False) == "Foo-Bar"


def test_extract_document_text_reads_plain_text(tmp_path):
    p = tmp_path / "ref.txt"
    p.write_text(REFERENCE, encoding="utf-8")
    pages, ocr_used = extract_document_text(str(p))
    assert ocr_used is False
    assert "Inventories" in pages[0]


def test_compare_documents_on_text_files(tmp_path):
    ref = tmp_path / "ref.txt"
    scan = tmp_path / "scan.txt"
    ref.write_text(REFERENCE, encoding="utf-8")
    scan.write_text(REFERENCE.replace("18,950", "18,350"), encoding="utf-8")

    result = compare_documents(str(ref), str(scan))
    assert isinstance(result, ComparisonResult)
    assert result.identical is False
    assert "changed" in _kinds(result)
    # The JSON view is well-formed and reports the difference.
    data = result.as_dict()
    assert data["identical"] is False
    assert data["number_change_count"] >= 1
