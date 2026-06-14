from newsalpha import sentiment


def test_bullish_headline_scores_positive():
    s = sentiment.score_text("Nvidia beats estimates and raises guidance on record demand")
    assert s > 0.4
    assert sentiment.label(s) in ("bullish", "very bullish")


def test_bearish_headline_scores_negative():
    s = sentiment.score_text("Tesla misses delivery estimates, stock plunges on weak guidance")
    assert s < -0.4
    assert sentiment.label(s) in ("bearish", "very bearish")


def test_neutral_headline_is_near_zero():
    s = sentiment.score_text("Apple to present at an industry conference next week")
    assert abs(s) < 0.15
    assert sentiment.label(s) == "neutral"


def test_negation_flips_sentiment():
    pos = sentiment.score_text("earnings beat expectations")
    neg = sentiment.score_text("earnings did not beat expectations")
    assert pos > 0
    assert neg < pos


def test_multiword_phrase_dominates_components():
    # "record high" is strongly positive; should not be diluted oddly
    s = sentiment.score_text("shares hit a record high")
    assert s > 0.3


def test_score_is_bounded():
    spam = "surge soar rally beat upgrade record breakthrough " * 10
    assert -1.0 <= sentiment.score_text(spam) <= 1.0


def test_empty_text():
    assert sentiment.score_text("") == 0.0
