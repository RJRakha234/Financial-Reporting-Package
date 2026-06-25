from fincheck.notes.compare import (
    Document,
    compare_documents,
    diff_notes,
)
from fincheck.notes.normalize import DocKind
from fincheck.notes.sections import Section


def _section(number, title, prose):
    # narrative_text keeps no-cell rows >= 25 chars not beside a table row, so
    # give each prose unit its own comfortably long row.
    from fincheck.extract import Row

    rows = [
        Row(page_index=0, top=i * 10, bottom=i * 10 + 8, label=line, label_x0=70)
        for i, line in enumerate(prose)
    ]
    return Section(number=number, title=title, page_start=0, page_end=0, rows=rows)


def _doc(name, kind, sections):
    from fincheck.notes.normalize import canonical_topic

    return Document(name=name, kind=kind, notes={canonical_topic(s.title): s for s in sections})


SA = DocKind("indas", "standalone", "inr")
CO = DocKind("indas", "consol", "inr")


def test_expected_difference_is_not_flagged():
    left = "The Company measures the asset at fair value under Ind AS 109 at each reporting date."
    right = "The Group measures the asset at fair value under IFRS 9 at each reporting date."
    sim, diffs = diff_notes("fininstr", "Financial instruments", "A", left, "B", right)
    assert sim == 1.0
    assert diffs == []


def test_real_wording_difference_is_flagged():
    left = "The asset is subsequently measured at amortized cost using the effective interest method."
    right = "The asset is subsequently measured at fair value through profit or loss every period."
    sim, diffs = diff_notes("fininstr", "Financial instruments", "A", left, "B", right)
    assert sim < 1.0
    assert len(diffs) >= 1
    joined = " ".join(d.left.text + d.right.text for d in diffs).lower()
    assert "amortized" in joined or "fair value" in joined


def test_compare_builds_matrix_and_common_topics():
    common_prose_a = ["The Company prepares these statements under the historical cost convention here."]
    common_prose_b = ["The Group prepares these statements under the historical cost convention here."]
    a = _doc("SA", SA, [
        _section("1.2", "Basis of preparation", common_prose_a),
        _section("2.13", "Trade payables", ["Trade payables are recognized at amortized cost in the books."]),
    ])
    b = _doc("CO", CO, [
        _section("1.2", "Basis of preparation", common_prose_b),
        _section("2.1", "Business combinations", ["Business combinations are accounted using the acquisition method here."]),
    ])
    result = compare_documents([a, b])

    # Basis of preparation is shared; trade payables / business combinations are not.
    assert result.common_topics == [__import__("fincheck.notes.normalize", fromlist=["canonical_topic"]).canonical_topic("Basis of preparation")]
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    # The only shared note differs solely by the entity term -> no difference.
    assert pair.differences == []


def test_difference_hash_is_stable_and_content_sensitive():
    _, d1 = diff_notes("t", "T", "A", "The cost is amortized over five years here.", "B", "The cost is amortized over ten years here.")
    _, d2 = diff_notes("t", "T", "A", "The cost is amortized over five years here.", "B", "The cost is amortized over ten years here.")
    assert d1[0].hash == d2[0].hash  # same content -> same hash
    # Changing the wording changes the hash (re-surfaces for review).
    _, d3 = diff_notes("t", "T", "A", "The cost is amortized over five years here.", "B", "The cost is depreciated over ten years here.")
    assert d3[0].hash != d1[0].hash
