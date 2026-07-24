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


# --- adjacent row-order swap (Life Sciences <-> Hi-Tech) -------------------

_SEG_PDF = (
    "Revenue by business segment\n"
    "Financial Services 13,463 12,976\nManufacturing 7,668 7,358\n"
    "Energy, Utilities, Resources and Services 6,452 6,114\nRetail 6,172 5,958\n"
    "Communication 5,791 5,752\nLife Sciences 3,842 3,393\nHi-Tech 3,710 3,558\n"
    "All other segments 1,113 1,293\nTotal 48,211 46,402"
)
_ROWS = {
    "fs": "<tr><td>Financial Services</td><td>13,463</td><td>12,976</td></tr>",
    "mf": "<tr><td>Manufacturing</td><td>7,668</td><td>7,358</td></tr>",
    "en": "<tr><td>Energy, Utilities, Resources and Services</td><td>6,452</td><td>6,114</td></tr>",
    "rt": "<tr><td>Retail</td><td>6,172</td><td>5,958</td></tr>",
    "co": "<tr><td>Communication</td><td>5,791</td><td>5,752</td></tr>",
    "ls": "<tr><td>Life Sciences</td><td>3,842</td><td>3,393</td></tr>",
    "ht": "<tr><td>Hi-Tech</td><td>3,710</td><td>3,558</td></tr>",
    "ao": "<tr><td>All other segments</td><td>1,113</td><td>1,293</td></tr>",
    "tt": "<tr><td>Total</td><td>48,211</td><td>46,402</td></tr>",
}


def _seg_html(order):
    body = "<tr><td>Revenue by business segment</td></tr>" + "".join(_ROWS[k] for k in order)
    return f"<table>{body}</table>"


def _seg_run(order):
    return Annotator(make_corpus([_SEG_PDF]), level="sigma", pdf_paths=["d.pdf"],
                     strict=True).run(_seg_html(order), "r.pdf", "d.html")


def test_row_order_swap_is_caught():
    # Hi-Tech and Life Sciences swapped vs the PDF
    r = _seg_run(["fs", "mf", "en", "rt", "co", "ht", "ls", "ao", "tt"])
    assert [i for i in r.issues if i.kind == "grid-row-order"], \
        "an adjacent row-order swap must be caught"


def test_correct_row_order_not_flagged():
    r = _seg_run(["fs", "mf", "en", "rt", "co", "ls", "ht", "ao", "tt"])
    assert [i for i in r.issues if i.kind == "grid-row-order"] == []


# --- transposed table: a swapped segment COLUMN is caught via column-order ---

def test_transposed_column_swap_is_caught():
    # Segments as COLUMNS. Life Sciences and Hi-Tech columns swapped (header +
    # the values beneath). A data row with a normal-length label ("Segment
    # profit") trips the column-order check.
    pdf = ("Segment reporting\n"
           "Particulars Financial Services Manufacturing Energy Retail "
           "Communication Life Sciences Hi-Tech All other Total\n"
           "Revenue 13,463 7,668 6,452 6,172 5,791 3,842 3,710 1,113 48,211\n"
           "Segment profit 3,662 1,685 1,576 1,701 1,180 619 911 75 11,409")
    html = ("<table><tr><th>Particulars</th><th>Financial Services</th>"
            "<th>Manufacturing</th><th>Energy</th><th>Retail</th>"
            "<th>Communication</th><th>Hi-Tech</th><th>Life Sciences</th>"
            "<th>All other</th><th>Total</th></tr>"
            "<tr><td>Revenue</td><td>13,463</td><td>7,668</td><td>6,452</td>"
            "<td>6,172</td><td>5,791</td><td>3,710</td><td>3,842</td>"
            "<td>1,113</td><td>48,211</td></tr>"
            "<tr><td>Segment profit</td><td>3,662</td><td>1,685</td><td>1,576</td>"
            "<td>1,701</td><td>1,180</td><td>911</td><td>619</td><td>75</td>"
            "<td>11,409</td></tr></table>")
    r = Annotator(make_corpus([pdf]), level="sigma", pdf_paths=["d.pdf"],
                  strict=True).run(html, "r.pdf", "d.html")
    assert [i for i in r.issues if i.kind == "column-order"], \
        "a swapped column in a transposed table must be caught"


def test_transposed_correct_order_not_flagged():
    pdf = ("Segment reporting\n"
           "Particulars Financial Services Manufacturing Energy Retail "
           "Communication Life Sciences Hi-Tech All other Total\n"
           "Revenue 13,463 7,668 6,452 6,172 5,791 3,842 3,710 1,113 48,211\n"
           "Segment profit 3,662 1,685 1,576 1,701 1,180 619 911 75 11,409")
    html = ("<table><tr><th>Particulars</th><th>Financial Services</th>"
            "<th>Manufacturing</th><th>Energy</th><th>Retail</th>"
            "<th>Communication</th><th>Life Sciences</th><th>Hi-Tech</th>"
            "<th>All other</th><th>Total</th></tr>"
            "<tr><td>Revenue</td><td>13,463</td><td>7,668</td><td>6,452</td>"
            "<td>6,172</td><td>5,791</td><td>3,842</td><td>3,710</td>"
            "<td>1,113</td><td>48,211</td></tr>"
            "<tr><td>Segment profit</td><td>3,662</td><td>1,685</td><td>1,576</td>"
            "<td>1,701</td><td>1,180</td><td>619</td><td>911</td><td>75</td>"
            "<td>11,409</td></tr></table>")
    r = Annotator(make_corpus([pdf]), level="sigma", pdf_paths=["d.pdf"],
                  strict=True).run(html, "r.pdf", "d.html")
    assert [i for i in r.issues if i.kind == "column-order"] == []


# --- geometry: duplicate-label value swaps (silent-green case #7) -----------

from secverify.grid import _row_swap_findings


def _swaps(html_tables, geom_rows):
    return list(_row_swap_findings(html_tables, geom_rows))


def test_duplicate_label_value_swap_is_caught():
    # "commercial paper" appears twice in one table; the HTML has the two rows'
    # values swapped vs the PDF (same set, reassigned).
    html_tables = [[
        ("commercialpaper", ["5810", "7735"]),   # swapped
        ("otherinvestments", ["38", "60"]),
        ("commercialpaper", ["3255", "6403"]),   # swapped
    ]]
    geom_rows = [
        ("commercialpaper", ["3255", "6403"]),
        ("otherinvestments", ["38", "60"]),
        ("commercialpaper", ["5810", "7735"]),
    ]
    found = _swaps(html_tables, geom_rows)
    assert found, "a duplicate-label value swap must be caught"
    assert found[0][0] == "grid-row-swap"


def test_duplicate_label_correct_order_not_flagged():
    html_tables = [[
        ("commercialpaper", ["3255", "6403"]),
        ("commercialpaper", ["5810", "7735"]),
    ]]
    geom_rows = [
        ("commercialpaper", ["3255", "6403"]),
        ("commercialpaper", ["5810", "7735"]),
    ]
    assert _swaps(html_tables, geom_rows) == []


def test_legitimate_distinct_repeat_not_flagged():
    # current vs non-current income-tax-assets: different VALUES, same order —
    # the value set differs, so it must NOT be treated as a swap.
    html_tables = [[
        ("incometaxassets", ["1835", "2975"]),
        ("incometaxassets", ["666", "1622"]),
    ]]
    geom_rows = [
        ("incometaxassets", ["1835", "2975"]),
        ("incometaxassets", ["666", "1622"]),
    ]
    assert _swaps(html_tables, geom_rows) == []


def test_count_mismatch_not_flagged():
    # label appears twice in the HTML table but 3x across the PDF (another
    # table) — not comparable, must stay silent.
    html_tables = [[
        ("commercialpaper", ["5810", "7735"]),
        ("commercialpaper", ["3255", "6403"]),
    ]]
    geom_rows = [
        ("commercialpaper", ["3255", "6403"]),
        ("commercialpaper", ["5810", "7735"]),
        ("commercialpaper", ["9999", "8888"]),
    ]
    assert _swaps(html_tables, geom_rows) == []


# --- unit-scale: reverse direction (a caption unit absent from the PDF) ------

def test_unit_absent_from_pdf_is_caught():
    # PDF reports in crore; the HTML caption says "lakh", which the PDF never
    # uses — a single table silently rescaled.
    corpus = make_corpus(["Financial statements (In crore)\nTotal assets 1,55,967 1,48,903"])
    html = ("<p>Financial statements (In lakh)</p>"
            "<p>Total assets 1,55,967 1,48,903</p>")
    r = Annotator(corpus).run(html, "r.pdf", "d.html")
    units = [i for i in r.issues if i.kind == "unit-scale"]
    assert units, "an HTML caption unit absent from the PDF must be flagged"
    assert "lakh" in units[0].remark


def test_matching_units_not_flagged():
    corpus = make_corpus(["Financial statements (In crore)\nTotal assets 1,55,967 1,48,903"])
    html = ("<p>Financial statements (In crore)</p>"
            "<p>Total assets 1,55,967 1,48,903</p>")
    r = Annotator(corpus).run(html, "r.pdf", "d.html")
    assert [i for i in r.issues if i.kind == "unit-scale"] == []


# --- segment table: swap where each segment appears twice (revenue+profit) ---

from secverify.grid import _row_sequence_findings

_SEG_GEOM = [
    ("financialservices", ["13463", "12976"]), ("manufacturing", ["7668", "7358"]),
    ("energy", ["6452", "6114"]), ("retail", ["6172", "5958"]),
    ("communication", ["5791", "5752"]), ("lifesciences", ["3842", "3393"]),
    ("hitech", ["3710", "3558"]), ("allothersegments", ["1113", "1293"]),
    ("financialservices", ["3662", "3410"]), ("manufacturing", ["1685", "1541"]),
    ("energy", ["1576", "1548"]), ("retail", ["1701", "1811"]),
    ("communication", ["1180", "1027"]), ("lifesciences", ["619", "659"]),
    ("hitech", ["911", "930"]), ("allothersegments", ["75", "241"]),
]


def test_segment_swap_with_repeated_labels_is_caught():
    # Life Sciences <-> Hi-Tech swapped in the revenue section only; the labels
    # repeat (revenue + profit) so only the (label, values) key disambiguates.
    html = [[
        ("financialservices", ["13463", "12976"]), ("manufacturing", ["7668", "7358"]),
        ("energy", ["6452", "6114"]), ("retail", ["6172", "5958"]),
        ("communication", ["5791", "5752"]),
        ("hitech", ["3710", "3558"]), ("lifesciences", ["3842", "3393"]),
        ("allothersegments", ["1113", "1293"]),
        ("financialservices", ["3662", "3410"]), ("manufacturing", ["1685", "1541"]),
        ("energy", ["1576", "1548"]), ("retail", ["1701", "1811"]),
        ("communication", ["1180", "1027"]), ("lifesciences", ["619", "659"]),
        ("hitech", ["911", "930"]), ("allothersegments", ["75", "241"]),
    ]]
    found = list(_row_sequence_findings(html, _SEG_GEOM))
    assert found, "a swap among repeated-label segment rows must be caught"


def test_segment_correct_order_not_flagged():
    html = [[(l, f) for l, f in _SEG_GEOM]]
    assert list(_row_sequence_findings(html, _SEG_GEOM)) == []


def test_lone_outlier_row_not_flagged():
    # one row's single PDF match lands far away (a coincidental cross-table
    # hit): a lone spike, not an adjacent transposition — must NOT flag.
    geom = [("aaaaaa", ["1"]), ("bbbbbb", ["2"]), ("cccccc", ["3"]),
            ("dddddd", ["4"]), ("eeeeee", ["5"])]
    html = [[("aaaaaa", ["1"]), ("eeeeee", ["5"]), ("bbbbbb", ["2"]),
             ("cccccc", ["3"]), ("dddddd", ["4"])]]
    assert list(_row_sequence_findings(html, geom)) == []


# --- broader structural moves: table relocation, non-adjacent row, column ----

def _run_kinds(pdf, html):
    r = Annotator(make_corpus(pdf), level="sigma", pdf_paths=["d.pdf"],
                  strict=True).run(html, "r.pdf", "d.html")
    return [i.kind for i in r.issues]


def test_whole_table_relocated_is_caught():
    pdf = [
        "Note about the investments held by the group described in detail here now.",
        "Quoted debt securities carried at amortized cost 5,000 6,000\n"
        "Unquoted equity and preference securities held 7,000 8,000\n"
        "Total investments carried at amortized cost 12,000 14,000",
        "Another note about leases and their treatment described in detail here now.",
        "A further note about deferred taxes and their treatment described in detail.",
    ]
    # the table is moved to AFTER the later paragraphs
    html = (
        "<p>Note about the investments held by the group described in detail here now.</p>"
        "<p>Another note about leases and their treatment described in detail here now.</p>"
        "<p>A further note about deferred taxes and their treatment described in detail.</p>"
        "<table><tr><td>Quoted debt securities carried at amortized cost</td><td>5,000</td><td>6,000</td></tr>"
        "<tr><td>Unquoted equity and preference securities held</td><td>7,000</td><td>8,000</td></tr>"
        "<tr><td>Total investments carried at amortized cost</td><td>12,000</td><td>14,000</td></tr></table>"
    )
    assert "order" in _run_kinds(pdf, html), "a relocated table must be flagged"


def test_non_adjacent_row_move_unique_labels_is_caught():
    pdf = ["Big schedule\n" + "\n".join(
        f"Line item {chr(97 + k)} long label {100 + k} {200 + k}" for k in range(16))]
    order = list(range(16))
    order.insert(1, order.pop(15))  # move the 16th row to 2nd
    rows = "".join(
        f"<tr><td>Line item {chr(97 + k)} long label</td>"
        f"<td>{100 + k}</td><td>{200 + k}</td></tr>" for k in order)
    assert "grid-row-order" in _run_kinds(pdf, f"<table>{rows}</table>")


def test_column_swap_is_caught():
    pdf = ["Movement schedule\n"
           "Land holdings total 1,438 11,825 5,544 9,495 3,325 45 31,672\n"
           "Additions in year 0 684 284 486 140 0 1,594\n"
           "Deletions in year 0 2 35 402 39 1 479\n"
           "Depreciation charge 0 113 91 279 60 0 543\n"
           "Closing balance total 1,438 12,574 5,806 9,607 3,449 44 32,918"]
    rows = [("Land holdings total", ["1,438", "11,825", "5,544", "9,495", "3,325", "45", "31,672"]),
            ("Additions in year", ["0", "684", "284", "486", "140", "0", "1,594"]),
            ("Deletions in year", ["0", "2", "35", "402", "39", "1", "479"]),
            ("Depreciation charge", ["0", "113", "91", "279", "60", "0", "543"]),
            ("Closing balance total", ["1,438", "12,574", "5,806", "9,607", "3,449", "44", "32,918"])]
    def swapcol(v):
        v = v[:]; c = v.pop(5); v.insert(1, c); return v  # move 6th col to 2nd
    html = "<table>" + "".join(
        "<tr><td>" + l + "</td>" + "".join(f"<td>{x}</td>" for x in swapcol(v)) + "</tr>"
        for l, v in rows) + "</table>"
    assert "grid-column-order" in _run_kinds(pdf, html)


def test_non_adjacent_move_in_repeated_label_table_is_caught():
    # segment listed twice (revenue + profit); a segment moved SEVERAL places
    # inside the revenue section — not an adjacent swap.
    segs = ["financialservices", "manufacturing", "energy", "retail",
            "communication", "lifesciences", "hitech", "allothersegments"]
    rev = {s: [str(1000 + i), str(900 + i)] for i, s in enumerate(segs)}
    prof = {s: [str(300 + i), str(200 + i)] for i, s in enumerate(segs)}
    geom = [(s, rev[s]) for s in segs] + [(s, prof[s]) for s in segs]
    rev_order = ["financialservices", "hitech", "manufacturing", "energy",
                 "retail", "communication", "lifesciences", "allothersegments"]
    html = [[(s, rev[s]) for s in rev_order] + [(s, prof[s]) for s in segs]]
    found = list(_row_sequence_findings(html, geom))
    assert found, "a non-adjacent move in a repeated-label table must be caught"


def test_spurious_edge_match_not_flagged():
    # a row whose only PDF match sits BEYOND the table's in-order span (a
    # cross-sub-table coincidence) must not be flagged.
    geom = [("aaaaaa", ["1"]), ("bbbbbb", ["2"]), ("cccccc", ["3"]),
            ("dddddd", ["4"]), ("eeeeee", ["5"])]
    html = [[("aaaaaa", ["1"]), ("eeeeee", ["5"]), ("bbbbbb", ["2"]),
             ("cccccc", ["3"]), ("dddddd", ["4"])]]
    assert list(_row_sequence_findings(html, geom)) == []
