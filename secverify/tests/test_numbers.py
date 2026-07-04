from decimal import Decimal

from secverify.numbers import (
    canonical_key,
    canonical_value,
    is_significant,
    iter_tokens,
    valid_grouping,
)


def keys(text):
    return [k for _s, _e, _t, k in iter_tokens(text)]


def test_western_and_indian_grouping():
    assert keys("1,234,567.89") == ["1234567.89"]
    assert keys("12,34,567") == ["1234567"]
    assert keys("415,42,72,628") == ["4154272628"]


def test_parenthesised_negative_matches_positive():
    assert canonical_value("(2,318)") == Decimal("-2318")
    assert canonical_key(Decimal("-2318")) == canonical_key(Decimal("2318"))


def test_trailing_zeros_and_percent():
    assert keys("25.30%") == keys("25.3") == ["25.3"]


def test_currency_prefix():
    assert keys("₹ 4,204 and $12") == ["4204", "12"]


def test_date_without_space_is_not_a_grouped_number():
    # "June 30,2025" must yield 30 and 2025, not 302025
    assert not valid_grouping("30,2025")
    assert keys("June 30,2025") == ["30", "2025"]


def test_token_offsets_cover_original_text():
    text = "Total assets 1,23,696 (240)"
    spans = [(s, e, t) for s, e, t, _k in iter_tokens(text)]
    for s, e, t in spans:
        assert text[s:e] == t


def test_significance_filter():
    assert is_significant("56715", "56,715")
    assert is_significant("25.3", "25.3")
    assert not is_significant("2025", "2025")  # year
    assert not is_significant("7", "7")  # note ref / list index
