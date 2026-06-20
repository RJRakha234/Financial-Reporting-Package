import pytest

from sapfetch.config import PromptLabels
from sapfetch.errors import ConfigError
from sapfetch.period import Period


def test_to_period_defaults_to_from_period():
    p = Period(fiscal_year=2025, from_period=10)
    assert p.to_period == 10
    assert p.label == "FY2025_P10-10"


def test_label_is_zero_padded_and_ranged():
    p = Period(fiscal_year=2025, from_period=1, to_period=10)
    assert p.label == "FY2025_P01-10"


def test_as_prompt_values_maps_labels():
    p = Period(fiscal_year=2025, from_period=1, to_period=10, consol_group="G_GRUP")
    values = p.as_prompt_values(PromptLabels())
    assert values == {
        "Fiscal Year": "2025",
        "From Period": "1",
        "From To": "10",
        "Consol Group": "G_GRUP",
    }


def test_consol_group_omitted_when_absent():
    p = Period(fiscal_year=2025, from_period=1, to_period=10)
    values = p.as_prompt_values(PromptLabels())
    assert "Consol Group" not in values


def test_custom_prompt_labels_are_honoured():
    labels = PromptLabels(fiscal_year="Year", to_period="To Period")
    p = Period(fiscal_year=2024, from_period=3, to_period=6)
    values = p.as_prompt_values(labels)
    assert values["Year"] == "2024"
    assert values["To Period"] == "6"


@pytest.mark.parametrize("kwargs", [
    {"fiscal_year": 12, "from_period": 1},
    {"fiscal_year": 2025, "from_period": 0},
    {"fiscal_year": 2025, "from_period": 17},
    {"fiscal_year": 2025, "from_period": 10, "to_period": 3},
])
def test_invalid_periods_raise(kwargs):
    with pytest.raises(ConfigError):
        Period(**kwargs)
