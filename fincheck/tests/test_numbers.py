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


def test_european_grouping_is_not_silently_mangled():
    """1.234.567,89 and 1,234,567.89 are the same amount.

    Stripping commas and hoping turned the first into 456.78912 — a wrong
    number that still looks like a number, which is the one failure mode a
    reconciliation must never have.
    """
    assert parse_number("1.234.567,89") == 1234567.89
    assert parse_number("456.789,12") == 456789.12
    assert parse_number("(1.234,56)") == -1234.56
    # Space grouping never pairs with comma-as-thousands, so the comma decides.
    assert parse_number("12 345,67") == 12345.67


def test_anglo_grouping_still_parses_as_before():
    assert parse_number("1,234,567.89") == 1234567.89
    assert parse_number("1,234") == 1234.0
    assert parse_number("(1,234)") == -1234.0
    assert parse_number("1 234 567") == 1234567.0


def test_a_single_separator_keeps_its_existing_reading():
    """Genuinely ambiguous, so nothing that parsed before changes meaning."""
    assert parse_number("1.234") == 1.234
    assert parse_number("2.19") == 2.19
    assert parse_number("1.5") == 1.5
