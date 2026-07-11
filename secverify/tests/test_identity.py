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

    # HTML wording differs slightly from the PDF, so the sentence cannot be
    # verbatim-confirmed — the figure and reference must be blue-marked.
    pages = ["Revenue was 500 crore refer note 12 for details"]
    html = (
        "<html><body><p>Revenue was 500 crore refer note 12 for the details"
        " thereof</p>"
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


def test_review_zones_marks_unconfirmed_table_row():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # face-value swap of two tax lines; the note repeats both correctly, so the
    # strict checks stay silent — the in-table blue must flag the swapped row.
    pages = [
        "Tax expense recognised in profit or loss",
        "Current tax 4423 4924", "Deferred tax 601 497",
        "Note income tax Current tax 4423 4924 Deferred tax 601 497",
    ]
    html = (
        "<html><body><p>Tax expense recognised in profit or loss</p><table>"
        "<tr><td>Current tax</td><td>601</td><td>497</td></tr>"
        "<tr><td>Deferred tax</td><td>4423</td><td>4924</td></tr></table>"
        "<p>Note income tax Current tax 4423 4924 Deferred tax 601 497</p></body></html>"
    )
    r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                  review_zones=True).run(html, "ref.pdf", "doc.html")
    soup = BeautifulSoup(r.html_out, "html.parser")
    soup.find(id="secv-summary").extract()
    span601 = next(s for s in soup.find_all("span") if s.get_text(strip=True) == "601")
    assert "secv-num-review" in span601.get("class", [])   # swapped row → blue

    # a fully-confirmed row stays green
    ok_pages = [
        "Property plant and equipment 9868 10070", "Right of use assets 3201 3078",
        "Capital work in progress 891 778", "Deferred tax assets 601 497",
        "Total non current assets 14561 14416",
    ]
    ok_html = (
        "<html><body><table>"
        "<tr><td>Property plant and equipment</td><td>9868</td><td>10070</td></tr>"
        "<tr><td>Right of use assets</td><td>3201</td><td>3078</td></tr>"
        "<tr><td>Capital work in progress</td><td>891</td><td>778</td></tr>"
        "<tr><td>Deferred tax assets</td><td>601</td><td>497</td></tr>"
        "<tr><td>Total non current assets</td><td>14561</td><td>14416</td></tr></table></body></html>"
    )
    r2 = Annotator(make_corpus(ok_pages), level="sigma", pdf_paths=["d.pdf"],
                   review_zones=True).run(ok_html, "ref.pdf", "doc.html")
    s2 = BeautifulSoup(r2.html_out, "html.parser"); s2.find(id="secv-summary").extract()
    span = next(s for s in s2.find_all("span") if s.get_text(strip=True) == "9868")
    assert "secv-num-ok" in span.get("class", [])          # confirmed row → green


def test_flagged_identifier_recoloured_not_green():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # DIN present in the HTML signature block but NOT in the PDF -> a review
    # finding; its token must be painted amber, not left plain inside a green
    # (text-matched) block.
    pages = ["Independent Auditors Report", "We have audited the statements."]
    html = (
        "<html><body><p>We have audited the statements.</p>"
        "<p>Nandan M. Nilekani Chairman DIN: 00041245</p></body></html>"
    )
    r = Annotator(make_corpus(pages), level="base", pdf_paths=["d.pdf"]).run(
        html, "ref.pdf", "doc.html"
    )
    soup = BeautifulSoup(r.html_out, "html.parser")
    soup.find(id="secv-summary").extract()
    din = next(s for s in soup.find_all("span") if s.get_text(strip=True) == "00041245")
    assert "secv-token-warn" in din.get("class", [])


def test_context_check_catches_repeated_label_swap():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # "Government securities" recurs: current 1470/1500, non-current 3504/3600.
    # Anchors (Total current / Total non current) pin each table's context.
    filler = [f"Unrelated note item {i} {i}0 {i}5" for i in range(1, 16)]
    pages = ["\n".join([
        "Current investments",
        "Government securities 1470 1500", "Equity shares 97 57",
        "Bonds carried at cost 213 196", "Mutual fund units 476 465",
        "Total current investments 2256 2318",
        *filler,   # real schedules sit pages apart; keep the two contexts distinct
        "Non current investments",
        "Government securities 3504 3600", "Equity instruments 25 20",
        "Bonds measured at amortised 169 160", "Debentures held 3510 1957",
        "Total non current investments 7233 5897",
    ])]

    def html(cur1, cur2):
        return (
            "<html><body><table>"
            f"<tr><td>Government securities</td><td>{cur1}</td><td>{cur2}</td></tr>"
            "<tr><td>Equity shares</td><td>97</td><td>57</td></tr>"
            "<tr><td>Bonds carried at cost</td><td>213</td><td>196</td></tr>"
            "<tr><td>Mutual fund units</td><td>476</td><td>465</td></tr>"
            "<tr><td>Total current investments</td><td>2256</td><td>2318</td></tr></table>"
            "<table><tr><td>Government securities</td><td>3504</td><td>3600</td></tr>"
            "<tr><td>Equity instruments</td><td>25</td><td>20</td></tr>"
            "<tr><td>Bonds measured at amortised</td><td>169</td><td>160</td></tr>"
            "<tr><td>Debentures held</td><td>3510</td><td>1957</td></tr>"
            "<tr><td>Total non current investments</td><td>7233</td><td>5897</td></tr></table></body></html>"
        )

    def blue_in_gov_row(h):
        r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                      review_zones=True).run(h, "ref.pdf", "doc.html")
        soup = BeautifulSoup(r.html_out, "html.parser")
        soup.find(id="secv-summary").extract()
        gov = next(tr for tr in soup.find_all("tr")
                   if "Government securities" in tr.get_text())
        return any("secv-num-review" in " ".join(sp.get("class", []))
                   for sp in gov.find_all("span"))

    # clean: the current gov-sec row matches its context → not blue
    assert not blue_in_gov_row(html("1470", "1500"))
    # exchange swap: current gov-sec shows the non-current figures → blue
    assert blue_in_gov_row(html("3504", "3600"))


def test_context_marks_anchorless_repeated_rows():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # Two mirrored fair-value tables: identical labels, different values, and
    # NO row unique on both sides -> the tool cannot anchor them, so the
    # repeated rows must be blue-marked for the eye.
    pages = ["\n".join([
        "Fair value hierarchy level 1",
        "Government securities 100 200", "Mutual fund units 300 400",
        "Debentures held now 500 600", "Equity instruments 700 800",
        "Bonds at amortised cost 900 950",
        "Fair value hierarchy level 2",
        "Government securities 111 222", "Mutual fund units 333 444",
        "Debentures held now 555 666", "Equity instruments 777 888",
        "Bonds at amortised cost 999 951",
    ])]
    html = (
        "<html><body><table>"
        "<tr><td>Government securities</td><td>100</td><td>200</td></tr>"
        "<tr><td>Mutual fund units</td><td>300</td><td>400</td></tr>"
        "<tr><td>Debentures held now</td><td>500</td><td>600</td></tr>"
        "<tr><td>Equity instruments</td><td>700</td><td>800</td></tr>"
        "<tr><td>Bonds at amortised cost</td><td>900</td><td>950</td></tr></table></body></html>"
    )
    r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                  review_zones=True).run(html, "ref.pdf", "doc.html")
    soup = BeautifulSoup(r.html_out, "html.parser")
    soup.find(id="secv-summary").extract()
    gov = next(tr for tr in soup.find_all("tr")
               if "Government securities" in tr.get_text())
    assert any("secv-num-review" in " ".join(sp.get("class", []))
               for sp in gov.find_all("span"))


def test_scale_word_amount_marked_blue():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # "8 crore" is ₹80,000,000 — a small digit + scale word must be eyeballed,
    # though it dodges the significance filter on its own. The HTML wording
    # differs from the PDF so the sentence is not verbatim-confirmed.
    pages = ["The Company recognised a provision of 8 crore during the quarter"]
    html = ("<html><body><p>The Company recognised a provision of 8 crore "
            "in respect of the quarter</p></body></html>")
    r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                  review_zones=True).run(html, "ref.pdf", "doc.html")
    soup = BeautifulSoup(r.html_out, "html.parser")
    soup.find(id="secv-summary").extract()
    eight = next(s for s in soup.find_all("span") if s.get_text(strip=True) == "8")
    assert "secv-num-review" in eight.get("class", [])


def test_verbatim_sentence_auto_validates_figure():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # The whole sentence (words AND figure, in order) matches the PDF verbatim
    # -> the figure's placement is machine-proven; even strict mode keeps it
    # green instead of blue.
    pages = ["The Company recognised a provision of 8 crore during the quarter"]
    html = ("<html><body><p>The Company recognised a provision of 8 crore "
            "during the quarter</p></body></html>")
    r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                  strict=True).run(html, "ref.pdf", "doc.html")
    soup = BeautifulSoup(r.html_out, "html.parser")
    soup.find(id="secv-summary").extract()
    eight = next(s for s in soup.find_all("span") if s.get_text(strip=True) == "8")
    assert "secv-num-ok" in eight.get("class", [])       # validated, not blue
    assert not soup.find_all("span", class_="secv-xref-review")

    # a swapped figure (that exists elsewhere in the PDF, so presence passes)
    # changes the sentence -> verbatim confirmation fails -> stays blue
    pages2 = [
        "The Company recognised a provision of 8 crore during the quarter. "
        "Other income was 9 crore for the period."
    ]
    html2 = ("<html><body><p>The Company recognised a provision of 9 crore "
             "during the quarter.</p>"
             "<p>Other income was 8 crore for the period.</p></body></html>")
    r2 = Annotator(make_corpus(pages2), level="sigma", pdf_paths=["d.pdf"],
                   strict=True).run(html2, "ref.pdf", "doc.html")
    s2 = BeautifulSoup(r2.html_out, "html.parser"); s2.find(id="secv-summary").extract()
    nine = next(s for s in s2.find_all("span") if s.get_text(strip=True) == "9")
    assert "secv-num-review" in nine.get("class", [])


def test_relocated_paragraph_flagged_for_review():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    pA = ("The provision for onerous contracts was recognised during the "
          "current quarter following a detailed reassessment of obligations.")
    pB = ("The gratuity obligation increased materially as at the reporting "
          "date owing to the revision in actuarial assumptions this year.")
    # PDF order: A then B (separate lines). HTML swaps them — every word
    # verbatim, only the order changed. Must be flagged for review.
    r = Annotator(make_corpus([pA + "\n" + pB]), level="sigma",
                  pdf_paths=["d.pdf"]).run(
        f"<html><body><p>{pB}</p><p>{pA}</p></body></html>", "r.pdf", "d.html")
    soft = [i for i in r.issues if i.kind == "order" and i.severity == "review"]
    assert soft, "swapped verbatim paragraphs must be flagged for review"
    assert not [i for i in r.issues if i.kind == "order" and i.severity == "error"]

    # unswapped: no order issue of any kind
    r2 = Annotator(make_corpus([pA + "\n" + pB]), level="sigma",
                   pdf_paths=["d.pdf"]).run(
        f"<html><body><p>{pA}</p><p>{pB}</p></body></html>", "r.pdf", "d.html")
    assert not [i for i in r2.issues if i.kind == "order"]


def test_occurrence_paired_and_short_heading_anchors():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    def soft(pages, html):
        r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"]).run(
            html, "r.pdf", "d.html")
        return [i for i in r.issues if i.kind == "order" and i.severity == "review"]

    boiler = ("These interim statements should be read in conjunction with "
              "the annual financial statements")
    pA = ("The company recognised revenue in accordance with the applicable "
          "accounting standards during this period.")
    pB = ("Provisions are measured at the present value of the expected "
          "outflows required to settle the obligation.")
    pC = ("Deferred tax is recognised on temporary differences arising "
          "between carrying amounts and the tax bases.")
    pdf = "\n".join([boiler, pA, pB, pC, boiler])
    ok = f"<html><body><p>{boiler}</p><p>{pA}</p><p>{pB}</p><p>{pC}</p><p>{boiler}</p></body></html>"
    moved = f"<html><body><p>{boiler}</p><p>{pA}</p><p>{boiler}</p><p>{pB}</p><p>{pC}</p></body></html>"
    assert not soft([pdf], ok)          # clean repeated boilerplate: silent
    assert soft([pdf], moved)           # relocated 2nd copy: review flag

    pdf2 = "\n".join(["Fair value hierarchy", pA, pB, "Capital management", pC])
    ok2 = (f"<html><body><p>Fair value hierarchy</p><p>{pA}</p><p>{pB}</p>"
           f"<p>Capital management</p><p>{pC}</p></body></html>")
    mv2 = (f"<html><body><p>{pA}</p><p>{pB}</p><p>Capital management</p>"
           f"<p>Fair value hierarchy</p><p>{pC}</p></body></html>")
    assert not soft([pdf2], ok2)        # clean headings: silent
    assert soft([pdf2], mv2)            # moved short heading: review flag


def test_footed_suppresses_single_unconfirmed_row_in_totaled_table():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from bs4 import BeautifulSoup
    from test_annotate import make_corpus
    from secverify.annotate import Annotator

    # one row's figures unconfirmable (note repeats correct pairing elsewhere);
    # the table HAS a total. footed=True: single suspect row is footing's job.
    pages = ["Tax expense recognised in profit or loss",
             "Current tax 4423 4924", "Deferred tax 601 497",
             "Total tax expense 5024 5421",
             "Note income tax Current tax 4423 4924 Deferred tax 601 497"]
    html = ("<html><body><p>Tax expense recognised in profit or loss</p><table>"
            "<tr><td>Current tax</td><td>601</td><td>497</td></tr>"
            "<tr><td>Deferred tax</td><td>4423</td><td>4924</td></tr>"
            "<tr><td>Total tax expense</td><td>5024</td><td>5421</td></tr></table>"
            "<p>Note income tax Current tax 4423 4924 Deferred tax 601 497</p></body></html>")
    def blue601(footed):
        r = Annotator(make_corpus(pages), level="sigma", pdf_paths=["d.pdf"],
                      review_zones=True, footed=footed).run(html, "r.pdf", "d.html")
        soup = BeautifulSoup(r.html_out, "html.parser")
        soup.find(id="secv-summary").extract()
        spans = [s for s in soup.find_all("span")
                 if s.get_text(strip=True) in ("601", "4423")
                 and "secv-num-review" in s.get("class", [])]
        return len(spans)
    # both swapped rows unconfirmed -> 2 pending -> footed does NOT suppress
    assert blue601(footed=True) > 0
    assert blue601(footed=False) > 0
