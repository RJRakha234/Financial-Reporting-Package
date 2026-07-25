"""Ordered number-sequence alignment (secverify.seqdiff).

These tests pin the behaviour that motivates the module: a number is verified by
its POSITION in the document's number sequence, not by whether its value exists
somewhere in the PDF.  That is what makes a small value — a list marker, a note
reference, a bare count — checkable at all; a presence-only test cannot confirm
one, because the digit occurs on nearly every page.
"""

import re

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


def _row_cells(line):
    """Split "Label 1,234 5,678" into a label cell plus one cell per figure."""
    m = re.match(r"^(.*?)((?:\s+[\d,]+)+)$", line)
    if not m:
        return [line]
    return [m.group(1).strip()] + m.group(2).split()


def _soup_table(rows):
    """The same content as a real <table> — where cell order IS reliable, so
    insertions and deletions are itemised rather than counted as prose reflow."""
    trs = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in _row_cells(r)) + "</tr>"
        for r in rows
    )
    return BeautifulSoup(
        f"<html><body><table>{trs}</table></body></html>", "html.parser"
    )


def _find_table(rows, pdf=None):
    return list(sequence_findings(pdf or _PDF, _soup_table(rows)))


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
    # prose insert/delete is counted rather than itemised (paragraph rewrapping
    # and PDF word-run-together noise live here), so the enumerator change shows
    # up in the prose-order count — visible, but not 3 separate items.
    assert "seq-prose-order" in _kinds(rows)


def test_dropped_table_row_is_caught():
    rows = [r for r in _ROWS if "Capital work in progress" not in r]
    hits = [f for f in _find_table(rows) if f[0] == "seq-missing"]
    assert hits and "891" in hits[0][2]


def test_added_table_row_is_caught():
    rows = list(_ROWS)
    rows.insert(5, "Goodwill on consolidation 7,777 8,888")
    hits = [f for f in _find_table(rows) if f[0] == "seq-extra"]
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
    keys = [it[0] for it in pdf_number_sequence(["Alpha 1,000 2,000", "Beta 3,000"])]
    assert keys == ["1000", "2000", "3000"]


def test_html_sequence_preserves_dom_order():
    soup = _soup(["Alpha 1,000 2,000", "Beta 3,000"])
    assert [it[0] for it in html_number_sequence(soup)] == ["1000", "2000", "3000"]


def test_html_sequence_marks_table_membership():
    seq = html_number_sequence(_soup_table(["Alpha 1,000"]))
    assert all(it[2] for it in seq), "table figures must be marked in_table"
    assert not any(it[2] for it in html_number_sequence(_soup(["Alpha 1,000"])))


def test_bulk_runs_are_summarised_not_dropped():
    # a long run of unmatched numbers is aggregated into one finding, so the
    # report stays readable while the count remains visible
    pdf = _PDF + [f"Schedule line {i} value {i * 1000} {i * 1100}" for i in range(30)]
    hits = [f for f in _find_table(_ROWS, pdf) if f[0] == "seq-bulk"]
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


def test_prose_reflow_is_counted_not_itemised():
    # Prose numbers that merely change position (nothing substituted) are
    # aggregated: paragraph rewrapping and PDF extraction that runs words
    # together ("OnApril9,2024,IASBha") reorder a paragraph's numbers while every
    # value is still present.  On a real 38-page filing this one rule turned 37
    # individual review items into a single visible count.
    rows = list(_ROWS)
    rows[7] = "1) Revenue is recognised on transfer of control 1,234 2,345"
    got = _find(rows)
    assert [f[0] for f in got] == ["seq-prose-order"]
    assert "counted here rather than listed one by one" in got[0][3]


def test_prose_substitution_is_still_itemised():
    # The tiering must not weaken the flagship case: a SUBSTITUTED value is
    # itemised wherever it sits, prose included.
    rows = [r.replace("has 3 wholly", "has 8 wholly") for r in _ROWS]
    hits = [f for f in _find(rows) if f[0] == "seq-value"]
    assert hits and hits[0][1] == "error"


def test_table_insertions_are_still_itemised():
    # inside a table, cell order IS preserved by both renderings, so an added
    # or dropped figure stays an individual finding
    rows = list(_ROWS)
    rows.insert(5, "Goodwill on consolidation 7,777 8,888")
    assert [f for f in _find_table(rows) if f[0] == "seq-extra"]


# --- letter-spaced PDF figures ("1 ,812") -----------------------------------

from secverify.pdfside import _rejoin_comma_groups


def test_split_comma_group_is_rejoined():
    # A real fair-value table extracted as "5 07 1 ,812": the line-based checks
    # read "1 ,812" as the two values 1 and 812, then reported the phantom 812 as
    # a figure missing from the HTML — a red error on a correct filing.
    assert _rejoin_comma_groups("carried at amortized cost 5 07 1 ,812") \
        == "carried at amortized cost 507 1,812"
    assert _rejoin_comma_groups("Quoted price 5 ,192 1 ,957") == "Quoted price 5,192 1,957"
    assert _rejoin_comma_groups("Total 1 ,234 ,567") == "Total 1,234,567"


def test_unambiguous_only_nothing_else_touched():
    # already correct text is untouched
    assert _rejoin_comma_groups("normal 1,812 here") == "normal 1,812 here"
    # a 2-digit group is not a thousands group — left alone
    assert _rejoin_comma_groups("note 2 ,15") == "note 2 ,15"
    # ambiguous digit-by-digit spacing needs column geometry, not text rules:
    # "8 3 5 7" may be 83/57 (two columns) or 8357 (one figure); likewise
    # "4 83 4 65" (483/465) has no leading zero to disambiguate it
    assert _rejoin_comma_groups("col 8 3 5 7") == "col 8 3 5 7"
    assert _rejoin_comma_groups("Quoted price 4 83 4 65") == "Quoted price 4 83 4 65"
    # an identifier is never rejoined into a neighbouring figure
    assert _rejoin_comma_groups("DIN 00041245 stays") == "DIN 00041245 stays"


def test_leading_zero_fragment_is_rejoined():
    # "5 07" is 507: no standalone amount begins with 0, which is the same
    # assumption the corpus already makes when it treats a leading-zero run as
    # an identifier rather than a figure.
    assert _rejoin_comma_groups("cost 5 07 here") == "cost 507 here"


# --- symbol changes on ALIGNED occurrences ----------------------------------
#
# Found by tools/mutation_audit.py on a real press release: canonicalisation
# strips "%", "₹" and "$" so a symbol change leaves every digit AND every count
# intact.  Comparing the attributes on a pair the sequence diff has already
# aligned isolates a real symbol change from a merely missing occurrence — the
# failure that made a document-wide count census unusable (a figure rendered as
# an image reads as a dropped "%").

_SYM_PDF = [
    "Revenues in CC terms grew by 3.8% YoY and by 2.6% QoQ for the quarter.",
    "Operating margin was 20.8% and EPS rose 8.6% YoY in the same period.",
    "Large Deal TCV was $3.8 Billion with 55% Net New during the quarter.",
    "Free cash flow was $884 Million for the three months ended June 30, 2025.",
    "Total assets 17,447 17,419", "Total non-current assets 6,203 6,060",
    "Trade payables 422 415", "Income tax expense 329 318",
    "Net profit before interest 809 764", "Other income net 1,234 2,345",
]


def test_dropped_percent_sign_in_prose_is_caught():
    rows = [r.replace("by 2.6% QoQ", "by 2.6 QoQ") for r in _SYM_PDF]
    hits = [f for f in _find(rows, _SYM_PDF) if f[0] == "percent"]
    assert hits, "a dropped % turns a rate into a bare count — must be caught"
    assert hits[0][1] == "error"


def test_swapped_currency_in_prose_is_caught():
    rows = [r.replace("$3.8 Billion", "₹3.8 Billion") for r in _SYM_PDF]
    hits = [f for f in _find(rows, _SYM_PDF) if f[0] == "currency"]
    assert hits, "a $ -> ₹ swap in prose must be caught"
    assert hits[0][1] == "error"


def test_matching_symbols_stay_silent():
    assert _find(_SYM_PDF, _SYM_PDF) == []


def test_bare_grid_figures_are_left_to_the_row_check():
    # A figure alone in a grid cell takes its unit from the column header, so an
    # aligned pair legitimately differs there; the row-value check compares
    # symbols per row instead.  Built explicitly so the figure really is a bare
    # cell (no words), which is what marks it as grid data.
    pdf = ["Schedule of ratios and balances for the period then ended"] + [
        f"Line item {chr(97+i)} ratio {10 + i}.{i}% and balance {1000+i}"
        for i in range(12)
    ]
    cells = "".join(
        f"<tr><td>Line item {chr(97+i)} ratio</td><td>{10+i}.{i}</td>"
        f"<td>{1000+i}</td></tr>" for i in range(12)
    )
    soup = BeautifulSoup(
        "<html><body><p>Schedule of ratios and balances for the period then "
        f"ended</p><table><tr><th>Particulars</th><th>%</th><th>Amount</th></tr>"
        f"{cells}</table></body></html>",
        "html.parser",
    )
    got = [f for f in sequence_findings(pdf, soup) if f[0] == "percent"]
    assert not got, f"bare grid figures must not raise percent findings: {got[:2]}"


def test_prose_inside_a_layout_table_is_still_prose():
    # SEC exhibits nest whole sentences inside <td>.  Keying the prose-only
    # checks on table MEMBERSHIP disabled them on every real filing, so a
    # sentence in a cell must still be treated as prose.
    rows = [r.replace("by 2.6% QoQ", "by 2.6 QoQ") for r in _SYM_PDF]
    soup = BeautifulSoup(
        "<html><body><table><tr><td>"
        + "".join(f"<p>{r}</p>" for r in rows)
        + "</td></tr></table></body></html>",
        "html.parser",
    )
    hits = [f for f in sequence_findings(_SYM_PDF, soup) if f[0] == "percent"]
    assert hits, "a dropped % in a sentence must be caught even inside a <td>"


def test_symbol_finding_recolours_the_changed_number():
    # A dropped % leaves the MAGNITUDE correct, so the number passes the
    # presence check and is stamped green.  The finding must also mark the token
    # itself, or it lives only in the summary panel while the figure a reviewer
    # looks at still reads as verified — which is what the audit observed.
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    rows = [r.replace("by 2.6% QoQ", "by 2.6 QoQ") for r in _SYM_PDF]
    html = "<html><body>" + "".join(f"<p>{r}</p>" for r in rows) + "</body></html>"
    r = Annotator(
        make_corpus(["\n".join(_SYM_PDF)]), level="sigma",
        pdf_paths=["d.pdf"], strict=True,
    ).run(html, "r.pdf", "d.html")
    assert [i for i in r.issues if i.kind == "percent"], "the % change must fire"
    assert "secv-token-bad" in r.html_out, \
        "the changed value must be painted, not left green"


# --- conflicting style attributes -------------------------------------------

def test_duplicate_style_attributes_are_caught():
    # A browser applies the FIRST style attribute, text extractors keep the
    # LAST.  So this element renders invisibly to a reader while every automated
    # check reads it as visible content — and the hidden-text check cannot fire,
    # because the display:none was discarded at parse time.  Found while
    # investigating a mutation-audit miss that turned out to be this shape.
    from secverify.render import duplicate_style_findings

    html = (
        '<p style="display:none" style="font: 10pt Arial">Material fact</p>'
    )
    got = list(duplicate_style_findings(html))
    assert got, "conflicting style attributes must be reported"
    assert got[0][0] == "duplicate-style" and got[0][1] == "error"


def test_single_style_attribute_is_not_flagged():
    from secverify.render import duplicate_style_findings

    assert not list(duplicate_style_findings('<p style="display:none">x</p>'))
    assert not list(duplicate_style_findings('<p style="font: 10pt">ok</p>'))
    assert not list(duplicate_style_findings("<p>no style at all</p>"))


def test_properly_hidden_content_is_still_caught():
    from secverify.render import _hidden_text
    from bs4 import BeautifulSoup as BS

    got = list(_hidden_text(BS('<p style="display:none">Secret</p>', "html.parser")))
    assert got and got[0][0] == "hidden-text"


# --- content hidden by a STYLESHEET, not an inline attribute ----------------
#
# Found while evaluating a PDF-vs-PDF corroboration mode: rendering exposed
# figures the DOM-based checks reported as green. The gap turned out to be in
# the HTML path itself and needed no rendering to fix — the hidden-content check
# only inspected inline style attributes, so a class, an id, or a tag selector
# in a <style> block passed straight through.
#
# This is the more dangerous direction of hidden content. Hidden EXTRA text adds
# something a reader cannot see; a hidden REQUIRED figure means the filing is
# missing a number it must show — and because the value is still in the markup,
# every other check finds it and reports it verified.

def test_figure_hidden_by_a_stylesheet_class_is_caught():
    from secverify.render import _hidden_text

    soup = BeautifulSoup(
        "<html><head><style>.h{display:none}</style></head><body>"
        "<table><tr><td>Trade receivables</td>"
        '<td class="h">33,968</td><td>31,158</td></tr></table></body></html>',
        "html.parser",
    )
    got = [g for g in _hidden_text(soup) if "33,968" in g[2]]
    assert got, "a figure hidden by a stylesheet rule must be caught"
    assert got[0][1] == "error"


def test_figure_clipped_by_overflow_is_caught():
    from secverify.render import _hidden_text

    soup = BeautifulSoup(
        '<p style="max-height:0;overflow:hidden;display:block">33,968</p>',
        "html.parser",
    )
    assert list(_hidden_text(soup)), "content clipped out of view must be caught"


def test_overflow_alone_is_not_hidden():
    # clipping needs BOTH no room AND hidden overflow; either alone is ordinary
    # layout and must not be flagged
    from secverify.render import _hidden_text

    for style in ("overflow:hidden", "height:0", "overflow:auto;height:40pt"):
        soup = BeautifulSoup(f'<p style="{style}">33,968</p>', "html.parser")
        assert not list(_hidden_text(soup)), f"{style} is not concealment"


def test_css_injected_content_is_caught():
    # content:'(' turns a positive into a negative FOR THE READER ONLY — the
    # parentheses are not in the DOM, so no text or sign check can ever see them
    from secverify.render import _hidden_text

    soup = BeautifulSoup(
        "<html><head><style>.neg::before{content:'('}</style></head>"
        '<body><td class="neg">31,832</td></body></html>',
        "html.parser",
    )
    got = list(_hidden_text(soup))
    assert got and "31,832" in got[0][2]


def test_ordinary_stylesheet_rules_are_not_flagged():
    from secverify.render import _hidden_text

    soup = BeautifulSoup(
        "<html><head><style>td{font: 10pt Arial} .r{text-align:right}"
        "@media print{.x{display:none}}</style></head>"
        '<body><td class="r">33,968</td></body></html>',
        "html.parser",
    )
    assert not list(_hidden_text(soup))
