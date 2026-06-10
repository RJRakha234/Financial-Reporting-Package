from fincheck.numbers import format_number, is_numberish, parse_number


def test_plain_and_thousands():
    assert parse_number("1234") == 1234
    assert parse_number("1,234,567") == 1234567
    assert parse_number("1 234 567") == 1234567


def test_negatives():
    assert parse_number("(1,200)") == -1200
    assert parse_number("-500") == -500
    assert parse_number("–500") == -500  # en-dash sign


def test_currency_and_codes():
    assert parse_number("INR 8,750") == 8750
    assert parse_number("₹12,450") == 12450
    assert parse_number("Rs. 100") == 100


def test_nil_and_non_numbers():
    assert parse_number("-") == 0.0
    assert parse_number("nil") == 0.0
    assert parse_number("") is None
    assert parse_number("Total") is None
    assert parse_number("12.5%") is None  # percentages excluded from footing


def test_decimals():
    assert parse_number("1,234.50") == 1234.5
    assert format_number(1234.5) == "1,234.50"
    assert format_number(1234) == "1,234"


def test_is_numberish():
    assert is_numberish("1,234")
    assert is_numberish("(500)")
    assert not is_numberish("Total")
    assert not is_numberish("")
