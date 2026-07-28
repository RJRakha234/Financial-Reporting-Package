from fractions import Fraction

import pytest

from finround.units import RoundingSpec, ceil_div, floor_div, nearest, parse_value


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234", 1234),
        ("(1,234)", -1234),
        ("1 234 567", 1234567),
        ("₹ 45,000", 45000),
        ("INR -500", -500),
        ("-", 0),
        ("—", 0),
        ("12.50", Fraction(25, 2)),
        (0.1, Fraction(1, 10)),
        (7, 7),
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == expected


def test_parse_value_rejects_text():
    assert parse_value("Total assets") is None
    assert parse_value("") is None


def test_float_input_is_exact_not_binary():
    assert parse_value(0.1) * 3 == Fraction(3, 10)


def test_rounding_helpers_on_negatives():
    assert floor_div(Fraction(-5, 2)) == -3
    assert ceil_div(Fraction(-5, 2)) == -2
    assert nearest(Fraction(-5, 2)) == -3  # halves away from zero
    assert nearest(Fraction(5, 2)) == 3
    assert nearest(Fraction(-7, 3)) == -2


def test_spec_scales_and_steps():
    spec = RoundingSpec.make(scale=1000, decimals=1)
    assert spec.to_units(1234567) == Fraction(12345670, 1000)
    assert spec.from_units(12346) == Fraction(12346, 10)
    assert spec.format(Fraction(12346, 10)) == "1,234.6"
    assert "divided by 1,000" in spec.describe()


def test_spec_with_custom_step():
    spec = RoundingSpec.make(step="0.5")
    assert spec.places == 1
    assert spec.to_units(2) == 4
    assert spec.format(Fraction(-3, 2)) == "-1.5"


def test_spec_rejects_nonsense():
    with pytest.raises(ValueError):
        RoundingSpec.make(step=0)
    with pytest.raises(ValueError):
        RoundingSpec.make(scale=0)
