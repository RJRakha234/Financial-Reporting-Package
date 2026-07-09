"""Tests for the capability levels: alpha (Phase 1), beta (Phase 2),
sigma (Phase 3)."""

from bs4 import BeautifulSoup

from secverify.annotate import Annotator
from test_annotate import make_corpus


def run(pages, html, level, pdf_paths=("dummy.pdf",)):
    corpus = make_corpus(pages)
    return Annotator(corpus, level=level, pdf_paths=list(pdf_paths)).run(
        html, "ref.pdf", "doc.html"
    )


# ---- alpha: date + identifier-association -------------------------------

def test_alpha_flags_wrong_reporting_period():
    pages = ["Condensed Balance Sheet as at June 30, 2025 March 31, 2025"]
    html = "<html><body><p>Condensed Balance Sheet as at June 30, 2024</p></body></html>"
    r = run(pages, html, "alpha")
    assert [i for i in r.issues if i.kind == "date"]
    # base level does not run the date check
    assert not [i for i in run(pages, html, "base").issues if i.kind == "date"]


def test_alpha_ignores_historical_narrative_date():
    pages = ["Balance Sheet as at June 30, 2025. The 2015 Plan since 2016."]
    html = (
        "<html><body><p>Balance Sheet as at June 30, 2025</p>"
        "<p>the 2015 Plan, on March 31, 2016 the trust ...</p></body></html>"
    )
    r = run(pages, html, "alpha")
    assert not [i for i in r.issues if i.kind == "date"]


def test_alpha_identifier_association_is_disabled():
    # Identifier↔name association (B10) is implemented in phase1 but NOT
    # enabled: real filings' signature blocks are columnar (names and DINs on
    # different lines), so text-proximity binding false-flags correct DINs.
    # Reliable binding needs the geometry engine and is deferred. Alpha must
    # therefore raise NO identifier-name issue even on a transposed pairing.
    pages = ["Nandan Nilekani DIN: 00041245 Bobby Parikh DIN: 00019437"]
    html = (
        "<html><body><p>Nandan Nilekani DIN: 00019437 "
        "Bobby Parikh DIN: 00041245</p></body></html>"
    )
    r = run(pages, html, "alpha")
    assert not [i for i in r.issues if i.kind == "identifier-name"]


# ---- beta: grid ---------------------------------------------------------

_STMT = "\n".join(
    [
        "Condensed Balance Sheet as at June 30, 2025 March 31, 2025",
        "Property plant and equipment 9868 10070",
        "Right of use assets 3201 3078",
        "Capital work in progress 891 778",
        "Deferred tax assets net 601 497",
        "Total non current assets 48443 47768",
    ]
)


def _stmt_html(pp="9868", pp2="10070"):
    return (
        "<html><body><table>"
        "<tr><th>Particulars</th><th>June 30 2025</th><th>March 31 2025</th></tr>"
        f"<tr><td>Property plant and equipment</td><td>{pp}</td><td>{pp2}</td></tr>"
        "<tr><td>Right of use assets</td><td>3201</td><td>3078</td></tr>"
        "<tr><td>Capital work in progress</td><td>891</td><td>778</td></tr>"
        "<tr><td>Deferred tax assets net</td><td>601</td><td>497</td></tr>"
        "<tr><td>Total non current assets</td><td>48443</td><td>47768</td></tr>"
        "</table></body></html>"
    )


def test_beta_clean_statement_has_no_grid_issue():
    r = run([_STMT], _stmt_html(), "beta")
    assert not [i for i in r.issues if i.kind.startswith("grid")]


def test_beta_catches_wrong_value_that_exists_elsewhere():
    # 47768 exists elsewhere in the doc, so base presence would pass.
    r = run([_STMT], _stmt_html(pp="47768"), "beta")
    gv = [i for i in r.issues if i.kind == "grid-value"]
    assert gv and "propertyplantandequipment" in gv[0].excerpt


def test_beta_catches_column_transpose():
    r = run([_STMT], _stmt_html(pp="10070", pp2="9868"), "beta")
    assert [i for i in r.issues if i.kind == "grid-column-order"]


# A doc where "Government securities" recurs (current + non-current schedules)
# with different correct values — the repeated-label case positional alignment
# could not handle without false positives.
_REPEAT = "\n".join(
    [
        "Schedule of current investments",
        "Government securities 100 200",
        "Certificates of deposit 111 222",
        "Commercial papers 333 444",
        "Treasury bills 555 666",
        "Total current investments 1099 1532",
        "Schedule of non current investments",
        "Government securities 300 400",
        "Tax free bonds 121 131",
        "Equity shares 141 151",
        "Preference shares 161 171",
        "Total non current investments 723 853",
    ]
)


def _repeat_html(cur="100", cur2="200"):
    def tbl(gov1, gov2, rows):
        body = f"<tr><td>Government securities</td><td>{gov1}</td><td>{gov2}</td></tr>"
        body += "".join(
            f"<tr><td>{l}</td><td>{a}</td><td>{b}</td></tr>" for l, a, b in rows
        )
        return "<table>" + body + "</table>"

    cur_rows = [("Certificates of deposit", 111, 222),
                ("Commercial papers", 333, 444),
                ("Treasury bills", 555, 666),
                ("Total current investments", 1099, 1532)]
    non_rows = [("Tax free bonds", 121, 131),
                ("Equity shares", 141, 151),
                ("Preference shares", 161, 171),
                ("Total non current investments", 723, 853)]
    return (
        "<html><body>"
        + tbl(cur, cur2, cur_rows)
        + tbl("300", "400", non_rows)
        + "</body></html>"
    )


def test_beta_clean_repeated_label_no_issue():
    r = run([_REPEAT], _repeat_html(), "beta")
    assert not [i for i in r.issues if i.kind.startswith("grid")]


def test_beta_catches_wrong_value_on_repeated_label():
    # "Government securities" in the current schedule is 700/800 — a value that
    # appears against that label nowhere in the PDF (300/400 is the other
    # schedule's correct figure), so it must be flagged.
    r = run([_REPEAT], _repeat_html(cur="700", cur2="800"), "beta")
    gv = [i for i in r.issues if i.kind == "grid-value"]
    assert gv and "governmentsecurities" in gv[0].excerpt


def test_beta_repeated_label_swapped_schedule_not_flagged():
    # current-schedule row carries the NON-current schedule's correct figures;
    # that pair exists against the label in the PDF, so it is (correctly) not
    # flagged — the conservative, zero-false-positive trade-off.
    r = run([_REPEAT], _repeat_html(cur="300", cur2="400"), "beta")
    assert not [i for i in r.issues if i.kind == "grid-value"]


# A wide movement matrix (5 value columns) — the "caution" (brown) tier.
_WIDE = "\n".join(
    [
        "Statement of changes in equity",
        "Balance at April 1 2024 100 200 300 400 1000",
        "Profit for the period 11 22 33 44 110",
        "Other comprehensive income 1 2 3 4 10",
        "Dividends paid 5 6 7 8 26",
        "Balance at June 30 2024 117 230 343 456 1146",
    ]
)


def _wide_html(p1="11", p2="22"):
    return (
        "<html><body><table>"
        "<tr><th>Particulars</th><th>a</th><th>b</th><th>c</th><th>d</th><th>e</th></tr>"
        "<tr><td>Balance at April 1 2024</td><td>100</td><td>200</td><td>300</td><td>400</td><td>1000</td></tr>"
        f"<tr><td>Profit for the period</td><td>{p1}</td><td>{p2}</td><td>33</td><td>44</td><td>110</td></tr>"
        "<tr><td>Other comprehensive income</td><td>1</td><td>2</td><td>3</td><td>4</td><td>10</td></tr>"
        "<tr><td>Dividends paid</td><td>5</td><td>6</td><td>7</td><td>8</td><td>26</td></tr>"
        "<tr><td>Balance at June 30 2024</td><td>117</td><td>230</td><td>343</td><td>456</td><td>1146</td></tr>"
        "</table></body></html>"
    )


def test_wide_matrix_clean_no_issue():
    r = run([_WIDE], _wide_html(), "beta")
    assert not [i for i in r.issues if i.kind.startswith("grid")]


def test_wide_matrix_flagged_as_caution_not_error():
    # columns 1 and 2 of the profit row transposed inside a 5-column matrix
    r = run([_WIDE], _wide_html(p1="22", p2="11"), "beta")
    grid = [i for i in r.issues if i.kind.startswith("grid")]
    assert grid, "wide-matrix mismatch should be surfaced"
    assert all(i.severity == "caution" and i.tier == "caution" for i in grid)
    # and it must NOT contaminate the red/act tier
    assert not [i for i in r.issues if i.kind.startswith("grid") and i.severity == "error"]


def test_grid_off_below_beta():
    r = run([_STMT], _stmt_html(pp="47768"), "alpha")
    assert not [i for i in r.issues if i.kind.startswith("grid")]


# ---- sigma: hidden text -------------------------------------------------

def test_sigma_flags_hidden_text():
    pages = ["Some content 123"]
    html = (
        "<html><body><p>Some content 123</p>"
        "<p style='display:none'>invisible secret text here</p></body></html>"
    )
    r = run(pages, html, "sigma")
    assert [i for i in r.issues if i.kind == "hidden-text"]
    assert not [i for i in run(pages, html, "beta").issues if i.kind == "hidden-text"]
