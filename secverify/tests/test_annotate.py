from secverify.annotate import Annotator
from secverify.pdfside import PdfCorpus, _merged_numeric_words
from secverify.numbers import iter_tokens
from secverify.textnorm import canonicalize


def make_corpus(pages: list[str]) -> PdfCorpus:
    corpus = PdfCorpus()
    for page_idx, raw in enumerate(pages):
        corpus.pages_raw.append(raw)
        corpus.page_labels.append(f"p.{page_idx + 1}")
        for word in raw.split():
            for _s, _e, token, key in iter_tokens(word):
                corpus._add_number(token, key, page_idx + 1)
        for view, letters_only in ((corpus.alnum, False), (corpus.letters, True)):
            view.page_starts.append(len(view.canon))
            canon, index_map = canonicalize(raw, letters_only=letters_only)
            view.canon += canon
            view.index_map.extend((page_idx, off) for off in index_map)
    corpus.page_letters = [
        canonicalize(raw, letters_only=True)[0] for raw in pages
    ]
    return corpus


PAGE = (
    "Condensed Balance Sheet as at June 30, 2025\n"
    "Property, plant and equipment 9,868 10,070\n"
    "Total assets 1,23,696\n"
    "Trade receivables 27,751"
)

#: an HTML rendering that fully reflects PAGE, line by line
FULL_HTML = (
    "<html><body><p>Condensed Balance Sheet as at June 30, 2025</p>"
    "<table><tr><td>Property, plant and equipment</td><td>9,868</td><td>10,070</td></tr>"
    "<tr><td>Total assets</td><td>1,23,696</td></tr>"
    "<tr><td>Trade receivables</td><td>27,751</td></tr></table>"
    "</body></html>"
)


def run(html: str):
    return Annotator(make_corpus([PAGE])).run(html, "ref.pdf", "doc.html")


def test_full_reflection_goes_green_with_no_issues():
    result = run(FULL_HTML)
    assert not result.issues
    # The four table figures are significant and verified; the header date's
    # "30" and "2025" are immaterial (a day and a year) — present in the PDF
    # but rendered neutral, not counted as verified figures and never green.
    assert result.figures_total == result.figures_ok == 4
    assert result.coverage.missing == 0
    assert result.coverage.ok == result.coverage.total == 4
    assert 'class="secv-num-ok"' in result.html_out
    assert 'class="secv-num-minor"' in result.html_out
    assert "secv-text-ok" in result.html_out


def test_omitted_pdf_row_gets_inline_callout_at_its_position():
    # Drop the Trade receivables row from the HTML entirely.
    html = FULL_HTML.replace(
        "<tr><td>Trade receivables</td><td>27,751</td></tr>", ""
    )
    result = run(html)
    omissions = [i for i in result.issues if i.kind == "omission"]
    assert len(omissions) == 1
    assert "Trade receivables" in omissions[0].excerpt
    assert result.coverage.missing == 1
    # The callout is inserted inline, as a table row right after the last
    # reflected PDF line (Total assets) — no separate coverage section.
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(result.html_out, "html.parser")
    callout = soup.find(class_="secv-callout-bad")
    assert callout is not None
    assert "MISSING FROM HTML" in callout.get_text()
    assert "Trade receivables" in callout.get_text()
    prev_row = callout.find_parent("tr").find_previous_sibling("tr")
    assert "Total assets" in prev_row.get_text()
    assert soup.find(id="secv-coverage") is None


def test_wrong_figure_goes_red_with_remark():
    result = run("<html><body><p>Total assets 1,23,969</p></body></html>")
    figure_issues = [i for i in result.issues if i.kind == "figure"]
    assert len(figure_issues) == 1
    assert "123969" in figure_issues[0].remark
    assert "1,23,696" in figure_issues[0].remark  # transposition suggested
    assert "secv-num-bad" in result.html_out
    assert "SECVERIFY REMARK" in result.html_out


def test_missing_text_goes_red():
    result = run(
        "<html><body><p>This sentence exists only in the HTML filing.</p>"
        "</body></html>"
    )
    text_issues = [i for i in result.issues if i.kind == "text"]
    assert len(text_issues) == 1
    assert text_issues[0].severity == "error"
    assert "secv-text-bad" in result.html_out


def test_reordered_words_downgrade_to_review():
    result = run("<html><body><p>Balance Sheet Condensed</p></body></html>")
    text_issues = [i for i in result.issues if i.kind == "text"]
    assert len(text_issues) == 1
    assert text_issues[0].severity == "review"


def test_own_markers_are_not_treated_as_figures():
    # A red text block gets a [1] marker; the figure pass must not flag it.
    result = run("<html><body><p>Nonexistent auditor paragraph here.</p></body></html>")
    assert not [i for i in result.issues if i.kind == "figure"]



def test_page_continuation_header_reprint_is_not_flagged():
    # A table spanning a PDF page break reprints its column-header row at
    # the top of the next page; the HTML has no page breaks so the header
    # appears once. That must not be reported as a dropped instance.
    corpus = make_corpus(
        [
            "Statement of Cash Flows\n"
            "Particulars Note No. Three months ended June 30,\n"
            "Revenue received 4,204",
            "Particulars Note No. Three months ended June 30,\n"
            "Net cash generated 2,318",
        ]
    )
    html = (
        "<html><body><p>Statement of Cash Flows</p>"
        "<p>Particulars Note No. Three months ended June 30,</p>"
        "<p>Revenue received 4,204</p><p>Net cash generated 2,318</p>"
        "</body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [i for i in result.issues if i.kind == "omission"]
    assert result.coverage.missing == 0


def test_index_page_numbers_are_excluded_but_labels_checked():
    # The PDF's table of contents: dot leaders + page numbers (often garbled
    # by extraction, 19 -> "1.9"). Page numbers must not flag in an
    # unpaginated HTML; a missing index LABEL must still flag.
    corpus = make_corpus(
        [
            "Index Page No.\n"
            "2.11 Equity……………………………………….1.9\n"
            "2.12 Other financial liabilities……………………22\n"
            "2.13 Brand new section……………………31\n"
            "Body content follows here with details 1,234",
        ]
    )
    html = (
        "<html><body><p>2.11 Equity</p><p>2.12 Other financial liabilities</p>"
        "<p>Body content follows here with details 1,234</p></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    omission_texts = [
        i.excerpt for i in result.issues if i.kind == "omission"
    ]
    # 1.9/22/31 (page numbers) and the "Index Page No." header: no flags.
    assert not any("1.9" in t or "Index Page" in t for t in omission_texts)
    assert not [i for i in result.issues if i.kind == "figure-count"]
    # ...but the dropped index label still surfaces.
    assert any("Brand new section" in t for t in omission_texts)
    assert result.coverage.index_entries >= 3


def test_issue_tiers_separate_discrepancies_from_noise():
    corpus = make_corpus(
        [
            "The notes form part of the standalone financial statements\n"
            "Column One Column Two Column Three\n"
            "Alpha beta gamma delta epsilon"
        ]
    )
    html = (
        # discrepancy: consolidated vs standalone (close match)
        "<html><body><p>The notes form part of the consolidated financial statements</p>"
        # layout: words exist on the page, order scrambled
        "<p>Column Three Column One Column Two</p>"
        # absent: no counterpart at all
        "<p>Entirely unrelated auditor paragraph content here</p>"
        "<p>Alpha beta gamma delta epsilon</p></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    tiers = {i.tier for i in result.issues if i.kind == "text"}
    by_tier = {t: [i for i in result.issues if i.tier == t] for t in tiers}
    assert any("consolidated" in i.excerpt for i in by_tier.get("act", []))
    assert any("Column Three" in i.excerpt for i in by_tier.get("layout", []))
    assert any("unrelated" in i.excerpt for i in by_tier.get("absent", []))
    assert "Discrepancies — act on these" in result.html_out


def test_interleaved_table_label_is_layout_not_discrepancy():
    # PDF extraction interleaves another column's words into the label; the
    # label's own words appear IN ORDER, so it must classify as layout.
    corpus = make_corpus(
        [
            "Certificates of deposit carried at fair value through other "
            "Market observable inputs 1,196 3,257 comprehensive income"
        ]
    )
    html = (
        "<html><body><p>Certificates of deposit carried at fair value "
        "through other comprehensive income</p>"
        "<p>Market observable inputs</p><p>1,196</p><p>3,257</p>"
        "</body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    label_issues = [i for i in result.issues if "Certificates" in i.excerpt]
    assert len(label_issues) == 1
    assert label_issues[0].tier == "layout"
    assert "interleaves" in label_issues[0].remark
    # A real wording change must NOT be excused by the interleaving rule.
    html_bad = html.replace("through other comprehensive", "through the consolidated")
    result_bad = Annotator(make_corpus([
        "Certificates of deposit carried at fair value through other "
        "Market observable inputs 1,196 3,257 comprehensive income"
    ])).run(html_bad, "ref.pdf", "doc.html")
    bad = [i for i in result_bad.issues if "Certificates" in i.excerpt]
    assert bad and bad[0].tier == "act"


def _row_corpus():
    return make_corpus(
        ["Interest and dividend income (691) (576)\n"
         "Cash and cash equivalents at end 16,556 14,265"]
    )


def _row_html(a="(691)", b="(576)", c="16,556", d="14,265"):
    return (
        "<html><body><table>"
        f"<tr><td>Interest and dividend income</td><td>{a}</td><td>{b}</td></tr>"
        f"<tr><td>Cash and cash equivalents at end</td><td>{c}</td><td>{d}</td></tr>"
        "</table></body></html>"
    )


def test_clean_rows_pass_value_integrity():
    result = Annotator(_row_corpus()).run(_row_html(), "ref.pdf", "doc.html")
    assert not [
        i for i in result.issues
        if i.kind in ("sign", "column-order", "row-value", "currency", "percent")
    ]


def test_sign_flip_is_caught():
    # PDF shows (691) negative; HTML shows 691 positive.
    result = Annotator(_row_corpus()).run(_row_html(a="691"), "ref.pdf", "doc.html")
    sign = [i for i in result.issues if i.kind == "sign"]
    assert len(sign) == 1 and sign[0].severity == "error"
    assert "negative" in sign[0].remark
    assert "691" in sign[0].excerpt


def test_wrong_identifier_is_caught_and_correct_one_is_not():
    corpus = make_corpus(
        ["Vikas Bagaria Partner Membership No. 060408 UDIN: 25060408BMOCJH5716"]
    )
    # Correct identifiers -> no issue.
    ok_html = (
        "<html><body><p>Vikas Bagaria Partner Membership No. 060408 "
        "UDIN: 25060408BMOCJH5716</p></body></html>"
    )
    r_ok = Annotator(make_corpus(
        ["Vikas Bagaria Partner Membership No. 060408 UDIN: 25060408BMOCJH5716"]
    )).run(ok_html, "ref.pdf", "doc.html")
    assert not [i for i in r_ok.issues if i.kind == "identifier"]
    # A transposed UDIN -> flagged.
    bad_html = ok_html.replace("BMOCJH5716", "BMOCJH5761")
    r_bad = Annotator(corpus).run(bad_html, "ref.pdf", "doc.html")
    ids = [i for i in r_bad.issues if i.kind == "identifier"]
    assert len(ids) == 1 and "UDIN" in ids[0].excerpt


def test_sign_flip_on_short_label_row_is_caught():
    # The demonstrated audit miss: a short-label row's sign flip must be
    # caught by the document-wide census, not the (skipped) row check.
    corpus = make_corpus(["Total equity 84,643 87,332"])
    html = (
        "<html><body><table><tr><td>Total equity</td>"
        "<td>(84,643)</td><td>87,332</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    sign = [i for i in result.issues if i.kind == "sign"]
    assert len(sign) == 1 and "84,643" in sign[0].excerpt


def test_column_order_swap_is_caught():
    # The two period columns transposed.
    result = Annotator(_row_corpus()).run(
        _row_html(c="14,265", d="16,556"), "ref.pdf", "doc.html"
    )
    order = [i for i in result.issues if i.kind == "column-order"]
    assert len(order) == 1 and order[0].severity == "error"


def test_currency_symbol_swap_is_caught():
    # A value-column row (label then figures) with a currency prefix.
    corpus = make_corpus(["Grant date fair value per tranche ₹34.75 ₹34.20"])
    html = (
        "<html><body><table><tr>"
        "<td>Grant date fair value per tranche</td>"
        "<td>$34.75</td><td>$34.20</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    cur = [i for i in result.issues if i.kind == "currency"]
    # The row check reports the row; the document-wide symbol census also reports
    # each swapped magnitude (that census is what catches a swap in PROSE, where
    # no row exists to compare).  So the count is not pinned — what matters is
    # that the swap is caught and the remark names both symbols.
    assert cur, "a currency swap must be caught"
    assert any("₹" in i.remark and "$" in i.remark for i in cur)


def test_currency_symbol_absent_on_one_side_is_not_flagged():
    # ₹ in the PDF, plain number in the HTML (unit stated in a header) — fine.
    corpus = make_corpus(["Grant date fair value per tranche ₹34.75 ₹34.20"])
    html = (
        "<html><body><table><tr>"
        "<td>Grant date fair value per tranche</td>"
        "<td>34.75</td><td>34.20</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [i for i in result.issues if i.kind == "currency"]



def test_section_numbers_are_not_treated_as_figures():
    # "2.15 Provisions 888 993" — 2.15 is a note ref, not an amount; a clean
    # HTML row must not raise a value-integrity issue.
    corpus = make_corpus(["Provisions 2.15 888 993"])
    html = (
        "<html><body><table><tr><td>Provisions</td><td>2.15</td>"
        "<td>888</td><td>993</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [
        i for i in result.issues
        if i.kind in ("sign", "column-order", "row-value")
    ]


def test_minus_sign_negative_matches_parenthesised_negative():
    # PDF (691), HTML -691 — both negative, no sign issue.
    corpus = make_corpus(["Interest and dividend income (691) (576)"])
    html = (
        "<html><body><table><tr><td>Interest and dividend income</td>"
        "<td>-691</td><td>-576</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [i for i in result.issues if i.kind == "sign"]


def test_unit_scale_change_is_caught():
    corpus = make_corpus(
        ["(In ₹ crore)\nTotal assets 1,23,696 1,24,936"]
    )
    html = (
        "<html><body><p>(In ₹ million)</p>"
        "<p>Total assets 1,23,696 1,24,936</p></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    units = [i for i in result.issues if i.kind == "unit-scale"]
    assert len(units) == 1 and "crore" in units[0].remark
    assert "million" in units[0].remark


def test_assurance_scope_is_reported():
    corpus = make_corpus(["Total non current assets 48,443 47,768"])
    html = (
        "<html><body><table><tr><td>Total non current assets</td>"
        "<td>48,443</td><td>47,768</td></tr></table></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert result.coverage.rows_with_figures >= 1
    assert result.coverage.rows_value_checked >= 1
    assert "Assurance" in result.html_out
    assert "machine-verified" in result.html_out


def test_scrambled_row_wins_over_similar_sentence_elsewhere():
    # The fair-valuation row exists (scrambled) on page 1, while page 2 has
    # a DIFFERENT sentence sharing the long tail "...carried at fair value
    # through other comprehensive income". The checker must tie the HTML
    # label to its real row on p.1 (layout), not fuzzy-match the similar
    # sentence on p.2 and call it a discrepancy.
    corpus = make_corpus(
        [
            "Commercial Papers carried at fair value through other Market "
            "observable inputs 1,196 comprehensive income",
            "Interest income on financial assets carried at fair value "
            "through other comprehensive income 512",
        ]
    )
    html = (
        "<html><body><p>Commercial Papers carried at fair value through "
        "other comprehensive income</p>"
        "<p>Market observable inputs</p><p>1,196</p>"
        "<p>Interest income on financial assets carried at fair value "
        "through other comprehensive income</p><p>512</p></body></html>"
    )
    result = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    cp = [i for i in result.issues if "Commercial Papers" in i.excerpt]
    assert len(cp) == 1
    assert cp[0].tier == "layout"
    assert "p.1" in cp[0].remark          # tied to the real row's page
    assert "p.2" not in cp[0].remark


def test_high_frequency_phrase_shortfall_is_ignored():
    # A boilerplate phrase occurring dozens of times cannot be counted
    # reliably; a 1-off must not flag.
    from secverify.coverage import _count_shortfall

    # counts are whole-block occurrences now: a near-equal high count is noise
    assert _count_shortfall("condensedstandalone", 39, 37) is None
    # a clear shortfall on few-occurrence content is reported
    assert _count_shortfall("condensedstandalone", 4, 2) == (4, 2)



def test_figures_swapped_between_line_items_are_caught():
    # Both values exist in the document, so presence/count checks pass —
    # only the row-integrity check can see the swap.
    corpus = make_corpus(
        ["Trade receivables current portion 27,751\nLoans to employees granted 195"]
    )
    good = (
        "<html><body><table>"
        "<tr><td>Trade receivables current portion</td><td>27,751</td></tr>"
        "<tr><td>Loans to employees granted</td><td>195</td></tr>"
        "</table></body></html>"
    )
    result = Annotator(make_corpus(
        ["Trade receivables current portion 27,751\nLoans to employees granted 195"]
    )).run(good, "ref.pdf", "doc.html")
    assert not [i for i in result.issues if "Row integrity" in i.remark]

    swapped = good.replace("<td>27,751</td>", "<td>__X__</td>").replace(
        "<td>195</td>", "<td>27,751</td>"
    ).replace("<td>__X__</td>", "<td>195</td>")
    result = Annotator(corpus).run(swapped, "ref.pdf", "doc.html")
    rows = [i for i in result.issues if "Row integrity" in i.remark]
    assert len(rows) == 2  # both rows now show the wrong figure
    assert all(i.severity == "error" and i.tier == "act" for i in rows)
    assert any("27,751" in i.remark for i in rows)
    assert "swapped" in rows[0].remark


SECTION_A = (
    "Property plant and equipment are stated at cost less accumulated "
    "depreciation and impairment charges if any thereon"
)
SECTION_B = (
    "Goodwill represents the excess of consideration transferred over the "
    "fair value of net identifiable assets acquired in business combinations"
)
SECTION_C = (
    "Leases are recognised as a right of use asset with a corresponding "
    "liability at the date at which the leased asset becomes available"
)


def _order_corpus():
    return make_corpus([SECTION_A, SECTION_B, SECTION_C])


def test_content_in_pdf_order_passes(monkeypatch):
    from secverify import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "ORDER_SLACK", 10)
    html = f"<html><body><p>{SECTION_A}</p><p>{SECTION_B}</p><p>{SECTION_C}</p></body></html>"
    result = Annotator(_order_corpus()).run(html, "ref.pdf", "doc.html")
    assert not result.coverage.order_issues
    assert not [i for i in result.issues if i.kind == "order"]


def test_out_of_order_section_is_flagged(monkeypatch):
    from secverify import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "ORDER_SLACK", 10)
    # PDF order is A(p.1), B(p.2), C(p.3) — the HTML puts C before B.
    html = f"<html><body><p>{SECTION_A}</p><p>{SECTION_C}</p><p>{SECTION_B}</p></body></html>"
    result = Annotator(_order_corpus()).run(html, "ref.pdf", "doc.html")
    order = [i for i in result.issues if i.kind == "order"]
    assert len(order) == 1
    assert order[0].tier == "act" and order[0].severity == "error"
    assert "Out of sequence" in order[0].remark
    # cites the misplaced content's PDF page and where it sits in the HTML
    assert "p.2" in order[0].remark or "p.3" in order[0].remark


def test_summary_banner_injected():
    result = run("<html><body><p>Total assets 1,23,696</p></body></html>")
    assert 'id="secv-summary"' in result.html_out


def test_letter_spaced_digit_fragments_merge():
    words = [
        {"text": "executive", "x0": 278.3, "x1": 304.6, "top": 100.0},
        {"text": "3", "x0": 481.8, "x1": 485.3, "top": 100.0},
        {"text": "0", "x0": 485.3, "x1": 488.7, "top": 100.0},
        {"text": "2", "x0": 540.2, "x1": 543.7, "top": 100.0},
        {"text": "8", "x0": 543.7, "x1": 547.2, "top": 100.0},
    ]
    assert _merged_numeric_words(words) == ["30", "28"]


# --- symbol census: % and currency changes in PROSE -------------------------
#
# Found by the adversarial mutation audit (tools/mutation_audit.py) on a real
# press-release exhibit: canonicalisation strips "%", "₹" and "$", and those
# attributes were only compared INSIDE the gated row check — so a symbol change
# in running text passed every check and stayed green.

def test_dropped_percent_sign_in_prose_is_caught():
    corpus = make_corpus(
        ["Revenues in CC terms grew by 3.8% YoY and by 2.6% QoQ this quarter."]
    )
    html = (
        "<html><body><p>Revenues in CC terms grew by 3.8% YoY and by "
        "2.6 QoQ this quarter.</p></body></html>"
    )
    r = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    hits = [i for i in r.issues if i.kind == "percent"]
    assert hits, "a dropped % turns a rate into a bare count — must be caught"
    assert hits[0].severity == "error"


def test_swapped_currency_in_prose_is_caught():
    corpus = make_corpus(["Large Deal TCV was $3.8 Billion with 55% Net New."])
    html = (
        "<html><body><p>Large Deal TCV was ₹3.8 Billion with 55% Net "
        "New.</p></body></html>"
    )
    r = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    hits = [i for i in r.issues if i.kind == "currency"]
    assert hits, "a $ -> ₹ swap in prose must be caught"


def test_unit_in_header_instead_of_cell_is_not_flagged():
    # the convention the census must respect: the PDF prints the symbol inline,
    # the HTML states the unit once in a header and leaves the figures bare
    corpus = make_corpus(["Grant date fair value per tranche ₹34.75 ₹34.20"])
    html = (
        "<html><body><table><tr><th>Particulars</th><th>in ₹</th><th>in ₹</th></tr>"
        "<tr><td>Grant date fair value per tranche</td>"
        "<td>34.75</td><td>34.20</td></tr></table></body></html>"
    )
    r = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [i for i in r.issues if i.kind == "currency"]


def test_matching_percent_and_currency_stay_silent():
    corpus = make_corpus(["Operating margin was 20.8% and TCV was $3.8 Billion."])
    html = (
        "<html><body><p>Operating margin was 20.8% and TCV was $3.8 "
        "Billion.</p></body></html>"
    )
    r = Annotator(corpus).run(html, "ref.pdf", "doc.html")
    assert not [i for i in r.issues if i.kind in ("percent", "currency")]
