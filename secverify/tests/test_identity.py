"""Tests for geometry-bound identifier↔name association (B10, toolsigma)."""

from secverify import identity


def _w(top, x0, text):
    return {"top": float(top), "x0": float(x0), "x1": float(x0 + 8 * len(text)),
            "text": text}


# A columnar signature block: two signatories side by side, each a vertical
# column of name / title / DIN — exactly the layout that defeats flat text.
COLUMNAR = [
    # name line
    _w(100, 205, "Nandan"), _w(100, 240, "M."), _w(100, 258, "Nilekani"),
    _w(100, 473, "Bobby"), _w(100, 505, "Parikh"),
    # title line
    _w(112, 205, "Chairman"), _w(112, 473, "Director"),
    # DIN line
    _w(124, 205, "DIN:"), _w(124, 225, "00041245"),
    _w(124, 473, "DIN:"), _w(124, 493, "00019437"),
]


def test_geom_binds_din_to_its_column_name():
    b = identity._page_geom_bindings(COLUMNAR)
    assert b["00041245"] == {"nandan", "nilekani"}
    assert b["00019437"] == {"bobby", "parikh"}


def test_geom_does_not_cross_columns():
    # Salil's DIN sits one line lower because his title wraps two lines.
    words = COLUMNAR + [
        _w(100, 342, "Salil"), _w(100, 372, "Parekh"),
        _w(112, 342, "Chief"), _w(112, 372, "Executive"),
        _w(124, 342, "and"), _w(124, 350, "Managing"),
        _w(136, 342, "DIN:"), _w(136, 362, "01876159"),
    ]
    b = identity._page_geom_bindings(words)
    assert b["01876159"] == {"salil", "parekh"}


def test_html_binds_preceding_name():
    txt = "Nandan M. Nilekani Chairman DIN: 00041245 Bobby Parikh Director DIN: 00019437"
    h = identity._html_bindings(txt)
    assert h["00041245"] == {"nandan", "nilekani"}
    assert h["00019437"] == {"bobby", "parikh"}


def test_flags_swapped_din(monkeypatch):
    monkeypatch.setattr(
        identity, "_pdf_geom_bindings",
        lambda paths: {"00041245": {"nandan", "nilekani"},
                       "00019437": {"bobby", "parikh"}},
    )
    # HTML has the two DINs against the wrong directors
    html = ("Nandan M. Nilekani Chairman DIN: 00019437 "
            "Bobby Parikh Director DIN: 00041245")
    issues = []
    identity.check_identifier_geometry(
        ["x.pdf"], html,
        lambda k, s, e, r: issues.append((k, e)),
    )
    assert len(issues) == 2
    assert all(k == "identifier-name" for k, _ in issues)


def test_clean_pairing_not_flagged(monkeypatch):
    monkeypatch.setattr(
        identity, "_pdf_geom_bindings",
        lambda paths: {"00041245": {"nandan", "nilekani"},
                       "00019437": {"bobby", "parikh"}},
    )
    html = ("Nandan M. Nilekani Chairman DIN: 00041245 "
            "Bobby Parikh Director DIN: 00019437")
    issues = []
    identity.check_identifier_geometry(
        ["x.pdf"], html, lambda k, s, e, r: issues.append(k)
    )
    assert not issues


def test_superset_pdf_binding_not_flagged(monkeypatch):
    # column bleed makes the PDF name-set a superset; sharing a word => no flag
    monkeypatch.setattr(
        identity, "_pdf_geom_bindings",
        lambda paths: {"00041245": {"nandan", "nilekani", "salil"}},
    )
    html = "Nandan M. Nilekani Chairman DIN: 00041245"
    issues = []
    identity.check_identifier_geometry(
        ["x.pdf"], html, lambda k, s, e, r: issues.append(k)
    )
    assert not issues


# ---- blue manual-review overlay (Class 2/3 zones) -----------------------

def test_review_zones_marks_prose_figures_and_xrefs():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    pages = ["Revenue was 500 crore refer note 12 for details"]
    html = (
        "<html><body><p>Revenue was 500 crore refer note 12 for details</p>"
        "<table><tr><td>Revenue</td><td>500</td></tr></table></body></html>"
    )
    r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                  review_zones=True).run(html, "ref.pdf", "doc.html")
    out = r.html_out
    assert 'class="secv-num-review"' in out    # a prose figure painted blue
    assert 'class="secv-xref-review"' in out   # the cross-reference painted blue
    # in-table figure stays green, not blue
    assert 'class="secv-num-ok"' in out

    # overlay is OFF by default (the class name still appears in the CSS block,
    # so assert on the actual span attribute, which must be absent)
    r2 = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"]).run(
        html, "ref.pdf", "doc.html"
    )
    assert 'class="secv-num-review"' not in r2.html_out
    assert 'class="secv-xref-review"' not in r2.html_out
