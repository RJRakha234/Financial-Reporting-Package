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


# --- short unreadable header must not be a false red ----------------------

def test_short_unreadable_header_is_review_not_red():
    # PDF's segment header "Particulars" is white-on-blue / merged and does not
    # survive text extraction; the HTML has it as a clean cell.
    pdf = ("Revenue by business segment\nFinancial Services 13,463 12,976\n"
           "Total 48,211 46,402")
    html = ("<table><tr><th>Particulars</th></tr>"
            "<tr><td>Revenue by business segment</td></tr>"
            "<tr><td>Financial Services</td><td>13,463</td><td>12,976</td></tr>"
            "<tr><td>Total</td><td>48,211</td><td>46,402</td></tr></table>")
    r = Annotator(make_corpus([pdf]), level="sigma", strict=True).run(
        html, "r.pdf", "d.html")
    particulars = [
        i for i in r.issues if (i.excerpt or "").strip() == "Particulars"
    ]
    assert particulars, "Particulars should still be surfaced"
    assert particulars[0].severity == "review", "must be review, not a red error"
    assert "could not be located" in particulars[0].remark


# --- immaterial numbers (list markers, years) must not be stamped green ----

def test_list_marker_number_is_not_green():
    # PDF numbers its notes a) b) c); the HTML renumbers them 1. 2. 3.
    # The enumerator "1" must NOT be green (it recurs everywhere; presence is
    # no validation, and here the PDF marker is "a", not "1").
    # "1" recurs in the PDF (as it does in any real document), so the presence
    # check would otherwise stamp it green.
    pdf = ("a) The above information is extracted from the audited report.\n"
           "Refer to note 1 for the accounting policy.")
    html = "<ol><li>1. The above information is extracted from the audited report.</li></ol>"
    r = Annotator(make_corpus([pdf])).run(html, "r.pdf", "d.html")
    one = next(s for s in __import__("bs4").BeautifulSoup(r.html_out, "html.parser")
               .find_all("span") if s.get_text(strip=True) == "1")
    assert "secv-num-ok" not in (one.get("class") or []), "enumerator must not be green"
    assert "secv-num-minor" in (one.get("class") or [])


def test_material_small_amount_with_scale_word_stays_verified():
    # "8 crore" is material even though 8 < 100 — it must remain a real figure
    pdf = "The Company recognised a provision of 8 crore during the quarter."
    html = "<p>The Company recognised a provision of 8 crore during the quarter.</p>"
    r = Annotator(make_corpus([pdf])).run(html, "r.pdf", "d.html")
    eight = next(s for s in __import__("bs4").BeautifulSoup(r.html_out, "html.parser")
                 .find_all("span") if s.get_text(strip=True) == "8")
    assert "secv-num-minor" not in (eight.get("class") or []), "8 crore is material"
