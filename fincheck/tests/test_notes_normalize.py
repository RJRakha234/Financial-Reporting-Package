from fincheck.notes.normalize import (
    canonical_topic,
    fingerprint,
    infer_kind,
    normalize_with_map,
    parse_kind,
    split_sentences,
)


def test_canonical_topic_merges_title_variants():
    # Case/punctuation collapse automatically.
    assert canonical_topic("PROPERTY, PLANT AND EQUIPMENT") == canonical_topic(
        "Property, plant and equipment"
    )
    # Cross-framework wording variants merge via the alias table.
    assert canonical_topic("Goodwill and other intangible assets") == canonical_topic(
        "GOODWILL AND INTANGIBLE ASSETS"
    )
    assert canonical_topic("Provisions and other contingencies") == canonical_topic(
        "PROVISIONS"
    )
    assert canonical_topic("Prepayments and other assets") == canonical_topic(
        "OTHER ASSETS"
    )


def test_canonical_topic_keeps_distinct_topics_apart():
    assert canonical_topic("Trade receivables") != canonical_topic("Trade payables")
    assert canonical_topic("Other assets") != canonical_topic("Other financial assets")


def test_fingerprint_neutralizes_expected_differences():
    standalone = "The Company recognizes revenue under Ind AS 115."
    consolidated = "The Group recognises revenue under IFRS 15."
    # Entity (Company/Group) and framework (Ind AS 115 / IFRS 15) are expected
    # differences and must be neutralized to the same fingerprint. (The British
    # "recognises" vs American "recognizes" is intentionally NOT neutralized.)
    assert fingerprint("The Company recognizes revenue under Ind AS 115.") == fingerprint(
        "The Group recognizes revenue under IFRS 15."
    )
    assert fingerprint(standalone) != fingerprint(consolidated)  # the z/s remains


def test_fingerprint_is_spacing_agnostic():
    spaced = "The Company shall measure the asset at fair value."
    run_together = "TheCompanyshallmeasuretheassetatfairvalue."
    assert fingerprint(spaced) == fingerprint(run_together)


def test_fingerprint_neutralizes_currency_and_amounts():
    inr = "A provision of ₹126 crore was recognized."
    usd = "A provision of US$15 million was recognized."
    assert fingerprint(inr) == fingerprint(usd)


def test_fingerprint_flags_real_wording_change():
    a = "The asset is measured at amortized cost."
    b = "The asset is measured at fair value."
    assert fingerprint(a) != fingerprint(b)


def test_normalize_with_map_indices_point_into_source():
    text = "The Company holds ₹500 crore in deposits."
    norm, src = normalize_with_map(text)
    assert len(norm) == len(src)
    assert norm == fingerprint(text)
    # Every mapped index addresses a real, non-space character of the source.
    for k in src:
        assert 0 <= k < len(text)
        assert not text[k].isspace()


def test_split_sentences_ignores_decimals_and_clause_numbers():
    text = "See note 2.10 for details. The rate is 5.5 percent. Done."
    units = split_sentences(text)
    assert len(units) == 3
    assert units[0].startswith("See note 2.10")


def test_parse_kind_and_infer_kind():
    k = parse_kind("ifrs-consol-usd")
    assert (k.framework, k.entity, k.currency) == ("ifrs", "consol", "usd")
    assert k.label == "Consolidated IFRS (USD)"

    inferred = infer_kind(
        "Condensed Standalone Financial Statements prepared under Ind AS. (In ₹ crore)"
    )
    assert inferred.framework == "indas"
    assert inferred.entity == "standalone"
    assert inferred.currency == "inr"
