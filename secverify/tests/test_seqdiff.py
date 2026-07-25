"""Ordered number-sequence alignment (secverify.seqdiff).

These tests pin the behaviour that motivates the module: a number is verified by
its POSITION in the document's number sequence, not by whether its value exists
somewhere in the PDF.  That is what makes a small value — a list marker, a note
reference, a bare count — checkable at all; a presence-only test cannot confirm
one, because the digit occurs on nearly every page.
"""

from bs4 import BeautifulSoup

from secverify.seqdiff import (
    html_number_sequence,
    pdf_number_sequence,
    sequence_findings,
)

_PDF = [
    "Condensed Balance Sheet as at June 30, 2025 March 31, 2025",
    "Property plant and equipment 9,868 10,070",
    "Right of use assets 3,201 3,078",
    "Capital work in progress 891 778",
    "Deferred tax assets net 601 497",
    "Total non current assets 48,443 47,768",
    "The Company has 3 wholly-owned subsidiaries as at March 31, 2026.",
    "a) Revenue is recognised on transfer of control 1,234 2,345",
    "b) Leases are measured at present value 3,456 4,567",
    "c) Taxes include deferred amounts 5,678 6,789",
    "Segment revenue Hi-Tech 3,710 3,558 Retail 6,172 5,958",
]

_ROWS = [
    "Condensed Balance Sheet as at June 30, 2025 March 31, 2025",
    "Property plant and equipment 9,868 10,070",
    "Right of use assets 3,201 3,078",
    "Capital work in progress 891 778",
    "Deferred tax assets net 601 497",
    "Total non current assets 48,443 47,768",
    "The Company has 3 wholly-owned subsidiaries as at March 31, 2026.",
    "a) Revenue is recognised on transfer of control 1,234 2,345",
    "b) Leases are measured at present value 3,456 4,567",
    "c) Taxes include deferred amounts 5,678 6,789",
    "Segment revenue Hi-Tech 3,710 3,558 Retail 6,172 5,958",
]


def _soup(rows):
    return BeautifulSoup(
        "<html><body>" + "".join(f"<p>{r}</p>" for r in rows) + "</body></html>",
        "html.parser",
    )


def _find(rows, pdf=None):
    return list(sequence_findings(pdf or _PDF, _soup(rows)))


def _kinds(rows, pdf=None):
    return [f[0] for f in _find(rows, pdf)]


def test_faithful_conversion_aligns_cleanly():
    assert _find(_ROWS) == []


def test_small_count_substitution_is_caught_as_error():
    # "3 subsidiaries" -> "8 subsidiaries".  Both digits exist all over the
    # document, so a presence-only check passes; only the sequence position
    # distinguishes them.  This is the case a set-membership test cannot see.
    rows = [r.replace("has 3 wholly", "has 8 wholly") for r in _ROWS]
    hits = [f for f in _find(rows) if f[0] == "seq-value"]
    assert hits, "a substituted small number must be caught"
    assert hits[0][1] == "error", "a clean one-for-one substitution is an error"
    assert "3" in hits[0][2] and "8" in hits[0][2]


def test_large_value_substitution_is_caught():
    rows = [r.replace("Hi-Tech 3,710", "Hi-Tech 9,999") for r in _ROWS]
    hits = [f for f in _find(rows) if f[0] == "seq-value"]
    assert hits and "9999" in hits[0][2]


def test_changed_list_enumerators_are_caught():
    # PDF enumerates a) b) c); the HTML uses 1) 2) 3).  The HTML therefore
    # carries three numbers with no counterpart — previously these were stamped
    # green because the digits exist elsewhere in the document.
    rows = list(_ROWS)
    for i, (old, new) in enumerate((("a)", "1)"), ("b)", "2)"), ("c)", "3)"))):
        rows[7 + i] = rows[7 + i].replace(old, new, 1)
    assert "seq-extra" in _kinds(rows)


def test_dropped_row_is_caught():
    rows = [r for r in _ROWS if "Capital work in progress" not in r]
    hits = [f for f in _find(rows) if f[0] == "seq-missing"]
    assert hits and "891" in hits[0][2]


def test_added_row_is_caught():
    rows = list(_ROWS)
    rows.insert(5, "Goodwill on consolidation 7,777 8,888")
    hits = [f for f in _find(rows) if f[0] == "seq-extra"]
    assert hits and "7777" in hits[0][2]


def test_swapped_values_are_caught():
    # Hi-Tech and Retail revenue swapped: every value still present, only the
    # order changed, so presence, count and even footing all pass.
    rows = [
        r.replace("Hi-Tech 3,710 3,558 Retail 6,172 5,958",
                  "Hi-Tech 6,172 5,958 Retail 3,710 3,558")
        for r in _ROWS
    ]
    assert any(k.startswith("seq-") for k in _kinds(rows))


# --- layout noise that must NOT be mistaken for a substitution --------------

def test_page_numbers_are_ignored():
    # a page-number-only line carries a digit but no content — it must not
    # align against a real figure and shift everything after it
    pdf = _PDF + ["12", "Page 3 of 40", "| 7 |"]
    assert _find(_ROWS, pdf) == [], "bare page numbers must not align as content"


def test_repeated_header_counted_once():
    # The print layout reprints a table's column header at the top of the next
    # page when the table spans a break; the HTML carries it once.  Only the
    # first PDF occurrence may take part, or the reprint reads as extra content.
    # pages_raw holds one multi-line string PER PAGE, so the header sits in the
    # top-of-page edge band where reprints are recognised.
    header = "Particulars June 30, 2025 March 31, 2025"
    page1 = "\n".join([header] + _PDF[1:6])
    page2 = "\n".join([header] + _PDF[6:])
    rows = [header] + _ROWS[1:]
    assert _find(rows, [page1, page2]) == []


def test_identifier_digits_are_excluded():
    pdf = _PDF + ["Nandan Nilekani DIN: 00041245 Membership No A21918"]
    rows = _ROWS + ["Nandan Nilekani DIN: 00041245 Membership No A21918"]
    assert _find(rows, pdf) == []


def test_short_documents_are_skipped():
    # too few numbers to align meaningfully — the check declines rather than
    # guessing, and the grid/text checks still apply
    assert list(sequence_findings(["Total 5"], _soup(["Total 7"]))) == []


# --- sequence extraction ----------------------------------------------------

def test_sequences_preserve_reading_order():
    keys = [k for k, _c in pdf_number_sequence(["Alpha 1,000 2,000", "Beta 3,000"])]
    assert keys == ["1000", "2000", "3000"]


def test_html_sequence_preserves_dom_order():
    soup = _soup(["Alpha 1,000 2,000", "Beta 3,000"])
    assert [k for k, _c in html_number_sequence(soup)] == ["1000", "2000", "3000"]


def test_bulk_runs_are_summarised_not_dropped():
    # a long run of unmatched numbers is aggregated into one finding, so the
    # report stays readable while the count remains visible
    pdf = _PDF + [f"Schedule line {i} value {i * 1000} {i * 1100}" for i in range(30)]
    hits = [f for f in _find(_ROWS, pdf) if f[0] == "seq-bulk"]
    assert hits, "a large unmatched run must still be reported, in aggregate"
    assert "numbers" in hits[0][3]


# --- print/filing furniture found by running against a real filing ----------
#
# Each of these was a live false positive on a real 8-page press release exhibit
# before the corresponding filter existed.

from secverify.annotate import strip_edgar_submission_header
from secverify.textnorm import running_furniture


def test_running_footer_with_varying_page_number_is_furniture():
    # "Infosys Limited - Press Release Page 3 of 8" — constant wording, varying
    # page number, in the page's edge band.  Left in, this produced 8 phantom
    # "content missing from the HTML" errors and 8 phantom missing numbers.
    pages = [
        f"IFRS - USD Press Release\nBody line {i} with 1,234 and 5,678 in it.\n"
        f"Infosys Limited - Press Release Page {i} of 8"
        for i in range(1, 9)
    ]
    assert running_furniture(pages) == {"infosyslimitedpressreleasepageof"}


def test_repeating_data_row_is_not_furniture():
    # same signature — constant wording, varying digits, top of each page — but
    # the digits are real amounts, not page numbers.  Must be kept as content.
    pages = [f"Schedule line value {i},000 {i},500\nmore body text here" for i in range(1, 6)]
    assert running_furniture(pages) == set()


def test_constant_header_is_not_furniture():
    # identical every page (no varying page number): it is a title banner the
    # HTML carries once, handled by keeping the first occurrence instead
    pages = ["IFRS - USD Press Release\nbody text on this page" for _ in range(5)]
    assert running_furniture(pages) == set()


def test_edgar_submission_header_is_stripped():
    raw = (
        "<DOCUMENT>\n<TYPE>EX-99.1 CHARTER\n<SEQUENCE>2\n"
        "<FILENAME>exv99w01.htm\n<DESCRIPTION>IFRS USD PRESS RELEASE\n<TEXT>\n"
        "<HTML><BODY><P>Real content 1,234</P></BODY></HTML>\n"
    )
    out = strip_edgar_submission_header(raw)
    for gone in ("EX-99.1", "SEQUENCE", "exv99w01.htm", "IFRS USD PRESS RELEASE"):
        assert gone not in out, f"{gone} is EDGAR furniture, not exhibit content"
    assert "Real content 1,234" in out


def test_plain_html_passes_through_unchanged():
    raw = "<html><body><p>Total 1,234</p></body></html>"
    assert strip_edgar_submission_header(raw) == raw


def test_caption_digit_reflow_is_not_reported():
    # PDF sets the caption as "3 months ended 3 months ended" then the dates
    # beneath; the HTML pairs each caption with its date, moving one "3" a few
    # places.  A value swapping with its own identical twin is a no-op.
    pdf = ["\n".join(
        ["Extracted from the Condensed Consolidated Balance Sheet",
         "3 months ended 3 months ended",
         "June 30, 2025 June 30, 2024"]
        + [f"Line item {chr(97+i)} balance {1000+i} {2000+i}" for i in range(12)]
    )]
    rows = (
        ["Extracted from the Condensed Consolidated Balance Sheet",
         "3 months ended June 30, 2025", "3 months ended June 30, 2024"]
        + [f"Line item {chr(97+i)} balance {1000+i} {2000+i}" for i in range(12)]
    )
    assert _find(rows, pdf) == [], "a caption digit moving among its twins is a no-op"


def test_real_value_swap_survives_the_reflow_rule():
    # the reflow rule must stay narrow: two line items exchanging their figures
    # is the classic silent error and must always be reported
    pdf = ["\n".join(
        [f"Line item {chr(97+i)} balance {1000+i} {2000+i}" for i in range(12)]
        + ["Hi-Tech 3,710 3,558", "Retail 6,172 5,958"]
    )]
    rows = (
        [f"Line item {chr(97+i)} balance {1000+i} {2000+i}" for i in range(12)]
        + ["Hi-Tech 6,172 5,958", "Retail 3,710 3,558"]
    )
    assert any(k.startswith("seq-") for k in _kinds(rows, pdf)), \
        "a genuine swap of two items' figures must not be suppressed as reflow"
