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
