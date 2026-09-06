"""Tokenisation / number-reconstruction in the extractor."""

from fincheck.extract import _merge_numberish


def _w(text, x0, x1, top=10.0):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 8}


def _texts(words):
    return [w["text"] for w in words]


def test_merges_space_separated_thousands():
    words = [_w("1", 100, 108), _w("234", 110, 126), _w("567", 128, 144)]
    assert _texts(_merge_numberish(words)) == ["1234567"]


def test_reassembles_character_spaced_negative():
    # "( 3 , 1 5 5 )" rendered one glyph at a time -> single figure.
    xs = ["(", "3", ",", "1", "5", "5", ")"]
    words = [_w(c, 455 + k * 3, 458 + k * 3) for k, c in enumerate(xs)]
    out = _merge_numberish(words)
    assert _texts(out) == ["(3,155)"]


def test_does_not_merge_across_column_gap():
    # Two figures in different columns (large gap) stay separate.
    words = [_w("44,490", 360, 383), _w("40,986", 432, 455)]
    assert _texts(_merge_numberish(words)) == ["44,490", "40,986"]


def test_keeps_year_token_separate_from_day():
    # "30," + "2025" must not fuse into a bogus number; the year stays a token.
    words = [_w("30,", 438, 448), _w("2025", 450, 466)]
    assert _texts(_merge_numberish(words)) == ["30,", "2025"]


def test_label_punctuation_not_fused():
    words = [_w("Profit", 50, 80), _w("(loss)", 82, 110)]
    assert _texts(_merge_numberish(words)) == ["Profit", "(loss)"]
