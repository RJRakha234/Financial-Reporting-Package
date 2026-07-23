"""Date integrity inside otherwise-identical wording.

The failure this guards: a reporting date carried inside a sentence whose
words match the PDF verbatim ("quarter ended June 30, 2025" where the PDF
reads "…2026").  Canonicalisation drops the digits, so the words-only match
would clear the sentence green.  The Tier-2 date guard aligns to the PDF at
that span and compares the dates positionally.
"""

from secverify.annotate import Annotator
from tests.test_annotate import make_corpus


PAGE = (
    "EPS is not annualized for the quarter ended June 30, 2026, "
    "quarter ended March 31, 2026 and quarter ended June 30, 2025.\n"
    "During the year ended March 31, 2026 the Group completed two "
    "business combinations."
)


def run(html: str):
    return Annotator(make_corpus([PAGE])).run(html, "ref.pdf", "doc.html")


def _date_flags(result):
    return [i for i in result.issues if "Date mismatch" in (i.remark or "")]


def test_faithful_dates_stay_green():
    html = (
        "<p>EPS is not annualized for the quarter ended June 30, 2026, "
        "quarter ended March 31, 2026 and quarter ended June 30, 2025.</p>"
    )
    result = run(html)
    assert _date_flags(result) == []


def test_shifted_year_in_matching_sentence_is_caught():
    # exactly the screenshot: every year one behind, words identical
    html = (
        "<p>EPS is not annualized for the quarter ended June 30, 2025, "
        "quarter ended March 31, 2025 and quarter ended June 30, 2024.</p>"
    )
    result = run(html)
    flags = _date_flags(result)
    assert flags, "a shifted reporting year in matching wording must be flagged"
    assert flags[0].severity == "error"
    # the remark names the HTML date and the PDF date it should have been
    assert "june 30 2025" in flags[0].remark.lower()
    assert "june 30 2026" in flags[0].remark.lower()


def test_single_shifted_year_in_prose_is_caught():
    html = (
        "<p>During the year ended March 31, 2025 the Group completed two "
        "business combinations.</p>"
    )
    result = run(html)
    flags = _date_flags(result)
    assert flags, "a wrong year in a verbatim-matching prose sentence must flag"
    assert "march 31 2025" in flags[0].remark.lower()
    assert "march 31 2026" in flags[0].remark.lower()


def test_no_flag_when_no_dates_present():
    # a plain sentence with no calendar date must not engage the guard
    corpus = make_corpus(["The Group completed two business combinations."])
    result = Annotator(corpus).run(
        "<p>The Group completed two business combinations.</p>",
        "ref.pdf", "doc.html",
    )
    assert _date_flags(result) == []


def test_unequal_date_count_does_not_false_flag():
    # PDF side lists two dates, HTML one — a fracture/interleave shape; the
    # guard must stay silent (unequal counts are not comparable) rather than
    # invent a mismatch.
    corpus = make_corpus([
        "Comparatives for March 31, 2026 and March 31, 2025 are shown."
    ])
    # HTML wording is a subset whose letters still appear in the PDF line
    result = Annotator(corpus).run(
        "<p>Comparatives for March 31, 2026 and March 31, 2025 are shown.</p>",
        "ref.pdf", "doc.html",
    )
    assert _date_flags(result) == []
