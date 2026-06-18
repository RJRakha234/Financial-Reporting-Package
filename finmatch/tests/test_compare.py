from finmatch.compare import compare_lines
from finmatch.extract import Line


def _lines(texts):
    return [Line(page=0, rect=(0, i * 10, 100, i * 10 + 8), text=t) for i, t in enumerate(texts)]


def test_identical_language_different_numbers_is_consistent():
    a = _lines(["Revenue 1,000", "Total assets 9,33,990"])
    b = _lines(["Revenue 12.5", "Total assets 1,270.8"])
    result = compare_lines(a, b)
    assert result.consistent
    assert result.matched == 2
    assert result.coverage == 1.0


def test_missing_line_is_flagged():
    a = _lines(["Goodwill 3,200", "Cash 100"])
    b = _lines(["Cash 5"])
    result = compare_lines(a, b)
    assert not result.consistent
    assert result.mismatched_a == 1  # Goodwill only in A
    assert any(r.kind == "mismatch" and r.a and "Goodwill" in r.a.text for r in result.rows)


def test_reworded_line_is_flagged_on_both_sides():
    a = _lines(["Trade receivables 100"])
    b = _lines(["Trade and other receivables 5"])
    result = compare_lines(a, b)
    assert result.mismatched_a == 1 and result.mismatched_b == 1


def test_blank_numeric_lines_do_not_count():
    a = _lines(["1,234", "Revenue 1,000"])
    b = _lines(["Revenue 12"])
    result = compare_lines(a, b)
    assert result.consistent  # the lone number line is ignored
