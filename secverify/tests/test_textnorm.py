from secverify.textnorm import canonical, canonicalize, find_best_match


def test_canonical_ignores_whitespace_punct_and_case():
    # PDF extraction often loses spaces entirely
    assert canonical("The Company recognizes") == canonical("TheCompanyrecognizes")
    assert canonical("auditor’s") == canonical("auditors")
    assert canonical("state-\nments") == canonical("statements")


def test_letters_only_drops_figures():
    assert canonical("Total 1,234 assets", letters_only=True) == "totalassets"


def test_index_map_points_at_source():
    text = "A b—2 ﬁx"
    canon, index_map = canonicalize(text)
    assert canon == "ab2fix"
    assert len(index_map) == len(canon)
    assert text[index_map[0]] == "A"


def test_find_best_match_locates_near_duplicate():
    corpus = canonical(
        "unrelated preamble. the company recognizes revenue upon transfer of "
        "control of promised products or services. closing boilerplate."
    )
    needle = canonical(
        "The Company recognizes revenue upon transfer of control of promised "
        "products and services"  # 'and' vs 'or'
    )
    match = find_best_match(corpus, needle)
    assert match is not None
    start, end, ratio = match
    assert ratio > 0.9
    assert "recognizesrevenue" in corpus[start:end]


def test_find_best_match_rejects_garbage():
    corpus = canonical("completely different content about lease accounting")
    needle = canonical("unrelated auditor independence statement paragraph")
    assert find_best_match(corpus, needle) is None
