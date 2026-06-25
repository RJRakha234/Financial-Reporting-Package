from pdfhtmlcompare.numbers import format_number, is_figure, parse_number


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


def test_is_figure_accepts_real_figures():
    assert is_figure("12,450") == (True, 12450.0)
    assert is_figure("(1,200)") == (True, -1200.0)
    assert is_figure("800")[0] is True
    assert is_figure("332")[0] is True


def test_is_figure_rejects_non_statement_numbers():
    # four-digit years
    assert is_figure("2025")[0] is False
    assert is_figure("2024")[0] is False
    # identifier numbers with a leading zero (DIN / membership / reg no.)
    assert is_figure("00041245")[0] is False
    assert is_figure("060408")[0] is False
    # clause / note references
    assert is_figure("2.5")[0] is False
    assert is_figure("2.10")[0] is False
    # footnote markers
    assert is_figure("(1)")[0] is False
    # not a number at all
    assert is_figure("Loans")[0] is False


def test_format_number():
    assert format_number(1234.5) == "1,234.50"
    assert format_number(1234) == "1,234"
