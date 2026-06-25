from pdfhtmlcompare.numbers import format_number, is_numberish, parse_number


def test_parse_thousands_and_negatives():
    assert parse_number("1,234,567") == 1234567
    assert parse_number("1 234 567") == 1234567
    assert parse_number("(1,200)") == -1200
    assert parse_number("–500") == -500


def test_parse_currency_and_nil():
    assert parse_number("₹12,450") == 12450
    assert parse_number("INR 8,750") == 8750
    assert parse_number("-") == 0.0
    assert parse_number("12.5%") is None
    assert parse_number("Total") is None


def test_value_comparison_ignores_formatting():
    # numbers are compared by value, so formatting never matters
    assert parse_number("8,750") == parse_number("8750")
    assert parse_number("00041245") == 41245  # leading-zero identifier -> value


def test_is_numberish():
    assert is_numberish("1,234")
    assert is_numberish("(500)")
    assert not is_numberish("Total")
    assert not is_numberish("")


def test_format_number():
    assert format_number(1234.5) == "1,234.50"
    assert format_number(1234) == "1,234"
