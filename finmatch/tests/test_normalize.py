from finmatch.normalize import is_blank, normalize


def test_cross_currency_lines_match():
    inr = normalize("Cash and cash equivalents   6,75,000   7,20,000")
    usd = normalize("Cash and cash equivalents   288.6   238.0")
    assert inr == usd == "cash and cash equivalents #"


def test_currency_header_matches():
    assert normalize("(₹ in crores)") == normalize("($ in millions)")


def test_year_headers_are_masked():
    assert normalize("31 Mar 2024  31 Mar 2023") == normalize("31 Mar 2025  31 Mar 2024")


def test_parenthesised_negative_and_nil():
    assert normalize("Other income (1,250)") == normalize("Other income –")


def test_real_wording_difference_survives():
    assert normalize("Trade receivables 100") != normalize("Trade and other receivables 5")


def test_keep_currency_distinguishes():
    a = normalize("Total INR 1,000", mask_currency=False)
    b = normalize("Total USD 12", mask_currency=False)
    assert a != b  # currency codes preserved -> different language


def test_blank_detection():
    assert is_blank(normalize("  1,234  (5,678) "))
    assert not is_blank(normalize("Revenue 1,234"))
