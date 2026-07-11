"""Document auto-ordering for combined exhibits (multiple financials in one
HTML): the corpus must be concatenated in the HTML's own document order so
occurrence-pairing checks never pair a row from one financial with the
other's section."""

from secverify.pdfside import _order_docs_by_html


def _anchors(tag: str) -> list[str]:
    """Three distinct, long, doc-unique anchor lines."""
    return [
        f"{tag}revenuefromoperationslinewithfigures{i}0123456789012345"
        for i in range(3)
    ]


class TestOrderDocsByHtml:
    def test_reorders_to_html_order(self):
        a, b = _anchors("annual"), _anchors("condensed")
        html = "".join(b) + "x" * 50 + "".join(a)
        assert _order_docs_by_html([a, b], html) == [1, 0]

    def test_keeps_given_order_when_already_aligned(self):
        a, b = _anchors("first"), _anchors("second")
        html = "".join(a) + "".join(b)
        assert _order_docs_by_html([a, b], html) == [0, 1]

    def test_shared_anchors_are_dropped(self):
        # an anchor present in BOTH docs (identical SOCIE row in annual and
        # interim) must not place either document
        shared = "exchangedifferencesontranslationofforeignoperations352535"
        a, b = _anchors("annual"), _anchors("interim")
        html = "".join(b) + shared + "".join(a)
        assert _order_docs_by_html([[shared] + a, [shared] + b], html) == [1, 0]

    def test_refuses_to_guess_without_enough_anchors(self):
        # a document with <3 distinctive anchors locatable in the HTML gets
        # no position: the whole ordering is abandoned (given order kept)
        a = _anchors("present")
        b = _anchors("absent")
        html = "".join(a)  # doc b's content missing from the HTML
        assert _order_docs_by_html([a, b], html) is None

    def test_four_documents(self):
        docs = [_anchors(t) for t in ("car", "cfs", "aar", "afs")]
        html = "".join("".join(d) for d in (docs[2], docs[3], docs[0], docs[1]))
        assert _order_docs_by_html(docs, html) == [2, 3, 0, 1]
